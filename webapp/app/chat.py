"""Chat RAG flow: condensed history -> FTS5 search (+ optional query rewrite on
a thin result set) -> CHAT_MODEL answer -> validate_and_enrich -> persist.

`stream_chat()` is a generator so main.py's SSE endpoint can forward each step
as it happens (kasi_papers_analysis's `ask` tool + spherex-chatbot's SSE step
pattern, combined — see docs/spec/03_webapp.md). The final yielded item is
always `{"event": "done", ...enriched_payload}`; every other item is
`{"event": "step", "step": ..., "status": "start"|"done"}`.
"""
from __future__ import annotations

import json
import re
from typing import Iterator

from openai import OpenAI

from . import config, store
from .answer import InvalidCitationsError, find_uncited_author_year_mentions, validate_and_enrich
from .registry import CorpusRegistry
from .search import search_nodes

_client = OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY)

SYSTEM_PROMPT = (
    "You answer questions about a knowledge base of papers mentioning the "
    "exoplanet WASP-12b. Answer ONLY from the excerpts provided below — each "
    "labeled with its citable [n], doc_id, and node_id.\n\n"
    "CITATION RULE — every single claim needs an inline [n] marker, with NO "
    "exceptions: if you name a study by author/year in your prose (e.g. "
    "'Madhusudhan et al. (2011)'), that exact sentence must ALSO carry its "
    "[n] marker for the excerpt it came from. Never write an author-year "
    "mention as a substitute for [n] — always attach both.\n"
    "  BAD:  'Madhusudhan et al. (2011) reported a carbon-rich atmosphere, "
    "in contrast to the host star.'\n"
    "  GOOD: 'Madhusudhan et al. (2011) reported a carbon-rich atmosphere "
    "[2], in contrast to the host star [2].'\n\n"
    "If the excerpts don't support an answer, say so plainly rather than "
    "guessing. Respond with a single fenced json block:\n"
    "```json\n"
    '{"answer": "prose with [1] and [2] markers...",\n'
    ' "citations": [{"id": 1, "doc_id": "...", "node_id": "..."}]}\n'
    "```"
)

RETRY_PROMPT_SUFFIX = (
    "\n\nYour previous answer left some named studies without an inline [n] "
    "right next to them: {mentions}. Rewrite the answer so EVERY sentence "
    "that names a study also carries its [n] marker in that same sentence."
)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _condense_history(messages: list[dict], max_turns: int = 6) -> str:
    """Last N turns verbatim. (A long-thread summarization pass is a documented
    future enhancement, not needed for this corpus's realistic conversation
    lengths — see docs/spec/03_webapp.md.)"""
    recent = messages[-max_turns:]
    lines = []
    for m in recent:
        if m["role"] == "user":
            lines.append(f"User: {m['text']}")
        elif m["role"] == "assistant" and m.get("payload"):
            for r in m["payload"].get("results", []):
                lines.append(f"Assistant: {r['answer']}")
    return "\n".join(lines)


