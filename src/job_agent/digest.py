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
import sys
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


def _fetch_digest(path: str = "jobs.db") -> list[dict]:
    with store.connect(path) as conn:
        rows = conn.execute(QUERY, (MIN_SKILLS, MAX_RISK)).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["rationale"]) if r["rationale"] else {}
        out.append({**dict(r), **meta})
    return out


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("bucket") or "other", []).append(r)
    return groups


def _render_text(groups: dict[str, list[dict]]) -> str:
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
    parts = ['<div style="font-family:system-ui,sans-serif;max-width:680px">',
             "<h2>Your job digest</h2>"]
    for bucket, rows in groups.items():
        parts.append(f"<h3>{escape(BUCKET_LABEL.get(bucket, bucket))} "
                     f"<span style='color:#888;font-weight:400'>({len(rows)})</span></h3>")
        for r in rows:
            lowball = r.get("comp_flag") == "lowball"
            comp_tag = (" <span style='background:#fee;color:#c00;padding:1px 6px;"
                        "border-radius:3px;font-size:12px'>LOWBALL</span>") if lowball else ""
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


def _mark_sent(fingerprints: list[str], path: str = "jobs.db") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.connect(path) as conn:
        conn.executemany(
            "UPDATE postings SET digest_sent_at=? WHERE fingerprint=?",
            [(now, fp) for fp in fingerprints],
        )


def main() -> None:
    rows = _fetch_digest()
    if not rows:
        print("No new qualifying postings; nothing to send.")
        return

    groups = _group(rows)
    text = _render_text(groups)
    html = _render_html(groups)
    subject = f"Job digest — {len(rows)} new match{'es' if len(rows) != 1 else ''}"

    if os.environ.get("DIGEST_DRY_RUN") == "1":
        print(subject)
        print(text)
        return

    try:
        _send(subject, text, html)
    except Exception as e:
        sys.exit(f"send failed: {e}")

    _mark_sent([r["fingerprint"] for r in rows])
    print(f"Sent {len(rows)} postings.")


if __name__ == "__main__":
    main()
