"""Common schema shared across all ATS adapters.

Every adapter must return a list of Posting objects so the downstream
storage and LLM-scoring stages never need to know which ATS a row came from.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


def _clean(text: str | None) -> str:
    """Strip HTML tags and collapse whitespace from a description blob."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)  # drop HTML tags
    text = re.sub(r"&[a-z]+;", " ", text)  # crude entity strip
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@dataclass
class Posting:
    source: str  # ats vendor, e.g. "greenhouse"
    company: str  # company slug / display name
    external_id: str  # the ATS's own id for this job
    title: str
    location: str
    description: str
    url: str
    posted_at: str | None  # ISO 8601 string if the ATS gives one

    @property
    def fingerprint(self) -> str:
        """Stable hash for dedupe across sources and re-runs.

        Deliberately based on title+company+location+description rather than
        the ATS id, so the same role cross-posted to two boards collapses to
        one row while two distinct postings that happen to share a
        title/company/location stay separate (see data-model.md "Dedupe
        identity revision").

        Accepted failure mode: editing a posting's description (or
        board-specific boilerplate on a cross-post) changes this fingerprint,
        so an already-seen role can re-surface in a later digest as "new".
        This is intentional — the maintainer can flag the re-surfaced row
        with the `duplicate` application status to suppress it permanently.
        The alternative (the old three-part key) silently dropped distinct
        same-title postings via INSERT OR IGNORE, which is worse: silent data
        loss instead of a visible, correctable re-surfacing.
        """
        key = (
            f"{self.title.lower()}|{self.company.lower()}|"
            f"{self.location.lower()}|{self.description.lower()}"
        )
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict[str, str]:
        """Convert posting to a database row dict.

        Returns a dictionary with all posting fields plus fingerprint and
        fetched_at timestamp.
        """
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
    posted_at: str | None = None,
) -> Posting:
    """Normalize raw posting data into a canonical Posting object.

    Single choke point so adapters can't drift on field hygiene. Strips
    whitespace, validates/defaults location, and cleans description text.
    """
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
