"""FastAPI app factory for the knowledge base web UI.

Adapted from multi_doc_navigator/app/main.py's route shapes, extended with
search, chat (SSE), conversations, and an answers store — see
docs/spec/03_webapp.md for the full endpoint table and rationale (notably: no
cross-tab push, per that doc's decision — `publish_answer`/`POST /api/answers`
just return a stable `{id, url}`).

`create_app(settings)` builds everything inside the factory (registry, store,
routes) rather than at module import — the papertrail migration's governing
rule that nothing computes state at import time, since a module-level
`REGISTRY = CorpusRegistry()` (what this replaced) opens sqlite the moment
anything imports this module, before a corpus has even been chosen.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, load_settings
from ..core.citations import InvalidCitationsError, validate_and_enrich
from ..core.nodes import build_toc_outline
from ..core.registry import CorpusRegistry, section_payload
from ..core.search import search_nodes
from ..core.store import Store
from .chat import stream_chat

logging.basicConfig(level=logging.INFO)

# Interim: index.html still lives in webapp/static/ (repo-layout-relative),
# not yet inside the package — moving it in is papertrail migration Step 10
# (needs package-data declarations + importlib.resources, done together with
# the asset_prefix fix). This path only works from within this repo checkout.
_STATIC_DIR = Path(__file__).resolve().parents[3] / "webapp" / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    registry = CorpusRegistry(settings)
    store = Store(settings)

    app = FastAPI(title="WASP-12b Knowledge Base")
    app.state.settings = settings
    app.state.registry = registry
    app.state.store = store

    def _doc_or_404(doc_id: str):
        try:
            return registry.get_doc(doc_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown doc_id: {doc_id}")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "corpus": "wasp12b",
                "doc_count": len(registry.all_doc_ids())}

    @app.post("/api/reload")
    def reload_registry():
        """Drop all caches and re-read kb.sqlite — call after the pipeline
        adds or regenerates papers, no process restart needed."""
        registry.reload()
        return {"status": "ok", "doc_count": len(registry.all_doc_ids())}

    @app.get("/api/documents")
    def documents():
        return registry.doc_tree

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: str):
        doc = _doc_or_404(doc_id)
        # reference_label/journal/external_links — same fields
        # validate_and_enrich() adds to a citation (see citations.py) — so
        # the sticky doc-header the frontend renders from this payload
        # matches the reference-list format exactly.
        header = {"reference_label": registry.style.reference_label(doc.ref()),
                  "journal": doc.journal,
                  "external_links": [{"label": l.label, "url": l.url}
                                     for l in registry.style.external_links(doc.ref())]}
        if not doc.has_fulltext:
            return {"doc_id": doc.doc_id, "doc_name": doc.doc_name, "label": doc.label,
                    "has_fulltext": False, "tree": [], "html": "", **header}
        return {"doc_id": doc.doc_id, "doc_name": doc.doc_name, "label": doc.label,
                "has_fulltext": True, "tree": build_toc_outline(doc.tree["structure"]),
                "html": doc.html, **header}

    @app.get("/api/documents/{doc_id}/sections/{node_id}")
    def section(doc_id: str, node_id: str):
        doc = _doc_or_404(doc_id)
        if node_id not in doc.node_by_id:
            raise HTTPException(status_code=404, detail=f"unknown node_id: {node_id}")
        return section_payload(doc, node_id)

    @app.get("/api/search")
    def search(q: str = Query(..., min_length=1), limit: int = 20):
        hits = search_nodes(settings, q, limit=limit)
        return {"query": q, "hits": [
            {"doc_id": h["doc_id"], "node_id": h["node_id"], "block_type": h["block_type"],
             "title": h["title"], "year": h["year"], "snippet": h["snip"]}
            for h in hits]}

    @app.get("/api/wiki/{page:path}")
    def wiki_page(page: str):
        name = page.removesuffix(".md")
        path = (settings.wiki_dir / f"{name}.md").resolve()
        if not path.is_relative_to(settings.wiki_dir.resolve()) or not path.exists():
            pages = sorted(str(p.relative_to(settings.wiki_dir))[:-3]
                           for p in settings.wiki_dir.rglob("*.md"))
            raise HTTPException(status_code=404, detail={"error": f"page not found: {page}",
                                                           "available": pages})
        return {"page": name, "markdown": path.read_text(encoding="utf-8")}

    # --- conversations -----------------------------------------------------

    @app.post("/api/conversations")
    async def create_conversation(request: Request):
        body = await request.json()
        name = body.get("name") or "New conversation"
        cid = store.create_conversation(name)
        return {"id": cid, "name": name}

    @app.get("/api/conversations")
    def list_conversations():
        return store.list_conversations()

    @app.get("/api/conversations/{conversation_id}/messages")
    def conversation_messages(conversation_id: int):
        return store.list_messages(conversation_id)

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(conversation_id: int, request: Request):
        body = await request.json()
        store.rename_conversation(conversation_id, body.get("name", ""))
        return {"status": "ok"}

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: int):
        store.delete_conversation(conversation_id)
        return {"status": "ok"}

    # --- chat (SSE) ----------------------------------------------------------

    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        conversation_id = body.get("conversation_id")
        question = body.get("question", "")
        if not conversation_id or not question:
            raise HTTPException(status_code=400, detail="conversation_id and question are required")

        def gen():
            for event in stream_chat(settings, registry, store, conversation_id, question):
                yield _sse(event)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --- answers -------------------------------------------------------------

    @app.post("/api/answers")
    async def create_answer(request: Request, strict: bool = Query(default=False)):
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        title = payload.pop("title", None) if isinstance(payload, dict) else None
        try:
            enriched = validate_and_enrich(payload, registry.docs, registry.style, strict=strict)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except InvalidCitationsError as exc:
            return JSONResponse(status_code=422,
                                content={"detail": "invalid citations", "offending": exc.offending})
        answer_id = store.create_answer("agent_adhoc", enriched, title=title)
        return {"id": answer_id, "url": f"/#answer={answer_id}", **enriched}

    @app.get("/api/answers/{answer_id}")
    def get_answer(answer_id: str):
        answer = store.get_answer(answer_id)
        if answer is None:
            raise HTTPException(status_code=404, detail=f"unknown answer id: {answer_id}")
        return answer

    @app.get("/api/answers")
    def list_answers(kind: str | None = None):
        return store.list_answers(kind=kind)

    @app.get("/")
    def index():
        return FileResponse(_STATIC_DIR / "index.html")

    # Figure images: /assets/<doc_id>/<file>. Mounted last so it never
    # shadows the API routes above.
    if settings.assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=settings.assets_dir), name="assets")

    return app
