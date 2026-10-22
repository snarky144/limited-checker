"""Listing watcher: checks Gameflip and Eldorado for specific items under a price
limit and emails you. Alert-only: it never buys anything.

Env vars (set as GitHub Actions secrets):
  EMAIL_USER          Gmail address used to send
  EMAIL_APP_PASSWORD  Gmail app password (NOT your normal password)
  EMAIL_TO            where alerts go (can be the same address)
"""
import json
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

STATE_FILE = "seen.json"
CONFIG_FILE = "config.json"
FAIL_ALERT_AFTER = 12  # consecutive failed runs (~1 hour) before a "source broken" email

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def match_item(title, items):
    t = norm(title)
    for it in items:
        if norm(it["name"]) in t:
            return it
    return None


# ---------------------------------------------------------------- Gameflip
def fetch_gameflip(cfg, item):
    """Gameflip public listing search. Prices come back in cents."""
    params = {"status": "onsale", "term": item["name"], "limit": 50}
    r = requests.get(cfg["gameflip_api_url"], params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json().get("data", [])
    out = []
    for l in data:
        price = l.get("price")
        if price is None:
            continue
        out.append({
            "source": "Gameflip",
            "id": str(l.get("id")),
            "title": l.get("name", ""),
            "price": price / 100.0,
            "url": f"https://gameflip.com/item/{l.get('id')}",
        })
    return out


# ---------------------------------------------------------------- Eldorado
def _walk(node, found):
    """Walk embedded JSON and collect dicts that look like listings."""
    if isinstance(node, dict):
        title = node.get("offerTitle") or node.get("title") or node.get("name")
        price = None
        for k in ("pricePerUnit", "price", "priceUsd", "amount"):
            v = node.get(k)
            if isinstance(v, dict):
                v = v.get("amount") or v.get("value")
            if isinstance(v, (int, float)):
                price = float(v)
                break
        ident = node.get("id") or node.get("offerId")
        if isinstance(title, str) and price is not None and ident:
            found.append((str(ident), title, price))
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def fetch_eldorado(cfg, item):
    """Best effort: read listing data embedded in the search page.
    Eldorado has no public API, so this may need adjusting or may be blocked."""
    url = cfg["eldorado_search_url"].format(query=quote(item["name"]))
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    found = []
    for s in soup.find_all("script"):
        txt = (s.string or "").strip()
        if txt.startswith("{") or txt.startswith("["):
            try:
                _walk(json.loads(txt), found)
            except json.JSONDecodeError:
                pass
    return [{
        "source": "Eldorado",
        "id": i,
        "title": t,
        "price": p,
        "url": f"https://www.eldorado.gg/offer/{i}",
    } for i, t, p in found]


# ---------------------------------------------------------------- email
def send_email(subject, body):
    user = os.environ.get("EMAIL_USER")
    pw = os.environ.get("EMAIL_APP_PASSWORD")
    to = os.environ.get("EMAIL_TO", user)
    if not (user and pw):
        print("Email secrets missing; would have sent:\n", subject, "\n", body)
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.send_message(msg)


# ---------------------------------------------------------------- main
def main():
    cfg = load_json(CONFIG_FILE, None)
    if not cfg:
        sys.exit("config.json missing or invalid")
    state = load_json(STATE_FILE, {"alerted": [], "fails": {}})
    alerted = set(state.get("alerted", []))
    fails = state.get("fails", {})

    sources = {"Gameflip": fetch_gameflip, "Eldorado": fetch_eldorado}
    hits = []

    for name, fn in sources.items():
        ok_any = False
        for item in cfg["items"]:
            try:
                listings = fn(cfg, item)
                ok_any = True
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] {item['name']}: {e}")
                continue
            for l in listings:
                m = match_item(l["title"], cfg["items"])
                if not m:
                    continue
                if not (cfg.get("min_price_usd", 0) <= l["price"] <= m["max_usd"]):
                    continue
                key = f"{l['source']}:{l['id']}:{l['price']:.2f}"
                if key in alerted:
                    continue
                alerted.add(key)
                l["limit"] = m["max_usd"]
                hits.append(l)
            time.sleep(1)  # be polite, avoid rate limits

        fails[name] = 0 if ok_any else fails.get(name, 0) + 1
        if fails[name] == FAIL_ALERT_AFTER:
            send_email(
                f"[Bot] {name} has been failing for ~1 hour",
                f"{name} requests failed {FAIL_ALERT_AFTER} runs in a row. "
                "It is probably blocking GitHub's servers or changed its site. "
                "Check the Actions logs.",
            )

    if hits:
        hits.sort(key=lambda h: h["price"])
        lines = [
            f"${h['price']:.2f} (limit ${h['limit']}) | {h['source']} | {h['title']}\n{h['url']}"
            for h in hits
        ]
        body = (
            "\n\n".join(lines)
            + "\n\nCheck the seller's reviews and history before paying. "
            "Prices far below market are often scams."
        )
        send_email(f"[Bot] {len(hits)} deal(s): ${hits[0]['price']:.0f} {hits[0]['title']}", body)

    state["alerted"] = sorted(alerted)[-2000:]
    state["fails"] = fails
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    print(f"Done. {len(hits)} new hit(s).")


if __name__ == "__main__":
    main()
