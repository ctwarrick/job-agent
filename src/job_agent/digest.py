"""Digest stage.

Selects high-fit, low-category-risk postings you haven't been emailed yet,
sends a single grouped digest over SMTP, and marks them sent so tomorrow's
run won't repeat them. Your applications.status field is left untouched —
"already emailed" is tracked separately on postings.digest_sent_at.

Env (all required unless noted):
  SMTP_HOST            e.g. smtp.gmail.com
  SMTP_PORT            e.g. 587   (STARTTLS) or 465 (SSL)
  SMTP_USER            login / from address
  SMTP_PASS            password or app-password
  DIGEST_TO            recipient (defaults to SMTP_USER)
  DIGEST_MIN_SKILLS    optional, default 6
  DIGEST_MAX_RISK      optional, default 4
  DIGEST_DRY_RUN       optional, "1" prints instead of sending
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape

from . import store

MIN_SKILLS = int(os.environ.get("DIGEST_MIN_SKILLS", "6"))
MAX_RISK = int(os.environ.get("DIGEST_MAX_RISK", "4"))

QUERY = """
SELECT p.title, p.company, p.location, p.url,
       p.skills_fit, p.seniority_fit, p.category_risk, p.rationale,
       p.fingerprint
FROM postings p
JOIN applications a USING(fingerprint)
WHERE a.status = 'new'
  AND p.digest_sent_at IS NULL
  AND p.skills_fit IS NOT NULL
  AND p.skills_fit >= ?
  AND p.category_risk <= ?
ORDER BY p.skills_fit DESC, p.category_risk ASC
"""

BUCKET_LABEL = {
    "engineering": "Engineering",
    "tpm": "Technical PM / Program",
    "eng_mgmt": "Engineering Management",
    "other": "Other",
}


def _fetch_digest(path: str | None = None) -> list[dict]:
    """Fetch qualifying postings and merge in LLM-scoring rationale.

    Runs QUERY to get postings with skills_fit >= MIN_SKILLS,
    category_risk <= MAX_RISK, status='new', and not yet digest-sent.
    Deserializes each posting's rationale JSON and merges it into the row.

    Args:
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        List of posting dicts, each with posting and rationale fields.
    """
    with store.connect(path) as conn:
        rows = conn.execute(QUERY, (MIN_SKILLS, MAX_RISK)).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["rationale"]) if r["rationale"] else {}
        out.append({**dict(r), **meta})
    return out


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    """Group posting dicts by bucket.

    Args:
        rows: List of posting dicts with a 'bucket' field.

    Returns:
        Dict mapping bucket name (or 'other' if missing) to list of rows.
    """
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("bucket") or "other", []).append(r)
    return groups


def _render_text(groups: dict[str, list[dict]]) -> str:
    """Render posting groups as plain-text digest.

    Args:
        groups: Dict mapping bucket name to list of posting dicts.

    Returns:
        Plain-text digest with bucket headers and posting details.
    """
    lines = ["Your job digest", "=" * 40, ""]
    for bucket, rows in groups.items():
        lines.append(f"## {BUCKET_LABEL.get(bucket, bucket)} ({len(rows)})")
        for r in rows:
            comp = r.get("comp_flag", "unknown")
            comp_tag = "  [LOWBALL]" if comp == "lowball" else ""
            lines.append(f"  • {r['title']} — {r['company']} ({r['location']}){comp_tag}")
            lines.append(f"    fit {r['skills_fit']}/10  risk {r['category_risk']}/10")
            if r.get("trajectory_note"):
                lines.append(f"    {r['trajectory_note']}")
            lines.append(f"    {r['url']}")
            lines.append("")
    return "\n".join(lines)


def _render_html(groups: dict[str, list[dict]]) -> str:
    """Render posting groups as HTML digest.

    Args:
        groups: Dict mapping bucket name to list of posting dicts.

    Returns:
        HTML digest with bucket headers and styled posting details.
    """
    parts = [
        '<div style="font-family:system-ui,sans-serif;max-width:680px">',
        "<h2>Your job digest</h2>",
    ]
    for bucket, rows in groups.items():
        parts.append(
            f"<h3>{escape(BUCKET_LABEL.get(bucket, bucket))} "
            f"<span style='color:#888;font-weight:400'>({len(rows)})</span></h3>"
        )
        for r in rows:
            lowball = r.get("comp_flag") == "lowball"
            comp_tag = (
                (
                    " <span style='background:#fee;color:#c00;padding:1px 6px;"
                    "border-radius:3px;font-size:12px'>LOWBALL</span>"
                )
                if lowball
                else ""
            )
            note = escape(r.get("trajectory_note") or "")
            parts.append(
                "<div style='margin:0 0 16px;padding:10px 14px;border-left:3px solid #4a7'>"
                f"<div style='font-size:16px'><a href='{escape(r['url'])}' "
                f"style='color:#1a5;text-decoration:none'><b>{escape(r['title'])}</b></a>"
                f"{comp_tag}</div>"
                f"<div style='color:#555;font-size:14px'>{escape(r['company'])} · "
                f"{escape(r['location'])}</div>"
                f"<div style='color:#888;font-size:13px;margin:3px 0'>"
                f"fit {r['skills_fit']}/10 · category risk {r['category_risk']}/10</div>"
                f"<div style='font-size:13px;color:#444'>{note}</div>"
                "</div>"
            )
    parts.append("</div>")
    return "".join(parts)


def _send(subject: str, text: str, html: str) -> None:
    """Send an email via SMTP.

    Reads SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS from env. Uses
    SMTP_SSL for port 465, STARTTLS otherwise. Raises on send failure.

    Args:
        subject: Email subject line.
        text: Plain-text body.
        html: HTML body (added as alternative).

    Raises:
        smtplib.SMTPException: On authentication or send failure.
    """
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("DIGEST_TO", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, password)
            s.send_message(msg)


def _mark_sent(fingerprints: list[str], path: str | None = None) -> None:
    """Mark postings as sent by setting digest_sent_at timestamp.

    Args:
        fingerprints: List of posting fingerprints.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").
    """
    now = datetime.now(timezone.utc).isoformat()
    with store.connect(path) as conn:
        conn.executemany(
            "UPDATE postings SET digest_sent_at=? WHERE fingerprint=?",
            [(now, fp) for fp in fingerprints],
        )


def main() -> bool:
    """Send the digest (or a no-matches notice) and report confirmed-send status.

    Returns True only once `_send` has returned without raising — callers
    (main.py) commit digest_sent_at and the run's success outcome strictly
    after that confirmation (FR-004, data-model.md "Validation rules").
    """
    rows = _fetch_digest()

    if not rows:
        subject = "Job digest — no new matches"
        text = "No new qualifying postings today.\n"
        html = "<p>No new qualifying postings today.</p>"
    else:
        groups = _group(rows)
        text = _render_text(groups)
        html = _render_html(groups)
        subject = f"Job digest — {len(rows)} new match{'es' if len(rows) != 1 else ''}"

    if os.environ.get("DIGEST_DRY_RUN") == "1":
        print(subject)
        print(text)
        return True

    try:
        _send(subject, text, html)
    except Exception as e:
        print(f"send failed: {e}")
        return False

    if rows:
        _mark_sent([r["fingerprint"] for r in rows])
        print(f"Sent {len(rows)} postings.")
    else:
        print("Sent no-new-matches notice.")

    return True


if __name__ == "__main__":
    main()
