"""Listing watcher: checks Gameflip (and optionally Eldorado) for specific items
under a price limit and emails you. Alert-only: it never buys anything.

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
    """Match on the FULL item name, so lookalikes such as
    'Frozen Horns of the Dangerous One' don't count."""
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
    out, seen = [], set()

    # 1) Listing cards: <a href=".../oi/<id>"> whose text holds title, seller and "$12.34"
    for a in soup.find_all("a", href=re.compile(r"/oi/")):
        href = a.get("href", "")
        text = " ".join(a.get_text(" ", strip=True).split())
        m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", text)
        oid = href.rstrip("/").split("/")[-1]
        if not m or oid in seen:
            continue
        seen.add(oid)
        out.append({
            "source": "Eldorado",
            "id": oid,
            "title": text,
            "price": float(m.group(1).replace(",", "")),
            "url": ("https://www.eldorado.gg" + href) if href.startswith("/") else href,
        })

    # 2) Fallback: listing data embedded as JSON in script tags
    if not out:
        found = []
        for s in soup.find_all("script"):
            txt = (s.string or "").strip()
            if txt.startswith("{") or txt.startswith("["):
                try:
                    _walk(json.loads(txt), found)
                except json.JSONDecodeError:
                    pass
        out = [{
            "source": "Eldorado",
            "id": i,
            "title": t,
            "price": p,
            "url": f"https://www.eldorado.gg/offer/{i}",
        } for i, t, p in found]
    if not out:
        title = soup.title.string.strip() if soup.title and soup.title.string else None
        print(f"[Eldorado debug] HTTP {r.status_code}, {len(r.text)} bytes, page title: {title}")
    return out


# ---------------------------------------------------------------- email
def send_email(subject, body):
    """Returns True if the email was sent. Tries SSL (465), then STARTTLS (587)."""
    user = (os.environ.get("EMAIL_USER") or "").strip()
    # app passwords are shown with spaces; remove ALL whitespace (incl. non-breaking)
    pw = "".join((os.environ.get("EMAIL_APP_PASSWORD") or "").split())
    to = (os.environ.get("EMAIL_TO") or user).strip()
    if not (user and pw):
        print("Email secrets missing (EMAIL_USER / EMAIL_APP_PASSWORD).")
        return False
    print(f"Email: app password length = {len(pw)} (Google app passwords are 16)")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    print(f"Email: sender looks like an address: {'@' in user}, domain: {user.split('@')[-1]}")

    ctx = ssl.create_default_context()
    for label in ("ssl465", "starttls587"):
        stage = "connect"
        try:
            if label == "ssl465":
                s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30, context=ctx)
            else:
                s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
                stage = "starttls"
                s.starttls(context=ctx)
            with s:
                stage = "login"
                s.login(user, pw)
                stage = "send"
                s.send_message(msg)
            print(f"Email sent via {label}.")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"Email attempt {label} failed at stage '{stage}': {type(e).__name__}: {e}")
    return False


def send_ntfy(title, body):
    """Optional phone push via ntfy.sh (free). Needs secret NTFY_TOPIC."""
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8")[:3500],
            headers={
                "Title": title.encode("ascii", "ignore").decode(),
                "Priority": "high",
                "Tags": "moneybag",
            },
            timeout=20,
        )
        r.raise_for_status()
        print("Push (ntfy) sent.")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"Push (ntfy) failed: {type(e).__name__}: {e}")
        return False


def notify(subject, body):
    """Send via phone push and email; counts as sent if either worked."""
    ok_push = send_ntfy(subject, body)
    ok_mail = send_email(subject, body)
    return ok_push or ok_mail


# ---------------------------------------------------------------- main
def main():
    cfg = load_json(CONFIG_FILE, None)
    if not cfg:
        sys.exit("config.json missing or invalid")
    state = load_json(STATE_FILE, {"alerted": [], "fails": {}})
    alerted = set(state.get("alerted", []))
    fails = state.get("fails", {})

    sources = {"Gameflip": fetch_gameflip}
    if cfg.get("eldorado_enabled"):
        sources["Eldorado"] = fetch_eldorado
    hits = []
    pending = {}
    email_failed = False

    # One-off test email: change "test_email_id" in config.json to send another.
    test_id = str(cfg.get("test_email_id", ""))
    if test_id and state.get("test_email_id") != test_id:
        if notify("[Bot] Test alert", "If you can read this, alerts work."):
            state["test_email_id"] = test_id
        else:
            email_failed = True

    for name, fn in sources.items():
        ok_any = False
        for item in cfg["items"]:
            try:
                listings = fn(cfg, item)
                ok_any = True
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] {item['name']}: {e}")
                continue
            exact = [x["price"] for x in listings if norm(item["name"]) in norm(x["title"])]
            cheapest = f"${min(exact):.2f}" if exact else "none"
            print(f"[{name}] {item['name']}: {len(listings)} results, "
                  f"{len(exact)} exact-name, cheapest exact-name: {cheapest}")
            for l in listings:
                m = match_item(l["title"], cfg["items"])
                if not m:
                    continue
                if not (cfg.get("min_price_usd", 0) <= l["price"] <= m["max_usd"]):
                    continue
                key = f"{l['source']}:{l['id']}:{l['price']:.2f}"
                if key in alerted or key in pending:
                    continue
                pending[key] = True
                l["key"] = key
                l["limit"] = m["max_usd"]
                hits.append(l)
            time.sleep(1)  # be polite, avoid rate limits

        fails[name] = 0 if ok_any else fails.get(name, 0) + 1
        if fails[name] == FAIL_ALERT_AFTER:
            notify(
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
        subject = f"[Bot] {len(hits)} deal(s): ${hits[0]['price']:.0f} {hits[0]['title']}"
        if notify(subject, body):
            alerted.update(h["key"] for h in hits)  # only remember them once sent
        else:
            email_failed = True  # not saved as alerted, so it retries next run

    state["alerted"] = sorted(alerted)[-2000:]
    state["fails"] = fails
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    print(f"Done. {len(hits)} new hit(s).")
    if email_failed:
        sys.exit("No alert channel worked (see log above). Will retry next run.")


if __name__ == "__main__":
    main()
