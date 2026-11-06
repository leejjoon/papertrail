"""EPUB -> Datalab/Marker-shaped JSON, so EPUB papers reuse the PDF pipeline.

The output mimics what `datalab.convert_pdf` returns: a poll-response dict
whose `json` is a Document -> Page -> leaf-block tree with Marker block types
(SectionHeader, Text, ListGroup, Table, Figure, Caption, Equation, Footnote)
and Marker-style `html`. `pipeline.sections.build_tree` therefore turns it into
the same section-tree node shapes as a converted PDF, and the index, web reader
and MCP server need no EPUB-specific code.

Differences from a PDF conversion, all deliberate:
  - An EPUB has no pages or geometry. Everything sits in one synthetic Page,
    blocks carry no bbox, and leaf `page` is None. With no bbox the
    paragraph-continuation heuristic never fires, which is right: EPUB
    paragraphs are never split by layout.
  - Block ids are "/epub/<Type>/<n>"; `anchor` keeps the XHTML id (S21, F3,
    R61, FN6, ...) so a node can be traced back to the publisher's anchor.
  - Publisher equations that ship only as images (EDP Sciences / A&A) become
    Equation blocks carrying the image in `images`; the section builder stores
    it as `image_asset`. Inline equation images have no text form and are
    replaced by "[equation]".

Tuned on EDP Sciences (A&A) EPUBs, whose main XHTML file inlines figure and
table thumbnails that link to per-item pages (F1.html, T1.html) holding the
full image or table. Generic XHTML (h1-h6, p, ul/ol, table, figure/img) is
handled too, walking every spine document in order.
"""
from __future__ import annotations

import base64
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree as ET

import warnings

from bs4 import BeautifulSoup as _BeautifulSoup
from bs4 import Comment, MarkupResemblesLocatorWarning, NavigableString, Tag


def BeautifulSoup(markup, features="html.parser"):  # noqa: N802 - drop-in wrapper
    # Fragments such as a footnote holding only a URL trip bs4's
    # "looks like a URL" warning; they are markup, not locators.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        return _BeautifulSoup(markup, features)

CONVERTER_VERSION = "papertrail-epub/1"

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_XHTML_TYPES = {"application/xhtml+xml", "text/html"}

# EDP Sciences chrome that is metadata, navigation or decoration, not content.
_DROP_SELECTORS = ("span.idline", "span.section", "span.block", "span.art_type",
                   "p.license", "p.margin_bot")
_INLINE_TAGS = {"a", "abbr", "b", "big", "br", "cite", "code", "em", "i", "img", "math",
                "q", "s", "small", "span", "strong", "sub", "sup", "u", "var"}
_SKIP_TAGS = {"script", "style", "head", "title", "meta", "link", "hr", "nav"}
_REFERENCE_HEADING = re.compile(r"^\s*(references|bibliography|literature cited)\s*$", re.I)
_GALLERY_IDS = {"tabs", "figs"}  # EDP's "All Tables" / "All Figures" duplicates
_MEDIA_PAGE = re.compile(r"^[FT][A-Z]?\d+\.x?html?$", re.I)


class EpubConversionError(RuntimeError):
    """Raised when a file is not a readable EPUB."""


# ---------------------------------------------------------------------------
# Package (OPF) reading
# ---------------------------------------------------------------------------

@dataclass
class EpubPackage:
    zf: zipfile.ZipFile
    opf_path: str
    metadata: dict
    manifest: dict          # href (zip path) -> media-type
    spine: list[str]        # zip paths of spine documents, in reading order

    def read(self, path: str) -> bytes:
        try:
            return self.zf.read(path)
        except KeyError as exc:
            raise EpubConversionError(f"EPUB is missing {path}") from exc

    def soup(self, path: str) -> BeautifulSoup:
        return BeautifulSoup(self.read(path).decode("utf-8", errors="replace"), "html.parser")


def _text(el) -> str | None:
    if el is None or el.text is None:
        return None
    return re.sub(r"\s+", " ", el.text).strip() or None


