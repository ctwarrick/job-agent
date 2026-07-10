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


def _degradation_facts(
    failed_sources: list[dict] | None,
    scoring: dict | None,
    partial_sources: list[dict] | None = None,
) -> dict | None:
    """Select the structured degradation facts shared by the email notice and
    the run-row detail, or None when the run was clean.

    Centralizing the "which facts and what counts" decision keeps the three
    renderings (text notice, HTML notice, run-row detail) from drifting -- e.g.
    a new cap_reason flows to all three without edits.

    Args:
        failed_sources: List of {source, company_slug, error} dicts, or None.
        scoring: The score stage's {scored, remaining, cap_reason}, or None.
        partial_sources: List of partially-fetched source dicts
            {source, company_slug, new, skipped, truncated, persistent}, or a
            budget-deferred board {source, company_slug, reason=
            "budget_deferred"}, or None (FR-014, 007 FR-005).

    Returns:
        Dict with source_count, names (["source/company_slug", ...]), remaining,
        cap_reason, partial_count, partials (one normalized dict per ordinary
        partial source), deferred_count, and deferred (["source/company_slug",
        ...] for budget-deferred boards); or None when nothing degraded.
    """
    source_count = len(failed_sources) if failed_sources else 0
    names = [f"{f['source']}/{f['company_slug']}" for f in (failed_sources or [])]
    remaining = scoring.get("remaining", 0) if scoring else 0
    cap_reason = scoring.get("cap_reason") if scoring else None
    ordinary = [p for p in (partial_sources or []) if p.get("reason") != "budget_deferred"]
    deferred_sources = [p for p in (partial_sources or []) if p.get("reason") == "budget_deferred"]
    partials = [
        {
            "name": f"{p['source']}/{p['company_slug']}",
            "new": p.get("new", 0),
            "skipped": p.get("skipped", 0),
            "truncated": p.get("truncated", False),
            "persistent": p.get("persistent", False),
        }
        for p in ordinary
    ]
    partial_count = len(partials)
    deferred = [f"{p['source']}/{p['company_slug']}" for p in deferred_sources]
    deferred_count = len(deferred)
    if not source_count and remaining <= 0 and not partial_count and not deferred_count:
        return None
    return {
        "source_count": source_count,
        "names": names,
        "remaining": remaining,
        "cap_reason": cap_reason,
        "partial_count": partial_count,
        "partials": partials,
        "deferred_count": deferred_count,
        "deferred": deferred,
    }


def _degradation_messages(facts: dict) -> list[str]:
    """Render the notice's human sentences from facts (shared by text + HTML).

    The sentences are identical between the plain-text and HTML notices; the
    renderers differ only in escaping and wrapping. Naming each source is
    permitted (FR-007); the raw adapter error is never included (A2/FR-007).

    A partial source (per-item skips or backstop truncation) reads as
    "partially fetched ... queued for the next run"; one stuck past the
    staleness bound reads as a persistent "behind" degradation needing action
    (FR-014/FR-015) -- both distinct from a wholly "unreachable" failed source.
    A budget-deferred board (never dispatched because the fetch-stage budget
    expired) reads as "deferred by the fetch budget ... queued for the next
    run" -- a third, distinct category from both (007 FR-005).

    Args:
        facts: The dict from `_degradation_facts`.

    Returns:
        One sentence per present degradation fact.
    """
    messages = []
    n = facts["source_count"]
    if n:
        names = ", ".join(facts["names"])
        messages.append(
            f"{n} source{'s' if n != 1 else ''} unreachable; "
            f"their postings are not included this run: {names}"
        )
    remaining = facts["remaining"]
    if remaining > 0:
        cap = facts["cap_reason"]
        suffix = f" (cap={cap})" if cap else ""
        messages.append(
            f"{remaining} posting(s) left unscored this run and "
            f"queued for the next run{suffix}."
        )
    for p in facts.get("partials", []):
        if p["persistent"]:
            messages.append(
                f"{p['name']} has stayed behind for longer than the staleness "
                f"bound; raise the budget, tighten the filter, or drop it."
            )
        else:
            messages.append(
                f"{p['name']} partially fetched: {p['new']} stored, "
                f"{p['skipped']} skipped, the rest queued for the next run."
            )
    deferred_count = facts.get("deferred_count", 0)
    if deferred_count:
        deferred_names = ", ".join(facts.get("deferred", []))
        messages.append(
            f"{deferred_count} board{'s' if deferred_count != 1 else ''} deferred by "
            f"the fetch budget; queued for the next run: {deferred_names}"
        )
    return messages