def _rewrite_query(question: str) -> str | None:
    try:
        resp = _client.chat.completions.create(
            model=config.BULK_MODEL,
            messages=[{"role": "user", "content":
                       "Rewrite this question as 5-8 search keywords "
                       "(astronomy jargon, synonyms). Output only the "
                       f"keywords, space-separated: {question}"}],
            temperature=0.3, max_tokens=200,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return None


def _parse_answer(text: str) -> dict | None:
    m = _JSON_BLOCK_RE.search(text)
    block = m.group(1) if m else text
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None


def stream_chat(registry: CorpusRegistry, conversation_id: int, question: str) -> Iterator[dict]:
    yield {"event": "step", "step": "history", "status": "start"}
    history_messages = store.list_messages(conversation_id)
    history_text = _condense_history(history_messages)
    yield {"event": "step", "step": "history", "status": "done"}

    yield {"event": "step", "step": "search", "status": "start"}
    hits = search_nodes(question, limit=12)
    if len(hits) < 3:
        yield {"event": "step", "step": "rewrite", "status": "start"}
        rewritten = _rewrite_query(question)
        if rewritten:
            hits = search_nodes(f"{question} {rewritten}", limit=12)
        yield {"event": "step", "step": "rewrite", "status": "done"}
    yield {"event": "step", "step": "search", "status": "done", "hit_count": len(hits)}

    if not hits:
        payload = {"results": [{"question": question,
                   "answer": "I couldn't find anything in the knowledge base relevant to this question.",
                   "citations": []}]}
        answer_id = store.create_answer("chat_message", payload, conversation_id=conversation_id)
        store.add_message(conversation_id, "user", text=question)
        store.add_message(conversation_id, "assistant", answer_id=answer_id)
        yield {"event": "done", "answer_id": answer_id, **payload, "documents": [], "warnings": []}
        return

    context = "\n\n".join(
        f"[{i + 1}] (doc_id={h['doc_id']}, node_id={h['node_id']}) "
        f"{h['title']} ({h['year']}) — {h['block_type']}:\n{h['full_text'][:1000]}"
        for i, h in enumerate(hits))
    hit_by_index = {i + 1: h for i, h in enumerate(hits)}

    yield {"event": "step", "step": "answer", "status": "start"}
    user_content = (f"Conversation so far:\n{history_text}\n\n" if history_text else "") + \
                   f"Excerpts:\n\n{context}\n\nQuestion: {question}"

    def call_model(extra_user_suffix: str = "") -> dict | None:
        resp = _client.chat.completions.create(
            model=config.CHAT_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_content + extra_user_suffix}],
            max_tokens=2000,
            # No `temperature` — this gateway 400s on it for claude-sonnet-5 ("temperature
            # is deprecated for this model"), same issue kasi_papers_analysis documented
            # for its Opus SYNTH_MODEL calls; kb/topic_pages.py already omits it for the
            # same reason.
        )
        return _parse_answer(resp.choices[0].message.content or "")

    raw = call_model()
    # Bounded retry-once: the model sometimes names a study by author/year in
    # prose without attaching the required [n] to that sentence — caught here
    # via the same heuristic validate_and_enrich uses, rather than letting a
    # citation-contract violation through silently (found via a real user
    # report: a chat answer named "Madhusudhan et al. (2011a,b)" with zero
    # citations anywhere near it, and validate_and_enrich's marker-only check
    # had no way to catch a *missing* marker, only a dangling one).
    if raw and raw.get("answer"):
        mentions = find_uncited_author_year_mentions(raw["answer"])
        if mentions:
            retried = call_model(RETRY_PROMPT_SUFFIX.format(mentions=mentions))
            if retried and retried.get("answer"):
                raw = retried
    yield {"event": "step", "step": "answer", "status": "done"}

    if raw is None:
        payload = {"results": [{"question": question,
                   "answer": "The model's response could not be parsed. Please try rephrasing.",
                   "citations": []}]}
    else:
        # The model was shown excerpts numbered 1..N — map its citation ids
        # back to real (doc_id, node_id) pairs itself (it was told doc_id/
        # node_id directly in the prompt, so this is a courtesy re-mapping in
        # case it echoed the excerpt index instead).
        citations = []
        for c in raw.get("citations", []):
            doc_id = c.get("doc_id")
            node_id = c.get("node_id")
            if (not doc_id or not node_id) and isinstance(c.get("id"), int) and c["id"] in hit_by_index:
                h = hit_by_index[c["id"]]
                doc_id, node_id = h["doc_id"], h["node_id"]
            citations.append({"id": c.get("id"), "doc_id": doc_id, "node_id": node_id})
        payload = {"results": [{"question": question, "answer": raw.get("answer", ""),
                   "citations": citations}]}

    yield {"event": "step", "step": "validate", "status": "start"}
    try:
        enriched = validate_and_enrich(payload, registry.docs, strict=False)
    except ValueError:
        enriched = {"results": payload["results"], "documents": [], "warnings": ["validation failed"]}
    yield {"event": "step", "step": "validate", "status": "done"}

    answer_id = store.create_answer("chat_message", enriched, conversation_id=conversation_id)
    store.add_message(conversation_id, "user", text=question)
    store.add_message(conversation_id, "assistant", answer_id=answer_id)

    yield {"event": "done", "answer_id": answer_id, **enriched}
