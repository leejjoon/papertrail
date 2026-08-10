"""wasp12b's citation style: the one thing AuthorYearStyle's defaults don't
cover is doc_id canonicalization — wasp12b's doc_ids are bibcodes with '&'
(A&A) filesystem-escaped to '+' (paper_download/config.py's safe_bibcode()),
so a caller (the MCP server's get_paper, mainly) may pass either the raw
bibcode or the escaped doc_id and expects both to resolve.

A 6-line subclass, not a generic `identity.doc_id_aliases` config knob — this
is an N=1 need specific to wasp12b's bibcode-based doc_ids, and the escape
hatch (`citation_style: ./style.py:ClassName`) exists exactly for cases like
this (see papertrail.identity.load_citation_style).
"""
from papertrail.identity import AuthorYearStyle


class Wasp12bStyle(AuthorYearStyle):
    def canonicalize(self, raw: str) -> list[str]:
        candidates = [raw]
        escaped = raw.replace("&", "+")
        if escaped != raw:
            candidates.append(escaped)
        return candidates