def _degradation_text(
    failed_sources: list[dict] | None,
    scoring: dict | None,
    partial_sources: list[dict] | None = None,
) -> str:
    """Plain-text degraded-run notice, or "" when the run was clean.

    Args:
        failed_sources: List of {source, company_slug, error} dicts, or None.
        scoring: The score stage's {scored, remaining, cap_reason}, or None.
        partial_sources: Partially-fetched source dicts, or None (FR-014).

    Returns:
        A text block ending in a newline, or "" when there is nothing to report.
    """
    facts = _degradation_facts(failed_sources, scoring, partial_sources)
    if facts is None:
        return ""
    lines = [f"  • {m}" for m in _degradation_messages(facts)]
    return "\n".join(["Degraded run — partial results", "-" * 40, *lines, ""]) + "\n"


def _degradation_html(
    failed_sources: list[dict] | None,
    scoring: dict | None,
    partial_sources: list[dict] | None = None,
) -> str:
    """HTML degraded-run notice, or "" when the run was clean.

    Escapes each whole message (covers source/company_slug), so a slug with
    HTML-special chars cannot break out of the markup (A2/FR-007).

    Args:
        failed_sources: List of {source, company_slug, error} dicts, or None.
        scoring: The score stage's {scored, remaining, cap_reason}, or None.
        partial_sources: Partially-fetched source dicts, or None (FR-014).

    Returns:
        An HTML block, or "" when there is nothing to report.
    """
    facts = _degradation_facts(failed_sources, scoring, partial_sources)
    if facts is None:
        return ""
    lis = "".join(f"<li>{escape(m)}</li>" for m in _degradation_messages(facts))
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


def main(
    failed_sources: list[dict] | None = None,
    scoring: dict | None = None,
    partial_sources: list[dict] | None = None,
) -> bool:
    """Send the digest (or a no-matches notice) and report confirmed-send status.

    When the orchestrator passes degradation context, a notice naming the
    failed sources (FR-005), any partially-fetched sources (FR-014), and any
    scoring backlog (FR-020) is prepended to both the text and HTML bodies,
    including on an otherwise empty day — so the degradation is visible by
    morning coffee even when no postings qualified. With the default (no)
    arguments, behavior is unchanged, preserving the standalone
    `jobagent-digest` entry point.

    Args:
        failed_sources: Optional list of {source, company_slug, error} dicts
            from fetch.main().
        scoring: Optional {scored, remaining, cap_reason} signal from
            score.main().
        partial_sources: Optional list of partially-fetched source dicts from
            fetch.main() (skips / backstop truncation / persistent staleness).

    Returns:
        True only once `_send` has returned without raising — callers (main.py)
        commit digest_sent_at and the run's outcome strictly after that
        confirmation (FR-004, data-model.md "Validation rules").
    """
    rows = _fetch_digest()
    deg_text = _degradation_text(failed_sources, scoring, partial_sources)
    deg_html = _degradation_html(failed_sources, scoring, partial_sources)

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


def _cli() -> None:
    """Console-script entry point (jobagent-digest).

    Runs the stage and discards main()'s return value (a bool indicating
    confirmed send, which exists for in-process orchestration by main.py) so a
    successful run exits 0: the hatchling wrapper does ``sys.exit(main())`` and
    ``sys.exit(True)`` exits 1.
    """
    main()


if __name__ == "__main__":
    _cli()
