#!/usr/bin/env python3
"""Stage B — LLM classification sidecar per taxonomy in kb/corpus.yaml.

One BULK_MODEL call per paper: title + truncated body text -> facet tags from a
closed vocabulary (research_theme, method, facility, role_of_wasp12b). Output:
kb/data/classification/<doc_id>.yaml. Coarse tagging only (not a citation-bearing
claim), so truncated context is fine here unlike Stage C/D.

Usage:
    python3 kb/classify.py --dry-run
    python3 kb/classify.py
    python3 kb/classify.py --only 2013Icar..225..432S --force
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml
from openai import OpenAI
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))
from kb import config

PROMPT_VERSION = "1"
MAX_BODY_WORDS = 4000
RETRIES = 2

client = OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY)


def flatten_text(nodes: list[dict]) -> str:
    parts = []
    for n in nodes:
        if n["kind"] == "container":
            parts.append(f"\n## {n['title']}\n")
            parts.append(flatten_text(n["nodes"]))
        elif n.get("block_type") in ("paragraph", "footnote"):
            parts.append(n["text"])
    return "\n\n".join(p for p in parts if p)


def truncated_body(section_tree: dict, max_words: int = MAX_BODY_WORDS) -> str:
    text = flatten_text(section_tree["structure"])
    words = text.split()
    return " ".join(words[:max_words])


def build_prompt(taxonomy: dict, title: str, body: str) -> tuple[str, str]:
    facet_lines = []
    for facet, spec in taxonomy.items():
        multi = "multiple values allowed" if spec.get("multi") else "exactly one value"
        facet_lines.append(f"- {facet} ({multi}): {', '.join(spec['values'])}")
    system = (
        "You classify astronomy papers for a knowledge base about the exoplanet "
        "WASP-12b. Tag the paper against this fixed taxonomy — use ONLY the listed "
        "values, never invent a new one. If a facet genuinely doesn't apply, return "
        "an empty list (multi-value facets) rather than guessing. Reason briefly "
        "in a few sentences at most, then give your final answer — do not write "
        "a long analysis.\n\n"
        "Taxonomy:\n" + "\n".join(facet_lines) + "\n\n"
        "End your response with a fenced yaml block shaped exactly like:\n"
        "```yaml\n"
        "research_theme: [value1, value2]\n"
        "method: [value1]\n"
        "facility: [value1]\n"
        "role_of_wasp12b: value\n"
        "confidence: high|medium|low\n"
        "evidence: one or two sentences citing what in the text supports these tags\n"
        "```"
    )
    user = f"# Title\n{title}\n\n# Paper text (truncated)\n{body}"
    return system, user


def parse_response(text: str) -> dict | None:
    m = re.search(r"```(?:yaml)?\s*\n(.*?)```", text, re.DOTALL)
    block = m.group(1) if m else text
    try:
        return yaml.safe_load(block)
    except yaml.YAMLError:
        return None


def validate(result: dict, taxonomy: dict) -> str | None:
    """Return an error string, or None if the result is well-formed."""
    if not isinstance(result, dict):
        return "response is not a mapping"
    for facet, spec in taxonomy.items():
        if facet not in result:
            return f"missing facet '{facet}'"
        value = result[facet]
        allowed = set(spec["values"])
        if spec.get("multi"):
            if not isinstance(value, list):
                return f"'{facet}' should be a list"
            bad = [v for v in value if v not in allowed]
            if bad:
                return f"'{facet}' has invalid value(s): {bad}"
        else:
            if value not in allowed:
                return f"'{facet}' has invalid value: {value!r}"
    return None


def classify_one(doc_id: str, title: str, body: str, taxonomy: dict) -> dict:
    system, user = build_prompt(taxonomy, title, body)
    last_error = None
    for attempt in range(RETRIES + 1):
        # deepseek-v4-flash-0731's chain-of-thought reasoning counts against
        # completion_tokens (no separate reasoning field on this gateway), and
        # its length varies a LOT by paper — verified some papers need >1500,
        # even >4000 tokens of reasoning before any visible content is emitted
        # at all (content comes back completely empty, not truncated-visible,
        # when the budget runs out mid-reasoning). Escalate per retry rather
        # than paying a huge default budget for every one of the other papers
        # that resolve fine at 4000.
        max_tokens = 4000 * (2 ** attempt)  # 4000, 8000, 16000
        resp = client.chat.completions.create(
            model=config.BULK_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.1,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        result = parse_response(text)
        if result is not None:
            error = validate(result, taxonomy)
            if error is None:
                return result
            last_error = error
        else:
            last_error = "could not parse yaml block from response"
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    raise ValueError(f"classification failed after {RETRIES + 1} attempts: {last_error}")


def process_paper(doc_id: str, papers_by_id: dict, taxonomy: dict) -> tuple[str, str | None]:
    """Returns (doc_id, error_or_None). Runs in a worker thread."""
    section_tree = json.loads((config.SECTIONS_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
    title = papers_by_id.get(doc_id, {}).get("title", "")
    body = truncated_body(section_tree)
    try:
        result = classify_one(doc_id, title, body, taxonomy)
    except Exception as e:
        return doc_id, str(e)

    result["generator"] = {
        "model": config.BULK_MODEL, "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    result["doc_id"] = doc_id
    out_path = config.CLASSIFICATION_DIR / f"{doc_id}.yaml"
    tmp = out_path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(result, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, out_path)
    return doc_id, None


def already_done(doc_id: str) -> bool:
    out_path = config.CLASSIFICATION_DIR / f"{doc_id}.yaml"
    if not out_path.exists():
        return False
    try:
        existing = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (existing or {}).get("generator", {}).get("prompt_version") == PROMPT_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify papers per kb/corpus.yaml's taxonomy.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", metavar="DOC_ID")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    if not config.API_KEY:
        sys.exit("API_KEY not found in .env")

    cfg = config.load_corpus_config()
    taxonomy = cfg["taxonomy"]

    section_files = sorted(config.SECTIONS_DIR.glob("*.json"))
    todo = []
    done = 0
    for f in section_files:
        doc_id = f.stem
        if args.only and doc_id != args.only:
            continue
        if not args.force and already_done(doc_id):
            done += 1
            continue
        todo.append(doc_id)
    if args.limit:
        todo = todo[:args.limit]

    print(f"Section trees: {len(section_files)} | already classified: {done} | "
          f"to classify: {len(todo)}")
    if args.dry_run:
        for d in todo:
            print(f"  [dry-run] {d}")
        return
    if not todo:
        return

    # doc_id -> title, via the metadata adapter (single pass, avoid a lookup miss
    # for the 2 papers with no PDF — those never reach this stage since there's
    # no section-tree file for them in the first place).
    papers_by_id = {p["doc_id"]: p for p in config.load_papers()}

    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_paper, doc_id, papers_by_id, taxonomy): doc_id
                   for doc_id in todo}
        with tqdm(total=len(futures), unit="paper", desc="Classifying") as pbar:
            for future in as_completed(futures):
                doc_id, error = future.result()
                if error:
                    tqdm.write(f"  FAILED {doc_id}: {error}")
                    failed.append(doc_id)
                pbar.update(1)

    print(f"\nClassified: {len(todo) - len(failed)} | failed: {len(failed)}")
    for d in failed:
        print(f"  FAILED {d}")


if __name__ == "__main__":
    main()
