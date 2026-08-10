#!/usr/bin/env python3
"""Stage A — Marker JSON -> section-tree JSON + image assets.

For each paper's <marker_dir>/<doc_id>.json, walks Datalab's
Document -> Page -> leaf-block tree and assembles a navigator-compatible section
tree with paragraph/table/figure/equation/footnote/reference granularity (see
docs/spec/01_data_contracts.md for the full schema and rationale). Fully
deterministic — no LLM calls.

Key fixes over the kasi_papers_analysis reference implementation this was adapted
from (kb/03_extract_text.py there):
  - ListGroup blocks arrive from Datalab's hosted API with ZERO children — the
    whole <ol>/<ul> lives in the block's `html`. kasi's extractor treats
    ListGroup as a pure container and recurses into (empty) children, silently
    dropping every bibliography. This extractor parses <li> items out of `html`
    directly, each becoming its own citable `reference` leaf node.
  - Figure images: despite this corpus being converted with
    disable_image_extraction=true, every Figure block's `images` field already
    carries full base64 image data (verified: 269/269 in a sample) — no PDF
    reconversion needed, just decode-and-write here.

Usage:
    papertrail sections --dry-run
    papertrail sections
    papertrail sections --only 2013Icar..225..432S
    papertrail sections --force
"""
import argparse
import base64
import hashlib
import itertools
import json
import os
import re

from bs4 import BeautifulSoup, NavigableString, Tag
from tqdm import tqdm

from ..config import Settings, load_settings

# Block types with no citable content of their own — recursed into (Page) or
# dropped outright (structural/decorative). Picture = uncaptioned image (no
# Caption sibling seen); Code kept for adoption to other corpora, never occurs here.
RECURSE_TYPES = {"Page"}
DROP_TYPES = {"PageHeader", "PageFooter", "Picture", "TableOfContents"}


# ---------------------------------------------------------------------------
# Inline HTML -> markdown-ish text
# ---------------------------------------------------------------------------

def _render_inline(node) -> str:
    parts = []
    for child in node.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            name = child.name.lower()
            if name in ("i", "em"):
                parts.append(f"*{_render_inline(child)}*")
            elif name in ("b", "strong"):
                parts.append(f"**{_render_inline(child)}**")
            elif name in ("sup", "sub"):
                parts.append(f"<{name}>{_render_inline(child)}</{name}>")
            elif name == "br":
                parts.append("\n")
            elif name == "math":
                parts.append(f"${child.get_text()}$")
            else:
                # <a>, <p>, <span>, etc. — drop the tag, keep its content
                parts.append(_render_inline(child))
    return "".join(parts)


