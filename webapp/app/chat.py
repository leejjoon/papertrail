"""Chat RAG flow: condensed history -> FTS5 search (+ optional query rewrite on
a thin result set) -> CHAT_MODEL answer (+ optional one-round context
escalation) -> validate_and_enrich -> persist.

`stream_chat()` is a generator so main.py's SSE endpoint can forward each step
as it happens (kasi_papers_analysis's `ask` tool + spherex-chatbot's SSE step
pattern, combined — see docs/spec/03_webapp.md). The final yielded item is
always `{"event": "done", ...enriched_payload}`; every other item is
`{"event": "step", "step": ..., "status": "start"|"done"}`.

Context escalation: retrieval is paragraph-granular, and a single paragraph
read alone is often ambiguous (an unresolved pronoun/comparison, or a
paper-specific term with no definition in that node) — validate_and_enrich()
only proves a citation resolves, never that it was read in enough context to
support the claim next to it. Rather than depend on this gateway's unverified
tool-calling support, the model can request more of a specific excerpt via a
`need_context` field in its normal fenced-json response (see SYSTEM_PROMPT);
`_expand_excerpts` widens it via the same `context_window` sibling-window
logic kb/mcp_server.py's `get_section(..., context=N)` uses for the MCP-agent
path, bounded to one round and to excerpts already retrieved.
"""
from __future__ import annotations

import json
import re
from typing import Iterator

from openai import OpenAI

from . import config, store
from .answer import InvalidCitationsError, find_uncited_author_year_mentions, validate_and_enrich
from .registry import CorpusRegistry, context_window
from .search import search_nodes

# Bounds on the one-shot context-escalation round (see stream_chat) — the
# model can widen what it was already shown, never fetch new corpus text, and
# can't loop or blow up prompt size doing it.
MAX_CONTEXT_REQUESTS = 4       # requests honored per escalation round
MAX_CONTEXT_WINDOW = 3         # clamp on the requested sibling count
EXPANDED_CHAR_BUDGET = 4000    # vs. the default 1000 — expansion would be
                               # pointless if immediately truncated back down

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
    "AMBIGUOUS EXCERPTS — each excerpt is one paragraph and can be ambiguous "
    "read alone: a pronoun or comparison pointing outside itself ('this "
    "discrepancy,' 'the equilibrium case'), or a paper-specific term with no "
    "definition in that excerpt (e.g. what a given paper's 'C/O ratio' "
    "assumes). If you can't be sure what an excerpt actually means, do not "
    "guess and do not cite it as-is — instead of a final answer, ask for "
    "more of its surrounding text:\n"
    "```json\n"
    '{"need_context": [{"doc_id": "...", "node_id": "...", "context": 2}]}\n'
    "```\n"
    "You'll then be shown the same excerpts again with more text around the "
    "ones you asked about, and must answer for real at that point — you get "
    "exactly one such request per question, so ask for everything you need "
    "at once.\n\n"
    "If the excerpts don't support an answer, say so plainly rather than "
    "guessing. Otherwise respond with a single fenced json block:\n"
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

CONTEXT_FOLLOWUP_SUFFIX = (
    "\n\nYou now have the expanded excerpts you asked for, widened with "
    "surrounding text (each cited node is still marked '>>> CITED NODE ... "
    ">>> ... <<<'). Answer now with the required fenced-json "
    '{"answer": ..., "citations": [...]} block — no further need_context '
    "requests will be honored this turn."
)

