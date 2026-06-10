"""Common schema shared across all ATS adapters.

Every adapter must return a list of Posting objects so the downstream
storage and LLM-scoring stages never need to know which ATS a row came from.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional


def _clean(text: Optional[str]) -> str:
    """Strip HTML tags and collapse whitespace from a description blob."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)          # drop HTML tags
    text = re.sub(r"&[a-z]+;", " ", text)          # crude entity strip
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@dataclass
class Posting:
    source: str               # ats vendor, e.g. "greenhouse"
    company: str              # company slug / display name
    external_id: str          # the ATS's own id for this job
    title: str
    location: str
    description: str
    url: str
    posted_at: Optional[str]  # ISO 8601 string if the ATS gives one

    @property
    def fingerprint(self) -> str:
        """Stable hash for dedupe across sources and re-runs.

        Deliberately based on title+company+location rather than the ATS id,
        so the same role cross-posted to two boards collapses to one row.
        """
        key = f"{self.title.lower()}|{self.company.lower()}|{self.location.lower()}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        d["fetched_at"] = datetime.now(timezone.utc).isoformat()
        return d


def normalize(
    *,
    source: str,
    company: str,
    external_id: str,
    title: str,
    location: str,
    description: str,
    url: str,
    posted_at: Optional[str] = None,
) -> Posting:
    """Single choke point so adapters can't drift on field hygiene."""
    return Posting(
        source=source,
        company=company,
        external_id=str(external_id),
        title=(title or "").strip(),
        location=(location or "").strip() or "Unspecified",
        description=_clean(description),
        url=(url or "").strip(),
        posted_at=posted_at,
    )
