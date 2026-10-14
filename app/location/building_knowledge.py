"""
Location resolution improvement -- remaining Phase B: building facts from
the EXISTING RAG knowledge base.

Spec section 8: "Existing RAG retrieves... Building Information" and
section 10: "safe altitude". The location resolver finds WHERE a building
is; this module finds WHAT the project's own documents say about it --
today, its height -- using the unmodified Phase 2 retriever. With the
user's `data/documents/Vit_building_details.pdf` indexed, "AB1" finds
"4 Academic Block 1 36" in that paper's Table 1 (H max, metres), page 6.

Rules this module follows (same as the rest of app/location/):
- Nothing is invented. If no document states a height, `height_m` is
  None and nothing downstream pretends otherwise.
- Every value carries its source file and PDF page, so a human can check it.
- Extraction is a documented heuristic over free text, so a value is only
  ever used to make safety STRICTER (a required roof clearance), never to
  relax a check. If documents disagree, the LARGEST height is kept (the
  cautious choice) and the disagreement is reported as a warning.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.location.building_names import building_name_variants, mentions_building

log = logging.getLogger(__name__)

# A plausible building height in metres -- outside this, a parsed number is
# almost certainly something else (a year, a page number, a building count).
_MIN_HEIGHT_M, _MAX_HEIGHT_M = 2.0, 300.0

# The chunk has a height column/label, e.g. "H (m)", "Height (m)", "height".
_HEIGHT_CONTEXT = re.compile(r"\bheight\b|\bH\s*\(\s*m\s*\)", re.IGNORECASE)


@dataclass
class BuildingFacts:
    """What the project's own documents say about one building. All fields optional -- absent means unknown, never guessed."""

    building: str
    height_m: float | None = None
    height_source: str | None = None
    height_page: int | None = None
    evidence: str | None = None          # the exact text the height was read from
    warnings: list[str] = field(default_factory=list)

    @property
    def height_known(self) -> bool:
        return self.height_m is not None

    def citation(self) -> str | None:
        if self.height_source is None:
            return None
        return f"{self.height_source}, page {self.height_page}" if self.height_page is not None else self.height_source

    def as_dict(self) -> dict:
        return {
            "building": self.building,
            "height_m": self.height_m,
            "height_source": self.height_source,
            "height_page": self.height_page,
            "evidence": self.evidence,
            "warnings": self.warnings,
        }


def _extract_height(text: str, variant: str) -> tuple[float, str] | None:
    """
    Two patterns, in order:
      1. The name followed (within a short span) by a number WITH a metre
         unit -- "AB1 | 7 | 45 m", "Academic Block 1 is 36 m tall".
      2. Only if the chunk has a height column/label ("H (m)", "Height"):
         the first number right after the name -- the user's PDF's Table 1
         row "4 Academic Block 1 36" under the header "H (m) max".
    Returns (height_m, evidence_snippet) or None.
    """
    name = re.escape(variant)
    boundary_before = r"(?<![A-Za-z0-9])"
    with_unit = re.compile(
        rf"{boundary_before}{name}(?![A-Za-z0-9])[^\n]{{0,40}}?(?<![\d.])(\d+(?:\.\d+)?)\s*(?:m|metres|meters)\b",
        re.IGNORECASE,
    )
    match = with_unit.search(text)
    if match is None and _HEIGHT_CONTEXT.search(text):
        bare = re.compile(rf"{boundary_before}{name}(?![A-Za-z0-9])\s*[|:,-]?\s*(\d+(?:\.\d+)?)(?![\d.])", re.IGNORECASE)
        match = bare.search(text)
    if match is None:
        return None
    value = float(match.group(1))
    if not (_MIN_HEIGHT_M <= value <= _MAX_HEIGHT_M):
        return None
    return value, match.group(0).strip()


def lookup_building_facts(building: str, campus: str | None = None, *, retrieve_fn=None, k: int = 5) -> BuildingFacts:
    """
    Query the existing RAG index for `building` (every name variant, e.g.
    "AB1" and "Academic Block 1") and extract a height if a document states
    one. Never raises for a missing/empty index -- returns empty facts.
    """
    facts = BuildingFacts(building=building)
    if not building:
        return facts

    if retrieve_fn is None:
        from app.rag.retriever import retrieve as retrieve_fn  # noqa: PLC0415 -- lazy, same as building_resolver.py

    variants = building_name_variants(building)
    query = f"{' / '.join(variants)} building height details" + (f" {campus}" if campus else "")
    try:
        chunks = retrieve_fn(query, k=k)
    except Exception as exc:  # noqa: BLE001 -- RAG unavailable must not break mission planning
        log.warning("Building-knowledge RAG lookup failed for %r: %s", building, exc)
        facts.warnings.append(f"RAG knowledge base unavailable ({exc}) -- building height unknown.")
        return facts

    found: list[tuple[float, str, int | None, str]] = []
    for chunk in chunks:
        if not mentions_building(chunk.text, variants):
            continue
        for variant in variants:
            hit = _extract_height(chunk.text, variant)
            if hit is not None:
                found.append((hit[0], chunk.source, getattr(chunk, "page", None), hit[1]))
                break

    if not found:
        return facts

    heights = sorted({h for h, *_ in found})
    height, source, page, evidence = max(found, key=lambda f: f[0])
    facts.height_m, facts.height_source, facts.height_page, facts.evidence = height, source, page, evidence
    if len(heights) > 1:
        facts.warnings.append(
            f"Documents disagree on {building}'s height ({', '.join(f'{h:g} m' for h in heights)}) -- "
            f"using the largest, {height:g} m, as the cautious choice."
        )
    log.info("Building facts for %r: height %s m (source: %s)", building, height, facts.citation())
    return facts


def render_building_facts(facts: BuildingFacts) -> str:
    """Plain-text lines appended to the mission's location context (same style as _render_location_context)."""
    if not facts.height_known:
        return "Building height: unknown (no project document states it) -- keep a conservative altitude."
    line = f"Building height: {facts.height_m:g} m (source: {facts.citation()})."
    for w in facts.warnings:
        line += f"\nNote: {w}"
    return line
