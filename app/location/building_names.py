"""
Location resolution improvement -- remaining Phase A: building-name variants.

Campus building codes ("AB1") rarely match how the same building is
written in OpenStreetMap or in a document ("Academic Block 1", "AB-1",
"AB 1"). Without this, neither Nominatim nor the RAG knowledge base can
ever match a short code the user typed -- e.g. the user's own
`data/documents/Vit_building_details.pdf` only ever says "Academic Block 1".

This module ONLY produces alternative spellings of a NAME, driven by
`settings.building_alias_prefixes` (a naming convention the user controls
in .env). It never produces, stores, or implies a coordinate -- every
variant still has to be found by a real source (OSM / RAG) before
anything is resolved, so this cannot fabricate a location.
"""

from __future__ import annotations

import re

from app.config import settings

# "AB1", "AB 1", "AB-1", "ab1" -> ("AB", "1")
_CODE_PATTERN = re.compile(r"^\s*([A-Za-z]+)\s*[-_ ]?\s*(\d+[A-Za-z]?)\s*$")


def building_name_variants(building: str, alias_prefixes: dict[str, str] | None = None) -> list[str]:
    """
    Returns `building` itself first, then any alternative spellings, with
    duplicates removed (case-insensitively) and original order kept.

        building_name_variants("AB1")              -> ["AB1", "AB 1", "AB-1", "Academic Block 1"]
        building_name_variants("Academic Block 1") -> ["Academic Block 1", "AB1", "AB 1", "AB-1"]
        building_name_variants("Library")          -> ["Library"]
    """
    prefixes = alias_prefixes if alias_prefixes is not None else settings.building_alias_prefixes
    name = (building or "").strip()
    if not name:
        return []

    variants: list[str] = [name]

    match = _CODE_PATTERN.match(name)
    if match:
        code, number = match.group(1), match.group(2)
        variants += [f"{code.upper()}{number}", f"{code.upper()} {number}", f"{code.upper()}-{number}"]
        for abbrev, full in prefixes.items():
            if abbrev.lower() == code.lower():
                variants.append(f"{full} {number}")
    else:
        # Reverse direction: "Academic Block 1" -> "AB1"
        for abbrev, full in prefixes.items():
            reverse = re.match(rf"^\s*{re.escape(full)}\s+(\d+[A-Za-z]?)\s*$", name, re.IGNORECASE)
            if reverse:
                number = reverse.group(1)
                variants += [f"{abbrev.upper()}{number}", f"{abbrev.upper()} {number}", f"{abbrev.upper()}-{number}"]

    seen: set[str] = set()
    unique: list[str] = []
    for v in variants:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def name_pattern(variant: str) -> re.Pattern:
    """
    Case-insensitive regex matching `variant` as a whole name -- so
    "Academic Block 1" does NOT match inside "Academic Block 12".
    """
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])", re.IGNORECASE)


_SEPARATORS = re.compile(r"[-_./,()#:;|&]+")
_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X"}


def normalize_name(text: str) -> str:
    """
    Lower-case, punctuation -> space, spaces collapsed, so real-world
    spellings compare equal: "Academic Block-1", "Academic Block - 1" and
    "ACADEMIC BLOCK 1" all become "academic block 1"; "AB-1" -> "ab 1".
    """
    return " ".join(_SEPARATORS.sub(" ", text or "").lower().split())


def _match_forms(variant: str) -> set[str]:
    """A variant plus its roman-numeral form ("Academic Block 1" -> "academic block i")."""
    norm = normalize_name(variant)
    forms = {norm}
    m = re.match(r"^(.*\D)\s*(\d+)$", norm)
    if m and int(m.group(2)) in _ROMAN:
        forms.add(f"{m.group(1).strip()} {_ROMAN[int(m.group(2))].lower()}")
    return {f for f in forms if f}


def mentions_building(text: str, variants: list[str]) -> bool:
    """
    True if `text` mentions any name variant as a WHOLE name, after
    normalizing punctuation/spacing on both sides (Phase 15: Google and OSM
    names like "Academic Block-1" or "VIT Chennai AB-1" must match "AB1").
    "Academic Block 1" still never matches "Academic Block 12".
    """
    haystack = f" {normalize_name(text)} "
    for variant in variants:
        for form in _match_forms(variant):
            if f" {form} " in haystack:
                return True
            # "ab1" vs "ab 1": also compare the code with its space removed
            compact = form.replace(" ", "")
            if compact != form and f" {compact} " in haystack:
                return True
    return False