def open_package(zf: zipfile.ZipFile) -> EpubPackage:
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
    except KeyError as exc:
        raise EpubConversionError("not an EPUB: META-INF/container.xml is missing") from exc
    rootfile = container.find(".//c:rootfile", _NS)
    if rootfile is None or not rootfile.get("full-path"):
        raise EpubConversionError("EPUB container.xml names no rootfile")
    opf_path = rootfile.get("full-path")
    opf = ET.fromstring(zf.read(opf_path))
    base = posixpath.dirname(opf_path)

    def resolve(href: str) -> str:
        return posixpath.normpath(posixpath.join(base, unquote(href)))

    items = {}
    manifest = {}
    for item in opf.findall("opf:manifest/opf:item", _NS):
        path = resolve(item.get("href", ""))
        items[item.get("id")] = (path, item.get("media-type", ""))
        manifest[path] = item.get("media-type", "")
    spine = [items[ref.get("idref")][0]
             for ref in opf.findall("opf:spine/opf:itemref", _NS)
             if ref.get("idref") in items and items[ref.get("idref")][1] in _XHTML_TYPES]

    md = opf.find("opf:metadata", _NS)
    meta: dict = {}
    if md is not None:
        meta["title"] = _text(md.find("dc:title", _NS))
        creators = [_text(c) for c in md.findall("dc:creator", _NS)]
        meta["authors"] = _split_creators([c for c in creators if c])
        for ident in md.findall("dc:identifier", _NS):
            scheme = (ident.get(f"{{{_NS['opf']}}}scheme") or "").lower()
            value = _text(ident)
            if value and scheme in ("doi", "bibcode", "isbn") and scheme not in meta:
                meta[scheme] = unquote(value)
            elif value and value.startswith("10.") and "doi" not in meta:
                meta["doi"] = value
        dates = {(d.get(f"{{{_NS['opf']}}}event") or ""): _text(d)
                 for d in md.findall("dc:date", _NS)}
        date = dates.get("publication") or next((d for d in dates.values() if d), None)
        if date and re.match(r"\d{4}", date):
            meta["year"] = int(date[:4])
        meta["publisher"] = _text(md.find("dc:publisher", _NS))
        meta["description"] = _text(md.find("dc:description", _NS))
    return EpubPackage(zf, opf_path, meta, manifest, spine)


def _split_creators(creators: list[str]) -> list[str]:
    """EDP packs every author into one dc:creator as "A, B, C and D"."""
    if len(creators) != 1:
        return creators
    parts = re.split(r",\s*|\s+and\s+", creators[0])
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Block emission
# ---------------------------------------------------------------------------

@dataclass
class _Emitter:
    pkg: EpubPackage
    blocks: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    in_references: bool = False
    after_references: bool = False
    notes_header_done: bool = False
    consumed: set = field(default_factory=set)  # spine docs inlined as media pages

    def emit(self, block_type: str, html: str, *, anchor: str | None = None,
             images: dict | None = None) -> dict:
        n = self.counts.get(block_type, 0)
        self.counts[block_type] = n + 1
        block = {"id": f"/epub/{block_type}/{n}", "block_type": block_type,
                 "html": html, "page": None, "images": images or None}
        if anchor:
            block["anchor"] = anchor
        self.blocks.append(block)
        return block

    def image_payload(self, doc_path: str, src: str | None) -> dict | None:
        if not src:
            return None
        path = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path),
                                                 unquote(urldefrag(src)[0])))
        try:
            data = self.pkg.zf.read(path)
        except KeyError:
            return None
        return {posixpath.basename(path): base64.b64encode(data).decode("ascii")}


def _classes(tag: Tag) -> set[str]:
    return set(tag.get("class") or [])


