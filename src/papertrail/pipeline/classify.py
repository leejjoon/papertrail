#!/usr/bin/env python3
"""Stage B — LLM classification sidecar per corpus.yaml's taxonomy.

One BULK_MODEL call per paper: title + truncated body text -> facet tags from a
closed vocabulary (research_theme, method, facility, role_of_wasp12b for
wasp12b — the taxonomy is corpus.yaml-defined). Output:
<corpus>/data/classification/<doc_id>.yaml. Coarse tagging only (not a
citation-bearing claim), so truncated context is fine here unlike Stage C/D.

Usage:
    papertrail classify --dry-run
    papertrail classify
    papertrail classify --only 2013Icar..225..432S --force
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yaml
from tqdm import tqdm

from .. import prompts
from ..config import Settings, load_settings
from .llm import get_client

MAX_BODY_WORDS = 4000
RETRIES = 2


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


def build_system_prompt(settings: Settings, taxonomy: dict) -> str:
    """The taxonomy-driven output_example (papertrail migration Step 9,
    Seam 2) fixes a real bug: the pre-Step-9 prompt built facet_lines
    dynamically from the taxonomy but then hardcoded the required-output
    example as a LITERAL naming wasp12b's four facets by name — a
    different corpus's taxonomy got a prompt whose own example
    contradicted it."""
    facet_lines = []
    output_lines = []
    for facet, spec in taxonomy.items():
        multi = "multiple values allowed" if spec.get("multi") else "exactly one value"
        facet_lines.append(f"- {facet} ({multi}): {', '.join(spec['values'])}")
        output_lines.append(f"{facet}: [value1, value2]" if spec.get("multi") else f"{facet}: value")
    output_lines.append("confidence: high|medium|low")
    output_lines.append("evidence: one or two sentences citing what in the text supports these tags")
    return prompts.render(settings.corpus, "classify.system",
                          facet_lines="\n".join(facet_lines),
                          output_example="\n".join(output_lines))


def build_prompt(system: str, title: str, body: str) -> tuple[str, str]:
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


def classify_one(client, model: str, system: str, title: str, body: str, taxonomy: dict) -> dict:
    system, user = build_prompt(system, title, body)
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
            model=model,
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


def process_paper(settings: Settings, client, doc_id: str, papers_by_id: dict,
                   taxonomy: dict, system_prompt: str, prompt_version: str) -> tuple[str, str | None]:
    """Returns (doc_id, error_or_None). Runs in a worker thread."""
    section_tree = json.loads((settings.sections_dir / f"{doc_id}.json").read_text(encoding="utf-8"))
    title = papers_by_id.get(doc_id, {}).get("title", "")
    body = truncated_body(section_tree)
    try:
        result = classify_one(client, settings.bulk_model, system_prompt, title, body, taxonomy)
    except Exception as e:
        return doc_id, str(e)

    result["generator"] = {
        "model": settings.bulk_model, "prompt_version": prompt_version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    result["doc_id"] = doc_id
    out_path = settings.classification_dir / f"{doc_id}.yaml"
    tmp = out_path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(result, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, out_path)
    return doc_id, None


def classification_status(settings: Settings, doc_id: str, prompt_version: str) -> str:
    """"missing" | "current" | "stale" — a version mismatch used to
    silently re-run every paper on any prompt tweak, however cosmetic
    (papertrail migration Step 9 gives classify.py the same
    --regenerate-if-prompt-changed gate paper_pages.py already had —
    necessary now that prompt_version is a content hash, not a manually
    bumped literal, so a hash change is the common case, not the rare one)."""
    out_path = settings.classification_dir / f"{doc_id}.yaml"
    if not out_path.exists():
        return "missing"
    try:
        existing = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    except Exception:
        return "missing"
    existing_version = (existing or {}).get("generator", {}).get("prompt_version")
    if existing_version == prompt_version:
        return "current"
    return "stale" if existing_version is not None else "missing"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Classify papers per corpus.yaml's taxonomy.")
    parser.add_argument("--corpus", metavar="PATH")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", metavar="DOC_ID")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--regenerate-if-prompt-changed", action="store_true")
    return parser


def run(settings: Settings, args) -> None:
    if not settings.api_key:
        sys.exit("API_KEY not found in .env")

    taxonomy = settings.corpus.raw["taxonomy"]
    system_prompt = build_system_prompt(settings, taxonomy)
    prompt_version = prompts.stage_prompt_version(system_prompt)

    section_files = sorted(settings.sections_dir.glob("*.json"))
    todo = []
    done = 0
    stale = 0
    for f in section_files:
        doc_id = f.stem
        if args.only and doc_id != args.only:
            continue
        status = "missing" if args.force else classification_status(settings, doc_id, prompt_version)
        if status == "current":
            done += 1
            continue
        if status == "stale" and not args.regenerate_if_prompt_changed:
            done += 1
            stale += 1
            continue
        todo.append(doc_id)
    if args.limit:
        todo = todo[:args.limit]

    print(f"Section trees: {len(section_files)} | already classified: {done} | "
          f"to classify: {len(todo)}")
    if stale:
        print(f"  ({stale} classified with a different prompt version — "
              f"use --regenerate-if-prompt-changed to redo them)")
    if args.dry_run:
        for d in todo:
            print(f"  [dry-run] {d}")
        return
    if not todo:
        return

    # doc_id -> title, via the metadata adapter (single pass, avoid a lookup miss
    # for the 2 papers with no PDF — those never reach this stage since there's
    # no section-tree file for them in the first place).
    papers_by_id = {p["doc_id"]: p for p in settings.corpus.load_papers()}
    client = get_client(settings)

    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_paper, settings, client, doc_id, papers_by_id,
                                   taxonomy, system_prompt, prompt_version): doc_id
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


def main(argv=None) -> None:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings(args.corpus, create_dirs=True)
    run(settings, args)


if __name__ == "__main__":
    main()
