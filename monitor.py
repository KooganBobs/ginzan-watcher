#!/usr/bin/env python3
"""Watch the Ginzan Onsen winter page for a live reservation link.

Alerts by push (ntfy) and email (SMTP). State is kept in state.json.
All credentials come from environment variables, never from this file.
"""

import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

PAGES = [
    "https://www.ginzanonsen.jp/winter/2026/",
    "https://www.ginzanonsen.jp/winter/2026/en/index.html",
    "https://www.ginzanonsen.jp/winter/2026/zh/index.html",
]
# Wording used by "link is being prepared" notices. A drop in matches means
# the notice was removed or changed.
PLACEHOLDER = re.compile(
    r"準備中|准备中|being prepared|in preparation|under preparation|coming soon",
    re.IGNORECASE,
)
STATE_FILE = Path(__file__).with_name("state.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal reservation watcher; polite 10 min interval)",
    "Cache-Control": "no-cache",
}


def snapshot(url, html=None):
    """Fetch a page and reduce it to text hash, links and placeholder count."""
    if html is None:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    links = set()
    for tag, attr in (("a", "href"), ("iframe", "src"), ("form", "action")):
        for el in soup.find_all(tag):
            value = (el.get(attr) or "").strip()
            if not value or value.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            links.add(urljoin(url, value).split("#")[0])

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return {
        "placeholder_count": len(PLACEHOLDER.findall(text)),
        "links": sorted(links),
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def compare(url, old, new):
    """Return a list of (level, message) describing meaningful changes."""
    out = []
    if new["placeholder_count"] < old["placeholder_count"]:
        out.append(("high", f"The 'link being prepared' notice was removed on {url}"))
    added = sorted(set(new["links"]) - set(old["links"]))
    if added:
        host = urlparse(url).netloc
        external = [link for link in added if urlparse(link).netloc != host]
        level = "high" if external else "normal"
        out.append((level, f"New links on {url}: " + ", ".join(added[:5])))
    if new["hash"] != old["hash"] and not out:
        out.append(("normal", f"Page text changed on {url}"))
    return out


def send_push(title, message, priority="default", click=None, tags=None, forward_email=False):
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return False
    headers = {"Title": title, "Priority": priority}  # headers must be ASCII
    # ntfy can also forward the alert to email, so no SMTP password is needed
    email_to = (os.environ.get("EMAIL_TO") or "").strip()
    if forward_email and email_to and not os.environ.get("SMTP_PASS"):
        headers["Email"] = email_to
    if click:
        headers["Click"] = click
    if tags:
        headers["Tags"] = tags
    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError(f"ntfy returned {resp.status_code}: {resp.text[:300]}")
    return True


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to = os.environ.get("EMAIL_TO")
    if not all([host, user, password, to]):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT") or 587), timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    return True


def notify(title, body, priority="default", click=None, tags=None, email=True):
    """Send on every configured channel. Fail only if no channel succeeded."""
    delivered, errors = 0, []
    try:
        delivered += bool(send_push(title, body, priority, click, tags, forward_email=email))
    except Exception as exc:
        errors.append(f"push: {exc}")
    if email:
        try:
            delivered += bool(send_email(title, body))
        except Exception as exc:
            errors.append(f"email: {exc}")
    for err in errors:
        print("WARN notify failed ->", err)
    if not delivered:
        raise RuntimeError("No notification channel succeeded: " + "; ".join(errors or ["none configured"]))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def main():
    if os.environ.get("TEST_ALERT"):
        notify("Ginzan watcher: test alert", "Setup works. You will be alerted here.",
               priority="high", tags="white_check_mark")
        print("Test alert sent.")
        return

    state = load_state()
    pages = state.setdefault("pages", {})
    first_run = not pages
    alerts, fetched = [], 0

    for url in PAGES:
        try:
            snap = snapshot(url)
        except Exception as exc:
            print(f"WARN could not fetch {url}: {exc}")
            continue
        fetched += 1
        if url in pages:
            alerts += [(lvl, msg, url) for lvl, msg in compare(url, pages[url], snap)]
        pages[url] = snap

    if fetched == 0:
        sys.exit("All pages failed to load")

    now = datetime.now(timezone.utc)
    if alerts:
        high = any(level == "high" for level, _, _ in alerts)
        title = ("Ginzan Onsen: reservation link may be LIVE" if high
                 else "Ginzan Onsen: page updated")
        body = "\n".join(msg for _, msg, _ in alerts) + "\n\nBook now: " + PAGES[0]
        notify(title, body, priority="max" if high else "high",
               click=alerts[0][2], tags="rotating_light" if high else "eyes")
        print(title)
    elif first_run:
        notify("Ginzan watcher started", "Baseline saved. Watching 3 pages every 10 minutes.",
               priority="low", tags="eyes")

    last = state.get("last_heartbeat")
    if first_run or not last or now - datetime.fromisoformat(last) > timedelta(hours=24):
        try:
            send_push("Ginzan watcher: still running", "Daily heartbeat.", priority="min")
        except Exception as exc:
            print("WARN heartbeat failed:", exc)
        state["last_heartbeat"] = now.isoformat()

    save_state(state)  # saved only after alerts were delivered, so failures retry
    print(f"Checked {fetched}/{len(PAGES)} pages, {len(alerts)} alert(s).")


if __name__ == "__main__":
    main()