CONTEXT_FOLLOWUP_EMPTY_SUFFIX = (
    "\n\nNone of your context requests could be resolved (they didn't match "
    "a doc_id/node_id from the excerpts above). Answer using only the "
    "excerpts as originally given, or say plainly that they don't support an "
    "answer — no further need_context requests will be honored this turn."
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


def _build_excerpt(h: dict, idx: int, expanded: dict[tuple[str, str], str] | None = None) -> str:
    """One numbered excerpt line for the prompt. If `expanded` has a widened
    version of this hit (from a context-escalation round), use it in place of
    the default 1000-char truncation — its own [Parent Title] prefix (added
    by context_window) makes the ", section: ..." annotation redundant, so
    that's dropped only in this branch."""
    key = (h["doc_id"], h["node_id"])
    text = (expanded or {}).get(key)
    if text is None:
        text = h["full_text"][:1000]
        section = f", section: {h['parent_title']}" if h.get("parent_title") else ""
    else:
        section = ""
    return (f"[{idx}] (doc_id={h['doc_id']}, node_id={h['node_id']}) "
            f"{h['title']} ({h['year']}) — {h['block_type']}{section}:\n{text}")


def _expand_excerpts(registry: CorpusRegistry, hits: list[dict],
                     requests: list) -> dict[tuple[str, str], str]:
    """Widen a bounded subset of already-retrieved hits per the model's
    need_context requests. Restricted to (doc_id, node_id) pairs already in
    `hits` — the model can ask to see more of what it was shown, not fetch
    arbitrary corpus text — and clamped on count/window (MAX_CONTEXT_REQUESTS/
    MAX_CONTEXT_WINDOW) so a malformed or oversized request can't blow up
    prompt size. Silently skips anything that doesn't resolve rather than
    raising — this reads a payload the model generated, not a trusted
    caller."""
    hit_keys = {(h["doc_id"], h["node_id"]) for h in hits}
    expanded: dict[tuple[str, str], str] = {}
    for req in (requests or [])[:MAX_CONTEXT_REQUESTS]:
        if not isinstance(req, dict):
            continue
        key = (req.get("doc_id"), req.get("node_id"))
        if key not in hit_keys or key in expanded:
            continue
        try:
            ctx = max(0, min(int(req.get("context", 2)), MAX_CONTEXT_WINDOW))
        except (TypeError, ValueError):
            ctx = 2
        doc = registry.docs.get(key[0])
        if doc is None or not doc.has_fulltext:
            continue
        text = context_window(doc.tree["structure"], key[1], ctx)
        if text:
            expanded[key] = text[:EXPANDED_CHAR_BUDGET]
    return expanded


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

    excerpts_block = "\n\n".join(_build_excerpt(h, i + 1) for i, h in enumerate(hits))
    hit_by_index = {i + 1: h for i, h in enumerate(hits)}

    yield {"event": "step", "step": "answer", "status": "start"}
    user_content = (f"Conversation so far:\n{history_text}\n\n" if history_text else "") + \
                   f"Excerpts:\n\n{excerpts_block}\n\nQuestion: {question}"

    def call_model(content: str) -> dict | None:
        resp = _client.chat.completions.create(
            model=config.CHAT_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": content}],
            max_tokens=2000,
            # No `temperature` — this gateway 400s on it for claude-sonnet-5 ("temperature
            # is deprecated for this model"), same issue kasi_papers_analysis documented
            # for its Opus SYNTH_MODEL calls; kb/topic_pages.py already omits it for the
            # same reason.
        )
        return _parse_answer(resp.choices[0].message.content or "")

    raw = call_model(user_content)

    # Context escalation, exactly one round, before the uncited-mention retry
    # below (get the facts right first, then get the citation form right).
    # `raw.get("answer")` guards against a model that returns need_context
    # AND an answer in the same response — an answer given means it already
    # committed, so don't second-guess it into another round-trip.
    if raw and raw.get("need_context") and not raw.get("answer"):
        yield {"event": "step", "step": "context", "status": "start"}
        expanded = _expand_excerpts(registry, hits, raw["need_context"])
        if expanded:
            excerpts_block = "\n\n".join(_build_excerpt(h, i + 1, expanded) for i, h in enumerate(hits))
            user_content = (f"Conversation so far:\n{history_text}\n\n" if history_text else "") + \
                           f"Excerpts:\n\n{excerpts_block}\n\nQuestion: {question}"
            suffix = CONTEXT_FOLLOWUP_SUFFIX
        else:
            suffix = CONTEXT_FOLLOWUP_EMPTY_SUFFIX
        raw = call_model(user_content + suffix)
        yield {"event": "step", "step": "context", "status": "done"}

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
            retried = call_model(user_content + RETRY_PROMPT_SUFFIX.format(mentions=mentions))
            if retried and retried.get("answer"):
                raw = retried
    yield {"event": "step", "step": "answer", "status": "done"}

    if raw is None or not raw.get("answer"):
        # Covers an unparseable response AND the degenerate case where the
        # model still returned no answer after a context-escalation round
        # (e.g. its need_context requests didn't resolve to anything usable).
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