def clean_inline(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = _render_inline(soup)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def heading_level(html: str) -> int:
    m = re.search(r"<h([1-6])", html or "", re.IGNORECASE)
    return int(m.group(1)) if m else 2


def extract_equation(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    math = soup.find("math")
    if math:
        return f"$$\n{math.get_text().strip()}\n$$"
    return clean_inline(html)  # fallback: no <math> tag found


def convert_math_tags(html: str) -> str:
    """Replace every <math>...</math> with $...$ inline text, leaving all
    other structure untouched. <math> is a reserved HTML5/MathML element
    name — a browser tries to natively parse it as MathML, and since
    Datalab's <math> content is plain LaTeX source (not valid MathML), it
    silently renders nothing. clean_inline() already handles this for
    paragraphs/captions/footnotes/references (its _render_inline walks the
    DOM and converts <math> as it goes); this is the same conversion applied
    to table HTML, which otherwise passes through unprocessed and left every
    table cell containing a formula rendering completely blank in the
    browser — found via a real user report on Table 2 / Table A.4 of
    2024A+A...685A..63A, verified: the underlying data was never lost, only
    Stage A's raw HTML passthrough for tables broke it, this function fixes
    that passthrough rather than any Datalab/Marker extraction gap."""
    soup = BeautifulSoup(html or "", "html.parser")
    for math_tag in soup.find_all("math"):
        math_tag.replace_with(f"${math_tag.get_text()}$")
    return str(soup)


def clean_table_html(html: str) -> str:
    return re.sub(r"\s+", " ", convert_math_tags(html)).strip()


def parse_list_items(html: str) -> list[str]:
    """A ListGroup's html is either <ol>/<ul> with <li> items (AAS/A&A style) or
    — verified on MNRAS papers — a single <p> with entries separated by plain
    <br/> tags and no <li> at all. Handle both; <br>-splitting works on the raw
    HTML string rather than the parsed tree so inline markup (<i>, <b>, <math>)
    within each entry survives clean_inline() unchanged."""
    soup = BeautifulSoup(html or "", "html.parser")
    li_tags = soup.find_all("li")
    if li_tags:
        items = [clean_inline(li.decode_contents()) for li in li_tags]
        return [i for i in items if i]

    container = soup.find("p") or soup
    inner_html = container.decode_contents() if hasattr(container, "decode_contents") else str(container)
    raw_items = re.split(r"<br\s*/?>", inner_html, flags=re.IGNORECASE)
    items = [clean_inline(raw) for raw in raw_items]
    return [i for i in items if i]


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

def extract_image_asset(block: dict, doc_id: str, assets_dir) -> str | None:
    """Decode the first (typically only) image in a Figure block's `images`
    field to <assets_dir>/<doc_id>/<block_id_slug>.jpeg. Returns the relative
    path stored in the node's image_asset field, or None if no image present."""
    images = block.get("images") or {}
    if not images:
        return None
    _, b64data = next(iter(images.items()))
    block_id = block.get("id") or ""
    slug = block_id.strip("/").replace("/", "_") or f"block_{block.get('page', 0)}"
    out_dir = assets_dir / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.jpeg"
    try:
        out_path.write_bytes(base64.b64decode(b64data))
    except Exception:
        return None
    return f"{doc_id}/{slug}.jpeg"


# ---------------------------------------------------------------------------
# Section-tree assembly (stack-based, one pass over each Page's flat children)
# ---------------------------------------------------------------------------

def build_tree(doc: dict, doc_id: str, assets_dir=None) -> list[dict]:
    counter = itertools.count(1)
    root_nodes: list[dict] = []
    stack = [{"level": 0, "nodes": root_nodes}]
    pending_caption: dict | None = None

    def next_id() -> str:
        return f"{next(counter):04d}"

    def flush_pending_caption():
        nonlocal pending_caption
        if pending_caption is not None:
            stack[-1]["nodes"].append({
                "node_id": next_id(), "kind": "leaf", "block_type": "paragraph",
                "text": pending_caption["text"], "page": pending_caption["page"],
                "source_block_ids": [pending_caption["block_id"]],
            })
            pending_caption = None

    def walk(children: list[dict]):
        nonlocal pending_caption
        for block in children:
            bt = block.get("block_type")

            if bt in RECURSE_TYPES:
                walk(block.get("children") or [])
                continue
            if bt in DROP_TYPES:
                flush_pending_caption()
                continue

            if bt == "SectionHeader":
                flush_pending_caption()
                level = heading_level(block.get("html") or "")
                while len(stack) > 1 and stack[-1]["level"] >= level:
                    stack.pop()
                node = {"node_id": next_id(), "kind": "container",
                        "title": clean_inline(block.get("html") or ""),
                        "level": level, "nodes": []}
                stack[-1]["nodes"].append(node)
                stack.append({"level": level, "nodes": node["nodes"]})
                continue

            if bt == "Caption":
                flush_pending_caption()  # a caption with no following table/figure
                pending_caption = {
                    "text": clean_inline(block.get("html") or ""),
                    "page": block.get("page"), "block_id": block.get("id"),
                }
                continue

            if bt in ("Table", "Figure"):
                caption_text = caption_block_id = None
                if pending_caption is not None:
                    caption_text = pending_caption["text"]
                    caption_block_id = pending_caption["block_id"]
                    pending_caption = None
                source_ids = ([caption_block_id] if caption_block_id else []) + [block.get("id")]
                if bt == "Table":
                    node = {"node_id": next_id(), "kind": "leaf", "block_type": "table",
                            "text": clean_table_html(block.get("html") or ""),
                            "page": block.get("page"), "source_block_ids": source_ids}
                else:
                    node = {"node_id": next_id(), "kind": "leaf", "block_type": "figure",
                            "text": caption_text or "",
                            "page": block.get("page"), "source_block_ids": source_ids}
                    image_asset = extract_image_asset(block, doc_id, assets_dir) if assets_dir else None
                    if image_asset:
                        node["image_asset"] = image_asset
                if caption_text:
                    node["caption"] = caption_text
                stack[-1]["nodes"].append(node)
                continue

            if bt == "Equation":
                flush_pending_caption()
                stack[-1]["nodes"].append({
                    "node_id": next_id(), "kind": "leaf", "block_type": "equation",
                    "text": extract_equation(block.get("html") or ""),
                    "page": block.get("page"), "source_block_ids": [block.get("id")]})
                continue

            if bt == "Footnote":
                flush_pending_caption()
                text = clean_inline(block.get("html") or "")
                if text:
                    stack[-1]["nodes"].append({
                        "node_id": next_id(), "kind": "leaf", "block_type": "footnote",
                        "text": text, "page": block.get("page"),
                        "source_block_ids": [block.get("id")]})
                continue

            if bt == "ListGroup":
                flush_pending_caption()
                for i, item_text in enumerate(parse_list_items(block.get("html") or "")):
                    stack[-1]["nodes"].append({
                        "node_id": next_id(), "kind": "leaf", "block_type": "reference",
                        "text": item_text, "list_index": i,
                        "page": block.get("page"), "source_block_ids": [block.get("id")]})
                continue

            if bt == "Code":
                flush_pending_caption()
                stack[-1]["nodes"].append({
                    "node_id": next_id(), "kind": "leaf", "block_type": "code",
                    "text": f"```\n{block.get('html') or ''}\n```",
                    "page": block.get("page"), "source_block_ids": [block.get("id")]})
                continue

            if bt == "Text":
                flush_pending_caption()
                text = clean_inline(block.get("html") or "")
                if text:
                    stack[-1]["nodes"].append({
                        "node_id": next_id(), "kind": "leaf", "block_type": "paragraph",
                        "text": text, "page": block.get("page"),
                        "source_block_ids": [block.get("id")]})
                continue

            # Unknown/unobserved block type (e.g. a future Datalab addition, or a
            # type from the *local* marker library that never appears in the
            # hosted API — TextInlineMath, ListItem, Line, Span, TableCell, ...).
            # Defensive no-op: recurse into any children rather than silently
            # dropping content outright.
            flush_pending_caption()
            if block.get("children"):
                walk(block["children"])

    walk(doc.get("children") or [])
    flush_pending_caption()
    return root_nodes


def count_nodes(nodes: list[dict]) -> tuple[int, int]:
    """(total nodes, reference-type leaf nodes) recursively."""
    total = 0
    refs = 0
    for n in nodes:
        total += 1
        if n.get("block_type") == "reference":
            refs += 1
        if n.get("kind") == "container":
            t, r = count_nodes(n["nodes"])
            total += t
            refs += r
    return total, refs


def sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def process_one(settings: Settings, doc_id: str, marker_path) -> dict:
    """Returns a stats dict: {doc_id, node_count, ref_count, page_count, ok}."""
    marker_sha = sha256_of(marker_path)
    data = json.loads(marker_path.read_text(encoding="utf-8"))
    doc = data.get("json")
    if isinstance(doc, str):
        doc = json.loads(doc)

    structure = build_tree(doc, doc_id, settings.assets_dir)
    total, refs = count_nodes(structure)

    out = {
        "doc_id": doc_id,
        "page_count": data.get("page_count"),
        "source_marker_sha256": marker_sha,
        "structure": structure,
    }
    out_path = settings.sections_dir / f"{doc_id}.json"
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out_path)

    return {"doc_id": doc_id, "node_count": total, "ref_count": refs,
            "page_count": data.get("page_count"), "ok": True}


def already_done(settings: Settings, doc_id: str, marker_path) -> bool:
    out_path = settings.sections_dir / f"{doc_id}.json"
    if not out_path.exists():
        return False
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return existing.get("source_marker_sha256") == sha256_of(marker_path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Marker JSON -> section-tree JSON.")
    parser.add_argument("--corpus", metavar="PATH")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", metavar="DOC_ID")
    parser.add_argument("--limit", type=int)
    return parser


def run(settings: Settings, args) -> None:
    papers = settings.corpus.load_papers()
    marker_dir = settings.corpus.marker_dir()

    todo = []
    no_marker = []
    done = 0
    for p in papers:
        doc_id = p["doc_id"]
        if args.only and doc_id != args.only:
            continue
        # load_papers() returns every known entry (papertrail migration Step 9
        # merged load()/load_all_including_stubs() into one adapter function) —
        # has_fulltext is None for a corpus whose adapter doesn't supply it (a
        # minimal adapter, 05_adoption.md), in which case fall through to the
        # marker-file check below rather than silently skipping everything.
        if p.get("has_fulltext") is False:
            continue  # known no-PDF entry — not worth a "no marker JSON" report line
        marker_path = marker_dir / f"{doc_id}.json"
        if not marker_path.exists():
            no_marker.append(doc_id)
            continue
        if not args.force and already_done(settings, doc_id, marker_path):
            done += 1
            continue
        todo.append((doc_id, marker_path))
    if args.limit:
        todo = todo[:args.limit]

    print(f"Papers: {len(papers)} | already built: {done} | "
          f"no marker JSON: {len(no_marker)} | to build: {len(todo)}")
    if no_marker:
        print(f"  (expected — papers with no downloaded PDF: {', '.join(no_marker)})")
    if args.dry_run:
        for doc_id, _ in todo:
            print(f"  [dry-run] {doc_id}")
        return
    if not todo:
        return

    results = []
    for doc_id, marker_path in tqdm(todo, unit="paper", desc="Building sections"):
        try:
            results.append(process_one(settings, doc_id, marker_path))
        except Exception as e:
            tqdm.write(f"  FAILED {doc_id}: {type(e).__name__}: {e}")
            results.append({"doc_id": doc_id, "ok": False, "error": str(e)})

    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    node_counts = [r["node_count"] for r in ok]
    zero_refs = [r["doc_id"] for r in ok if r["ref_count"] == 0]
    big_pages = [(r["doc_id"], r["page_count"]) for r in ok if (r.get("page_count") or 0) > 60]

    print(f"\nBuilt: {len(ok)} | failed: {len(failed)}")
    if node_counts:
        node_counts.sort()
        n = len(node_counts)
        print(f"Nodes per paper — min {node_counts[0]}, median {node_counts[n // 2]}, "
              f"max {node_counts[-1]}")
    if zero_refs:
        print(f"\nPapers with zero reference nodes ({len(zero_refs)}) — "
              f"check for a ListGroup-parsing miss:")
        for d in zero_refs[:20]:
            print(f"  {d}")
    if big_pages:
        print(f"\nPage count > 60 (possible wrong/linked document, eyeball these):")
        for d, pc in big_pages:
            print(f"  {d}: {pc} pages")
    for r in failed:
        print(f"  FAILED {r['doc_id']}: {r.get('error')}")


def main(argv=None) -> None:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings(args.corpus, create_dirs=True)
    run(settings, args)


if __name__ == "__main__":
    main()