def _strip_chrome(root: Tag) -> None:
    """Remove publisher decoration in place, before any block is emitted."""
    for c in root.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for a in root.find_all("a"):
        href = a.get("href") or ""
        img = a.find("img")
        # ORCID badges and A&A's "[See]" reference linker.
        if (img is not None and (img.get("alt") or "").lower() == "orcid") or \
                "linker.aanda.org" in href:
            prev, nxt = a.previous_sibling, a.next_sibling
            if isinstance(prev, NavigableString) and prev.rstrip().endswith("[") \
                    and isinstance(nxt, NavigableString) and nxt.lstrip().startswith("]"):
                prev.replace_with(prev.rstrip()[:-1].rstrip())
                nxt.replace_with(nxt.lstrip()[1:])
            a.decompose()
    for span in root.find_all("span", class_="img-inline"):
        span.replace_with("[equation]")
    # EDP repeats every figure/table in an end-of-article gallery whose boxes
    # carry an "In the text" back-link (p.margin_bot); drop those duplicates.
    for back in root.select("p.margin_bot"):
        box = back.find_parent("div", class_="inset")
        if box is not None:
            box.decompose()
    for el in root.select(", ".join(_DROP_SELECTORS)):
        el.decompose()


def _inner(tag: Tag) -> str:
    """Inner HTML with link wrappers and empty anchors removed (text kept)."""
    frag = BeautifulSoup(tag.decode_contents(), "html.parser")
    for a in frag.find_all("a"):
        a.unwrap()
    for span in frag.find_all("span"):
        span.unwrap()
    for inner_block in frag.find_all(["p", "div"]):  # e.g. <li><p>..</p></li>
        inner_block.insert_before(" ")
        inner_block.unwrap()
    for img in frag.find_all("img"):
        img.decompose()
    for el in frag.find_all(True):
        el.attrs = {}
    return re.sub(r"\s+", " ", str(frag)).strip()


def _has_text(html: str) -> bool:
    return bool(BeautifulSoup(html, "html.parser").get_text(strip=True))


def _heading_level(tag: Tag) -> int:
    if _classes(tag) & {"title", "subtitle"}:
        return 1
    return int(tag.name[1])


