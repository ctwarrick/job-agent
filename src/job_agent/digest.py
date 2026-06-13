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


def _degradation_text(failed_sources: list[dict] | None, scoring: dict | None) -> str:
    """Build the plain-text degraded-run notice, or "" when the run was clean.

    Names each failed source (FR-005) and reports any scoring backlog
    (FR-020). The raw adapter error text is deliberately omitted -- it stays in
    the retained run logs, never in the email payload (A2/FR-007).

    Args:
        failed_sources: List of {source, company_slug, error} dicts, or None.
        scoring: The score stage's {scored, remaining, cap_reason} signal, or
            None.

    Returns:
        A text block ending in a newline, or "" when there is nothing to report.
    """
    lines = []
    if failed_sources:
        names = ", ".join(f"{f['source']}/{f['company_slug']}" for f in failed_sources)
        n = len(failed_sources)
        lines.append(
            f"  • {n} source{'s' if n != 1 else ''} unreachable; "
            f"their postings are not included this run: {names}"
        )
    if scoring and scoring.get("remaining", 0) > 0:
        remaining = scoring["remaining"]
        cap = scoring.get("cap_reason")
        suffix = f" (cap={cap})" if cap else ""
        lines.append(
            f"  • {remaining} posting(s) left unscored this run and "
            f"queued for the next run{suffix}."
        )
    if not lines:
        return ""
    return "\n".join(["Degraded run — partial results", "-" * 40, *lines, ""]) + "\n"


def _degradation_html(failed_sources: list[dict] | None, scoring: dict | None) -> str:
    """Build the HTML degraded-run notice, or "" when the run was clean.

    The HTML counterpart of `_degradation_text`; same source-naming and
    backlog-reporting rules, same omission of raw error text (A2/FR-007).

    Args:
        failed_sources: List of {source, company_slug, error} dicts, or None.
        scoring: The score stage's {scored, remaining, cap_reason} signal, or
            None.

    Returns:
        An HTML block, or "" when there is nothing to report.
    """
    items = []
    if failed_sources:
        names = ", ".join(escape(f"{f['source']}/{f['company_slug']}") for f in failed_sources)
        n = len(failed_sources)
        items.append(
            f"{n} source{'s' if n != 1 else ''} unreachable; "
            f"their postings are not included this run: {names}"
        )
    if scoring and scoring.get("remaining", 0) > 0:
        remaining = scoring["remaining"]
        cap = scoring.get("cap_reason")
        suffix = f" (cap={escape(str(cap))})" if cap else ""
        items.append(
            f"{escape(str(remaining))} posting(s) left unscored this run and "
            f"queued for the next run{suffix}."
        )
    if not items:
        return ""
    lis = "".join(f"<li>{it}</li>" for it in items)
    return (
        "<div style='margin:0 0 16px;padding:10px 14px;border-left:3px solid #c80;"
        "background:#fff8e1'>"
        "<b>Degraded run — partial results</b>"
        f"<ul style='margin:6px 0 0;padding-left:20px;color:#663'>{lis}</ul>"
        "</div>"
    )


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


def main(failed_sources: list[dict] | None = None, scoring: dict | None = None) -> bool:
    """Send the digest (or a no-matches notice) and report confirmed-send status.

    When the orchestrator passes degradation context, a notice naming the
    failed sources (FR-005) and any scoring backlog (FR-020) is prepended to
    both the text and HTML bodies, including on an otherwise empty day — so the
    degradation is visible by morning coffee even when no postings qualified.
    With the default (no) arguments, behavior is unchanged, preserving the
    standalone `jobagent-digest` entry point.

    Args:
        failed_sources: Optional list of {source, company_slug, error} dicts
            from fetch.main().
        scoring: Optional {scored, remaining, cap_reason} signal from
            score.main().

    Returns:
        True only once `_send` has returned without raising — callers (main.py)
        commit digest_sent_at and the run's outcome strictly after that
        confirmation (FR-004, data-model.md "Validation rules").
    """
    rows = _fetch_digest()
    deg_text = _degradation_text(failed_sources, scoring)
    deg_html = _degradation_html(failed_sources, scoring)

    if not rows:
        subject = "Job digest — no new matches"
        text = "No new qualifying postings today.\n"
        html = "<p>No new qualifying postings today.</p>"
    else:
        groups = _group(rows)
        text = _render_text(groups)
        html = _render_html(groups)
        subject = f"Job digest — {len(rows)} new match{'es' if len(rows) != 1 else ''}"

    if deg_text:
        text = deg_text + "\n" + text
    if deg_html:
        html = deg_html + html

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
