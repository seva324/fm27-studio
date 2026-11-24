"""The FM27 Match Lab knowledge base: measured match-engine findings.

The data is the person's own ``fm27-knowledge/studies.json`` from the FM AI
Assistant project, shipped here unchanged as ``data/fm27-studies.json``.  Every
finding is a paired-comparison measurement over replayed FM27 matches, so it
describes the *engine*, not this save: it is the assistant's semantic memory,
the counterpart of the career facts a snapshot holds.

Nothing here talks to a provider.  The module is a pure reader plus a search,
so ``tests/test_knowledge.py`` can drive it without Windows or a save.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
DATA_DIR = os.path.join(APP_DIR, "data")
STUDIES_PATH = os.path.join(DATA_DIR, "fm27-studies.json")

# The order ``fm27_get_management_priorities`` reports.  It is the assistant's
# own list, and it is deliberately short: sharpness and condition outrank every
# tactical change, familiarity outranks a tactic tweak, and Current Ability is
# not on it at all because the engine does not read it.
MANAGEMENT_PRIORITY_ORDER = (
    "S5-SHARPNESS-FIRST",
    "S5-CONDITION-SECOND",
    "S5-POSITION-RATINGS-XI",
    "S7-TACTIC-SPREAD",
    "S3-FAMILIARITY-COST",
    "S24-FAMILIARITY-PRIORITY",
    "S5-PRESSING-TRAP",
    "S5-MENTALITY",
    "S5-COHESION",
    "S15-HOME-ADVANTAGE",
)

# Match inputs the snapshot cannot hold, so advice must ask rather than assume.
NOT_IN_SNAPSHOT = ("match_sharpness", "match_condition", "tactical_familiarity")

TAGS = (
    "attribute", "tactics", "familiarity", "context", "goalkeeper", "position",
    "model", "method", "robustness", "negative-finding",
)


@dataclass(frozen=True)
class Finding:
    id: str
    study: str
    tags: tuple[str, ...]
    claim: str
    metric: str | None
    magnitude: float | None
    per: str | None
    confidence: str | None
    detail: str | None
    source: str | None

    def to_map(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "finding_id": self.id,
            "study": self.study,
            "tags": list(self.tags),
            "claim": self.claim,
        }
        if self.metric:
            out["metric"] = self.metric
        if self.magnitude is not None:
            out["magnitude"] = self.magnitude
        if self.per:
            out["per"] = self.per
        if self.confidence:
            out["confidence"] = self.confidence
        if self.detail:
            out["detail"] = self.detail
        if self.source:
            out["source"] = self.source
        return out


@dataclass(frozen=True)
class Pack:
    version: str
    engine: str
    source: str
    method: dict[str, str]
    caveats: tuple[str, ...]
    findings: tuple[Finding, ...]

    def study_ids(self) -> list[str]:
        return sorted({finding.study for finding in self.findings})


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse(payload: Any) -> Pack:
    """Build a pack from the decoded JSON, ignoring anything malformed."""
    if not isinstance(payload, dict):
        raise ValueError("The Match Lab data is not an object.")
    findings: list[Finding] = []
    for row in payload.get("findings") or []:
        if not isinstance(row, dict):
            continue
        identifier = _text(row.get("id"))
        claim = _text(row.get("claim"))
        if not identifier or not claim:
            continue
        raw_tags = row.get("tags")
        tags = tuple(
            str(tag).strip() for tag in raw_tags
            if str(tag or "").strip()) if isinstance(raw_tags, list) else ()
        findings.append(Finding(
            id=identifier,
            study=_text(row.get("study")) or "",
            tags=tags,
            claim=claim,
            metric=_text(row.get("metric")),
            magnitude=_number(row.get("magnitude")),
            per=_text(row.get("per")),
            confidence=_text(row.get("confidence")),
            detail=_text(row.get("detail")),
            source=_text(row.get("source")),
        ))
    method = payload.get("method")
    raw_caveats = payload.get("caveats")
    return Pack(
        version=_text(payload.get("version")) or "",
        engine=_text(payload.get("engine")) or "",
        source=_text(payload.get("source")) or "",
        method={str(k): str(v) for k, v in method.items()} if isinstance(method, dict) else {},
        caveats=tuple(str(item) for item in raw_caveats) if isinstance(raw_caveats, list) else (),
        findings=tuple(findings),
    )


_CACHE: Pack | None = None


def load(path: str | None = None) -> Pack:
    """The shipped pack, read once."""
    global _CACHE
    if path is None and _CACHE is not None:
        return _CACHE
    with open(path or STUDIES_PATH, encoding="utf-8") as handle:
        pack = parse(json.load(handle))
    if path is None:
        _CACHE = pack
    return pack


def all_findings() -> tuple[Finding, ...]:
    return load().findings


def find(finding_id: str) -> Finding | None:
    wanted = (finding_id or "").strip().upper()
    if not wanted:
        return None
    for finding in all_findings():
        if finding.id.upper() == wanted:
            return finding
    return None


def search(query: str = "", tag: str = "", limit: int = 5) -> list[Finding]:
    """Literal, case-insensitive matching over id, claim, detail and tags."""
    words = [word for word in (query or "").lower().split() if word]
    wanted_tag = (tag or "").strip().lower()
    scored: list[tuple[int, Finding]] = []
    for finding in all_findings():
        if wanted_tag and wanted_tag not in {t.lower() for t in finding.tags}:
            continue
        haystack = " ".join(filter(None, (
            finding.id, finding.study, finding.claim, finding.detail,
            finding.per, " ".join(finding.tags)))).lower()
        if words and not all(word in haystack for word in words):
            continue
        score = 0
        for word in words:
            if word in finding.id.lower():
                score += 6
            if word in finding.claim.lower():
                score += 3
            if word in " ".join(finding.tags).lower():
                score += 2
        scored.append((score, finding))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    cap = max(1, min(20, int(limit or 5)))
    return [finding for _score, finding in scored[:cap]]


def study(prefix: str) -> list[Finding]:
    wanted = (prefix or "").strip().lower()
    if not wanted:
        return []
    return [finding for finding in all_findings()
            if finding.study.lower() == wanted]


def priorities() -> list[dict[str, Any]]:
    """The management priorities, in order, with the ones present filled in."""
    out: list[dict[str, Any]] = []
    for identifier in MANAGEMENT_PRIORITY_ORDER:
        finding = find(identifier)
        if finding is None:
            continue
        row = finding.to_map()
        row["priority"] = len(out) + 1
        out.append(row)
    return out


def describe(pack: Pack | None = None) -> dict[str, Any]:
    pack = pack or load()
    return {
        "version": pack.version,
        "engine": pack.engine,
        "findings": len(pack.findings),
        "studies": pack.study_ids(),
        "source": pack.source,
        "method": pack.method,
        "caveats": list(pack.caveats),
    }


def guidance() -> str:
    return (
        "These are measured engine effects from replayed matches, not opinions. "
        "A confidence of medium means the effect was near the study's resolution. "
        "'No clear effect' means below resolution, never 'no effect'. They "
        "describe the engine, not this save's players; combine them with the "
        "snapshot and live memory data.")


def tags() -> Iterable[str]:
    return TAGS
