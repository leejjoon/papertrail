---
name: wasp12b-answer
description: Answer a question about the WASP-12b paper corpus using the wasp12b-kb MCP server, with every claim properly cited, then publish the result. Use whenever asked to research/answer something from this corpus, or to "cite properly," "publish an answer," or "answer with references" for WASP-12b papers.
---

Answer the question using the `wasp12b-kb` MCP server, then publish the result. Follow this exact sequence — do not skip steps or publish early.

## 1. Scope

Call `list_papers()` with no arguments first if you don't already know the corpus's taxonomy (research_theme/method/facility/role_of_wasp12b values), or `search_fulltext(question)` directly if you have a concrete question. Use taxonomy filters on `search_fulltext`/`list_papers` when the question names a theme, method, or facility.

## 2. Read before you cite

Never cite a `(doc_id, node_id)` pair you have not actually read. For every candidate hit from search, call `get_section(doc_id, node_id)` (or `get_paper(doc_id)` for a fuller view, `get_figure_image(doc_id, node_id)` if a figure matters) before using it as a citation. `get_section` on a heading node returns the whole subtree; on a leaf it returns that node plus its parent heading for context — use whichever fits what you're citing.

A single paragraph read alone can be ambiguous — a pronoun or comparison that points outside itself ("this discrepancy," "the equilibrium case," a bare ratio/number with no stated assumptions), or a term whose exact meaning is paper-specific (e.g. what "C/O ratio" assumes, what counts as a "detection" vs. "tentative"). If a node's text looks like it depends on something not in front of you, re-call `get_section(doc_id, node_id, context=N)` with a larger `N` before citing it — don't guess at the antecedent, and don't cite a node whose claim you can't actually verify from what you've read.

## 3. Draft with a hard citation rule

Every claim needs an inline `[n]` marker — no exceptions, including when you name a specific study by author/year in your prose. **Never write an author-year mention as a substitute for `[n]`; always attach both, to that exact sentence:**

- BAD:  `Madhusudhan et al. (2011) reported a carbon-rich atmosphere, in contrast to the host star.`
- GOOD: `Madhusudhan et al. (2011) reported a carbon-rich atmosphere [2], in contrast to the host star [2].`

Build the payload shape:
```json
{"results": [{"question": "...", "answer": "prose with [1] and [2] markers...",
              "citations": [{"id": 1, "doc_id": "...", "node_id": "..."}, ...]}]}
```

## 4. Validate — and read the warnings, not just the errors

Call `validate_answer(payload)`. Two distinct things can come back:

- **A hard error** (`{"error": "invalid citations", "offending": [...]}`) — a citation doesn't resolve. Fix the `doc_id`/`node_id` and retry.
- **A `warnings` list on an otherwise-successful response** — e.g. `"possible uncited claim(s) near: ['Madhusudhan et al. (2011)']"`. This is the heuristic that catches exactly the BAD pattern in step 3: a named study with no `[n]` anywhere near it. **Treat any such warning as something to go fix** (add the missing marker to that sentence) and re-validate — a clean HTTP-style non-error response does not by itself mean the citations are complete.

The heuristic does have real false positives on long, correctly-cited sentences (a citation placed at the end of a compound sentence, well after the author name, can still trigger it) — use judgment on a second look rather than looping forever chasing zero warnings.

## 5. Publish

Once `warnings` is empty (or every remaining one has been checked and is a genuine false positive), call `publish_answer(payload, title="...")`. Report the returned `url` back to the user directly — that's how they view it in the web app with the same citation UX as chat.
