from __future__ import annotations

import argparse
import sqlite3
import zipfile
from pathlib import Path

import pytest
import yaml

from papertrail.config import load_settings
from papertrail.core.renderer import render_node_html
from papertrail.epub import EpubConversionError, convert_epub, is_epub
from papertrail.pipeline import ingest_epub, ingest_pdf
from papertrail.pipeline.sections import build_tree, count_nodes

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
       b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"

CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

EDP_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:opf="http://www.idpf.org/2007/opf" version="2.0">
 <metadata>
  <dc:identifier opf:scheme="doi">10.1051/0004-6361/202300001</dc:identifier>
  <dc:identifier opf:scheme="bibcode">2024A%26A...600A...1X</dc:identifier>
  <dc:title>A&amp;A, 600, A1 (2024): Test paper on aromatic bands</dc:title>
  <dc:creator opf:role="aut">Ann Author, Bob Builder and Cee Writer</dc:creator>
  <dc:date opf:event="publication">2024-05-14</dc:date>
 </metadata>
 <manifest>
  <item id="main" href="paper.html" media-type="application/xhtml+xml"/>
  <item id="F1_html" href="F1.html" media-type="application/xhtml+xml"/>
  <item id="T1_html" href="T1.html" media-type="application/xhtml+xml"/>
  <item id="F1" href="paper-fig1.jpg" media-type="image/jpeg"/>
  <item id="eq1" href="paper-eq1.png" media-type="image/png"/>
 </manifest>
 <spine><itemref idref="main"/><itemref idref="F1_html"/><itemref idref="T1_html"/></spine>
</package>"""

EDP_MAIN = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>A&amp;A</title></head><body>
<div id="top"><div id="head"><span class="idline">A&amp;A, 600, A1 (2024)</span>
<p class="license">Open Access article.</p>
<h2 class="title">Test paper</h2><h3 class="subtitle">On aromatic bands<sup><a href="#FN1">★</a></sup></h3>
<p class="bold">Ann Author<sup>1</sup> <a href="https://orcid.org/0"><img alt="orcid" src="orcid.gif"/></a></p>
<p class="bold" id="abs">Abstract</p>
<p><i>Context</i>. Bands are bright.</p>
<div class="note"><hr class="hr"/><div id="FN1"><sup>★</sup> <p class="ligne">Data at the CDS.</p></div></div>
</div></div>
<h2 class="sec" id="S1">1 Introduction</h2>
<p>The 6.2 µm band (<a href="#R1">Alpha 2020</a>) is strong.</p>
<div id="F1" class="inset"><table><tr><td><a href="F1.html"><img alt="thumbnail" src="paper-fig1_small.jpg"/></a></td>
<td class="img-txt"><a href="F1.html"><span class="bold">Fig. 1 </span></a><p class="ligne">A spectrum of H<sub>2</sub>.</p></td></tr></table></div>
<h3 class="sec2" id="S11">1.1 Method</h3>
<p>We compute the ratio
<span class="img-equation" id="FD1"><img alt="equation" src="paper-eq1.png"/><span class="label-eq">(1)</span></span></p>
<p>where <span class="img-inline"><img alt="equation" src="paper-eq2.png"/></span> is the flux. Artifacts:
<ul class="dash"><li><p>Excess emission at 14.3 µm.</p></li><li><p>Fringes at 10 µm.</p></li></ul></p>
<div id="T1" class="inset"><p class="ligne"><a href="T1.html"><span class="bold">Table 1 </span></a></p>
<p class="ligne">Band widths.</p></div>
<h2 class="sec" id="ref">References</h2><!--biblio--><ul>
<li id="R1">Alpha, A. 2020, ApJ, 1, 2[<a href="http://linker.aanda.org/x/1">See</a>]</li>
<li id="R2">Beta, B. 2021, A&amp;A, 3, 4[<a href="http://linker.aanda.org/x/2">See</a>]</li>
</ul><!--/biblio-->
<hr class="hr"/><div><div id="FN4"><sup>1</sup> <p class="ligne">https://example.org</p></div></div>
<h2 id="figs" class="sec">All Figures</h2>
<div class="inset"><table><tr><td><a href="F1.html"><img alt="thumbnail" src="paper-fig1_small.jpg"/></a></td>
<td class="img-txt"><span class="bold">Fig. 1 </span><p class="ligne">A spectrum of H<sub>2</sub>.</p>
<p class="margin_bot"><a href="#F1">In the text</a></p></td></tr></table></div>
</body></html>"""

EDP_F1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><div id="annex"><h2 class="sec">Fig. 1 </h2>
<div class="img_annex"><img alt="thumbnail" src="paper-fig1.jpg"/></div>
<div class="inset"><p class="ligne">A spectrum of H<sub>2</sub>.</p></div></div></body></html>"""

EDP_T1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><div id="annex"><h2 class="sec">Table 1 </h2>
<p class="ligne">Band widths.</p><div class="tab"><table class="fm" frame="hsides">
<thead><tr><th align="center">Band</th><th align="center">FWHM</th></tr></thead>
<tbody><tr><td align="left">6.2</td><td>34.8</td></tr></tbody></table></div>
<div class="history"><div><p><b>Notes.</b> Widths in cm<sup>−1</sup>.</p></div></div>
</div></body></html>"""