class _Walker:
    def __init__(self, emitter: _Emitter, doc_path: str):
        self.e = emitter
        self.doc_path = doc_path
        self.inline: list = []
        self.stop = False

    # -- inline run buffering (loose text and inline tags between blocks) --
    def flush(self):
        if not self.inline:
            return
        html = "".join(str(x) for x in self.inline)
        self.inline = []
        frag = BeautifulSoup(f"<p>{html}</p>", "html.parser").p
        inner = _inner(frag)
        if _has_text(inner):
            self.e.emit("Text", f"<p>{inner}</p>")

    def text_block(self, tag: Tag, block_type: str = "Text"):
        inner = _inner(tag)
        if _has_text(inner):
            self.e.emit(block_type, f"<p>{inner}</p>", anchor=tag.get("id"))

    def walk(self, node: Tag):
        for child in list(node.children):
            if self.stop:
                return
            if isinstance(child, NavigableString):
                if not isinstance(child, Comment):
                    self.inline.append(child)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in _SKIP_TAGS:
                continue
            if name in _INLINE_TAGS and not child.find(["p", "div", "table", "ul", "ol"]) \
                    and not (_classes(child) & {"img-equation"}):
                self.inline.append(child)
                continue
            self.flush()
            self.block(child, name)
        if node.name in ("div", "section", "body", "article", "td", "li", "blockquote"):
            self.flush()

    def block(self, tag: Tag, name: str):
        cls = _classes(tag)
        if re.fullmatch(r"h[1-6]", name):
            if tag.get("id") in _GALLERY_IDS:
                self.stop = True
                return
            title = _inner(tag)
            if not _has_text(title):
                return
            self.e.in_references = bool(_REFERENCE_HEADING.match(
                BeautifulSoup(title, "html.parser").get_text()))
            if self.e.in_references:
                self.e.after_references = True
            level = _heading_level(tag)
            self.e.emit("SectionHeader", f"<h{level}>{title}</h{level}>", anchor=tag.get("id"))
            return

        if name == "p":
            if tag.get("id") == "abs" or (cls == {"bold"} and _inner(tag).lower() == "abstract"):
                self.e.emit("SectionHeader", "<h2>Abstract</h2>", anchor=tag.get("id"))
                return
            self.paragraph(tag)
            return

        if name in ("ul", "ol"):
            self.list_block(tag)
            return

        if name == "div" and "inset" in cls:
            self.media_inset(tag)
            return

        if name == "div" and "note" in cls:
            self.footnotes(tag)
            return

        if name == "div" and (tag.get("id") or "").startswith("FN"):
            self.footnote(tag)
            return

        if name == "figure":
            self.figure(tag)
            return

        if name == "table":
            caption = tag.find("caption")
            if caption is not None:
                self.e.emit("Caption", f"<p>{_inner(caption)}</p>")
                caption.decompose()
            self.e.emit("Table", _table_html(tag), anchor=tag.get("id"))
            return

        if name == "img":
            images = self.e.image_payload(self.doc_path, tag.get("src"))
            if images:
                self.e.emit("Figure", "", images=images, anchor=tag.get("id"))
            return

        if name == "math":
            self.e.emit("Equation", f'<p><math display="block">{tag.get_text()}</math></p>')
            return

        # Containers (div, section, blockquote, ...): recurse.
        self.walk(tag)

    def paragraph(self, tag: Tag):
        """A <p>, split around display-equation images and around block
        elements nested in it (EDP nests <ul> inside <p>)."""
        if not tag.find(["ul", "ol", "table", "div", "p"]) and \
                not tag.find("span", class_="img-equation"):
            self.text_block(tag)
            return
        run = []
        for child in list(tag.children):
            if isinstance(child, Tag) and "img-equation" in _classes(child):
                self._emit_run(run)
                run = []
                self.display_equation(child)
            elif isinstance(child, Tag) and child.name in ("ul", "ol", "table", "div", "p"):
                self._emit_run(run)
                run = []
                self.block(child, child.name)
            else:
                run.append(child)
        self._emit_run(run)

    def _emit_run(self, run):
        if run:
            frag = BeautifulSoup("<p>" + "".join(str(x) for x in run) + "</p>", "html.parser").p
            self.text_block(frag)

    def display_equation(self, span: Tag):
        img = span.find("img")
        label_el = span.find(class_="label-eq")
        label = label_el.get_text(strip=True) if label_el else ""
        images = self.e.image_payload(self.doc_path, img.get("src") if img else None)
        self.e.emit("Equation", f"<p>{label}</p>" if label else "<p></p>",
                    images=images, anchor=span.get("id"))

    def list_block(self, tag: Tag):
        items = tag.find_all("li", recursive=False)
        if self.e.in_references:
            lis = "".join(f"<li>{_inner(li)}</li>" for li in items if _has_text(_inner(li)))
            if lis:
                self.e.emit("ListGroup", f"<{tag.name}>{lis}</{tag.name}>", anchor=tag.get("id"))
            return
        # Ordinary bullet/numbered lists are prose, not bibliography: one Text
        # block per item (a Marker ListGroup would be indexed as references).
        for li in items:
            self.text_block(li)

    def footnotes(self, tag: Tag):
        for fn in tag.find_all("div", id=re.compile(r"^FN")):
            self.footnote(fn)

    def footnote(self, tag: Tag):
        if self.e.after_references and not self.e.notes_header_done:
            # EDP collects body footnotes after the bibliography; give them
            # their own section instead of filing them under References.
            self.e.notes_header_done = True
            self.e.in_references = False
            self.e.emit("SectionHeader", "<h2>Notes</h2>")
        sup = tag.find("sup", recursive=False)
        mark = sup.get_text(strip=True) if sup else ""
        if sup:
            sup.extract()
        body = " ".join(_inner(p) for p in tag.find_all("p")) or _inner(tag)
        if _has_text(body):
            prefix = f"<sup>{mark}</sup> " if mark else ""
            self.e.emit("Footnote", f"<p>{prefix}{body}</p>", anchor=tag.get("id"))

    # -- figures and tables --
    def media_inset(self, tag: Tag):
        """EDP thumbnail box: label + caption here, full item on a linked page."""
        link = next((a.get("href") for a in tag.find_all("a")
                     if _MEDIA_PAGE.match(posixpath.basename(urldefrag(a.get("href") or "")[0]))),
                    None)
        label_el = tag.find("span", class_="bold")
        label = label_el.get_text(" ", strip=True) if label_el else ""
        caption_ps = [p for p in tag.find_all("p", class_="ligne") if p.get_text(strip=True)
                      and p.get_text(strip=True) != label]
        caption = " ".join(_inner(p) for p in caption_ps)
        caption_html = _caption_html(label, caption)
        target = None
        if link:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(self.doc_path),
                                                       unquote(urldefrag(link)[0])))
        if target and target in self.e.pkg.manifest:
            self.e.consumed.add(target)
            self.media_page(target, caption_html, anchor=tag.get("id"))
            return
        # No linked page: fall back to what the inset itself holds.
        table = tag.find("table") if tag.find("th") else None
        if caption_html:
            self.e.emit("Caption", caption_html)
        if table is not None:
            self.e.emit("Table", _table_html(table), anchor=tag.get("id"))
        else:
            img = tag.find("img")
            images = self.e.image_payload(self.doc_path, img.get("src") if img else None)
            self.e.emit("Figure", "", images=images, anchor=tag.get("id"))

    def media_page(self, path: str, caption_html: str, anchor: str | None):
        soup = self.e.pkg.soup(path)
        annex = soup.find(id="annex") or soup.body or soup
        table = annex.find("table")
        if caption_html:
            self.e.emit("Caption", caption_html)
        if table is not None:
            self.e.emit("Table", _table_html(table), anchor=anchor)
            notes = annex.find(class_="history")
            if notes is not None and notes.get_text(strip=True):
                inner = " ".join(_inner(p) for p in notes.find_all("p")) or _inner(notes)
                self.e.emit("Footnote", f"<p>{inner}</p>")
            return
        img = annex.find("img")
        images = self.e.image_payload(path, img.get("src") if img else None)
        self.e.emit("Figure", "", images=images, anchor=anchor)

    def figure(self, tag: Tag):
        cap = tag.find("figcaption")
        if cap is not None:
            self.e.emit("Caption", f"<p>{_inner(cap)}</p>")
        table = tag.find("table")
        if table is not None:
            self.e.emit("Table", _table_html(table), anchor=tag.get("id"))
            return
        img = tag.find("img")
        images = self.e.image_payload(self.doc_path, img.get("src") if img else None)
        self.e.emit("Figure", "", images=images, anchor=tag.get("id"))