def write_epub(path: Path, files: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER)
        for name, data in files.items():
            zf.writestr(name, data)
    return path


@pytest.fixture
def edp_epub(tmp_path) -> Path:
    return write_epub(tmp_path / "aa00001-24.epub", {
        "OEBPS/content.opf": EDP_OPF,
        "OEBPS/paper.html": EDP_MAIN,
        "OEBPS/F1.html": EDP_F1,
        "OEBPS/T1.html": EDP_T1,
        "OEBPS/paper-fig1.jpg": JPEG,
        "OEBPS/paper-eq1.png": PNG,
    })


def blocks_of(result: dict) -> list[dict]:
    return result["json"]["children"][0]["children"]


def test_edp_epub_converts_to_marker_shaped_blocks(edp_epub):
    result = convert_epub(edp_epub)
    blocks = blocks_of(result)
    types = [b["block_type"] for b in blocks]

    assert result["json"]["block_type"] == "Document"
    assert result["json"]["children"][0]["block_type"] == "Page"
    assert all(b["id"].startswith("/epub/") and b["page"] is None for b in blocks)
    headers = [b["html"] for b in blocks if b["block_type"] == "SectionHeader"]
    assert headers[:3] == ["<h1>Test paper</h1>", "<h1>On aromatic bands<sup>★</sup></h1>",
                           "<h2>Abstract</h2>"]
    assert "<h3>1.1 Method</h3>" in headers and "<h2>Notes</h2>" in headers
    # The "All Figures" gallery and its duplicate inset are not content.
    assert not any("All Figures" in h for h in headers)
    assert types.count("Figure") == 1 and types.count("Table") == 1
    # Publisher chrome is gone.
    html = " ".join(b["html"] for b in blocks)
    assert "Open Access" not in html and "orcid" not in html and "See" not in html
    assert "In the text" not in html


def test_edp_epub_media_equations_lists_and_references(edp_epub):
    blocks = blocks_of(convert_epub(edp_epub))
    by_type = lambda t: [b for b in blocks if b["block_type"] == t]  # noqa: E731

    fig_i = next(i for i, b in enumerate(blocks) if b["block_type"] == "Figure")
    assert blocks[fig_i - 1]["block_type"] == "Caption"
    assert blocks[fig_i - 1]["html"] == "<p><b>Fig. 1.</b> A spectrum of H<sub>2</sub>.</p>"
    assert list(blocks[fig_i]["images"]) == ["paper-fig1.jpg"]

    table = by_type("Table")[0]
    assert "<th>FWHM</th>" in table["html"] and "align" not in table["html"]

    eq = by_type("Equation")[0]
    assert eq["html"] == "<p>(1)</p>" and list(eq["images"]) == ["paper-eq1.png"]
    texts = [b["html"] for b in by_type("Text")]
    assert "<p>We compute the ratio</p>" in texts
    assert any("where [equation] is the flux" in t for t in texts)
    # A <ul> nested in a <p> becomes one prose block per item, not references.
    assert "<p>Excess emission at 14.3 µm.</p>" in texts
    assert "<p>Fringes at 10 µm.</p>" in texts
    assert len(by_type("ListGroup")) == 1
    assert by_type("ListGroup")[0]["html"] == (
        "<ul><li>Alpha, A. 2020, ApJ, 1, 2</li><li>Beta, B. 2021, A&amp;A, 3, 4</li></ul>")
    notes = [b["html"] for b in by_type("Footnote")]
    assert "<p><sup>★</sup> Data at the CDS.</p>" in notes
    assert "<p><b>Notes.</b> Widths in cm<sup>−1</sup>.</p>" in notes
    assert "<p><sup>1</sup> https://example.org</p>" in notes


def test_epub_section_tree_matches_pdf_node_shapes(edp_epub, tmp_path):
    tree = build_tree(convert_epub(edp_epub)["json"], "doc", tmp_path / "assets")
    total, refs = count_nodes(tree)
    leaves = []

    def walk(nodes, path):
        for n in nodes:
            if n["kind"] == "container":
                walk(n["nodes"], path + [n["title"]])
            else:
                leaves.append((path, n))
    walk(tree, [])

    assert refs == 2
    ref_paths = {tuple(p) for p, n in leaves if n["block_type"] == "reference"}
    assert ref_paths == {("On aromatic bands<sup>★</sup>", "References")}
    figure = next(n for _, n in leaves if n["block_type"] == "figure")
    assert figure["caption"] == "**Fig. 1.** A spectrum of H<sub>2</sub>."
    assert figure["image_asset"] == "doc/epub_Figure_0.jpeg"
    assert (tmp_path / "assets" / figure["image_asset"]).read_bytes() == JPEG
    equation = next(n for _, n in leaves if n["block_type"] == "equation")
    assert equation["text"] == "[equation (1)]"
    assert equation["image_asset"] == "doc/epub_Equation_0.png"
    assert (tmp_path / "assets" / equation["image_asset"]).read_bytes() == PNG
    html = render_node_html(equation, "assets")
    assert '<img src="assets/doc/epub_Equation_0.png"' in html and "(1)" in html
    # Every leaf has the keys a PDF-derived leaf has.
    for _, n in leaves:
        assert {"node_id", "kind", "block_type", "text", "page", "source_block_ids"} <= set(n)


def test_generic_epub_without_publisher_conventions(tmp_path):
    opf = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"
     xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0"><metadata>
     <dc:title>Plain Book</dc:title><dc:creator>A. Writer</dc:creator><dc:creator>B. Writer</dc:creator>
     <dc:date>2019-01-01</dc:date></metadata>
     <manifest><item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
     <item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
     <item id="img" href="images/p.png" media-type="image/png"/></manifest>
     <spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>"""
    ch1 = """<html xmlns="http://www.w3.org/1999/xhtml"><body><section>
     <h1>Chapter 1</h1><p>First <em>idea</em>.</p>
     <figure><img src="../images/p.png"/><figcaption>Figure 1. A plot.</figcaption></figure>
     <table><caption>Table 1. Numbers.</caption><tr><td>1</td></tr></table>
     </section></body></html>"""
    ch2 = """<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Bibliography</h1>
     <ol><li>Smith 2001</li><li>Jones 2002</li></ol></body></html>"""
    epub = write_epub(tmp_path / "book.epub", {
        "OEBPS/content.opf": opf, "OEBPS/text/ch1.xhtml": ch1,
        "OEBPS/text/ch2.xhtml": ch2, "OEBPS/images/p.png": PNG})

    result = convert_epub(epub)
    blocks = blocks_of(result)
    assert [b["block_type"] for b in blocks] == [
        "SectionHeader", "Text", "Caption", "Figure", "Caption", "Table",
        "SectionHeader", "ListGroup"]
    assert list(blocks[3]["images"]) == ["p.png"]
    assert result["epub_metadata"]["authors"] == ["A. Writer", "B. Writer"]
    assert result["epub_metadata"]["year"] == 2019
    tree = build_tree(result["json"], "book", None)
    assert count_nodes(tree)[1] == 2


def test_non_epub_is_rejected(tmp_path):
    bogus = tmp_path / "x.epub"
    bogus.write_bytes(b"%PDF-1.7")
    assert not is_epub(bogus)
    with pytest.raises(EpubConversionError):
        convert_epub(bogus)
    corpus = tmp_path / "corpus"
    ingest_pdf.initialize_corpus(corpus, bogus)
    settings = load_settings(corpus, create_dirs=True)
    with pytest.raises(ValueError, match="not a readable EPUB"):
        ingest_epub.run(settings, _args(bogus, corpus))


def _args(epub, corpus, **kw):
    base = dict(epub=epub, corpus=str(corpus), doc_id=None, title=None, authors=None,
                year=None, journal=None, fulltext_source="publisher_epub", force=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_ingest_epub_end_to_end_reuse_and_metadata(edp_epub, tmp_path):
    corpus = tmp_path / "corpus"
    ingest_pdf.initialize_corpus(corpus, edp_epub)
    settings = load_settings(corpus, create_dirs=True)

    first = ingest_epub.run(settings, _args(edp_epub, corpus, doc_id="Test2024", year=2023))
    second = ingest_epub.run(settings, _args(edp_epub, corpus, doc_id="Test2024"))

    assert not first["reused"] and second["reused"]
    assert first["ref_count"] == 2
    docs = yaml.safe_load((corpus / "documents.yaml").read_text(encoding="utf-8"))["documents"]
    entry = docs[0]
    assert entry["title"] == "Test paper on aromatic bands"
    assert entry["authors"] == ["Ann Author", "Bob Builder", "Cee Writer"]
    assert entry["year"] == 2023  # command line beats the OPF date
    assert entry["journal"] == "A&A"
    assert entry["fulltext_source"] == "publisher_epub"
    assert entry["extra"] == {"doi": "10.1051/0004-6361/202300001",
                              "bibcode": "2024A&A...600A...1X"}
    with sqlite3.connect(settings.sqlite_path) as db:
        hits = db.execute("SELECT block_type, page FROM nodes WHERE doc_id='Test2024' "
                          "AND block_type='reference'").fetchall()
    assert hits == [("reference", None), ("reference", None)]
    manifest = (settings.data_dir / "ingest_manifest.json").read_text(encoding="utf-8")
    assert '"source_format": "epub"' in manifest