def _caption_html(label: str, caption: str) -> str:
    label = label.strip()
    if label and not label.endswith((".", ":")):
        label += "."
    text = f"<b>{label}</b> {caption}".strip() if label else caption
    return f"<p>{text}</p>" if text else ""


def _table_html(table: Tag) -> str:
    frag = BeautifulSoup(str(table), "html.parser")
    for a in frag.find_all("a"):
        a.unwrap()
    for span in frag.find_all("span"):
        span.unwrap()
    for el in frag.find_all(True):
        el.attrs = {k: v for k, v in el.attrs.items() if k in ("colspan", "rowspan")}
    return str(frag)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def is_epub(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.read("mimetype").strip() == b"application/epub+zip"
    except (zipfile.BadZipFile, KeyError, OSError):
        return False


def convert_epub(epub_path: Path) -> dict:
    """Convert one EPUB to a Datalab-poll-response-shaped dict (see module doc)."""
    try:
        zf = zipfile.ZipFile(epub_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise EpubConversionError(f"not a readable EPUB: {epub_path}") from exc
    with zf:
        pkg = open_package(zf)
        if not pkg.spine:
            raise EpubConversionError("EPUB spine lists no XHTML documents")
        emitter = _Emitter(pkg)
        # Media pages (F1.html, T1.html) are spine items too; inline them where
        # the main text references them, and walk only what is left.
        for path in pkg.spine:
            if path in emitter.consumed:
                continue
            soup = pkg.soup(path)
            body = soup.body or soup
            _strip_chrome(body)
            walker = _Walker(emitter, path)
            walker.walk(body)
            walker.flush()

    page = {"id": "/epub/Page/0", "block_type": "Page", "page": None,
            "bbox": None, "children": emitter.blocks}
    document = {"block_type": "Document", "children": [page],
                "metadata": {"source_format": "epub", "converter": CONVERTER_VERSION}}
    return {
        "status": "complete",
        "success": True,
        "json": document,
        "page_count": None,
        "source_format": "epub",
        "converter": CONVERTER_VERSION,
        "epub_metadata": pkg.metadata,
    }
