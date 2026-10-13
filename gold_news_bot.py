#!/usr/bin/env python3
"""
XAUUSD News Bot
- Pulls headlines (Google News queries + direct RSS feeds)
- Pulls economic calendar (ForexFactory weekly JSON)
- Scores each item by gold impact, de-duplicates, sends alerts
  to Telegram (or prints to console if Telegram is not configured)

- Optional AI summary: a short "what it means for gold" note on each alert
  (needs ANTHROPIC_API_KEY; without it, alerts are sent without a summary)

Setup:
    pip install requests
    export TELEGRAM_TOKEN="123456:ABC..."      # from @BotFather
    export TELEGRAM_CHAT_ID="123456789"        # from @userinfobot
    export ANTHROPIC_API_KEY="sk-ant-..."      # from console.anthropic.com (for AI summaries)
    python gold_news_bot.py                    # runs forever, polls every 5 min
    python gold_news_bot.py --once             # single run (for cron / GitHub Actions)
"""
import os
import re
import sys
import json
import time
import html
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AI_MODEL = "claude-haiku-5-5"  # fast and cheap; use "claude-sonnet-5-5" for deeper takes
AI_MIN_SCORE = 4               # only summarize headlines scoring at least this
AI_MAX_PER_RUN = 10            # cap on AI calls per polling cycle (controls cost)
POLL_MINUTES = 5
MIN_SCORE = 3                 # raise to get fewer, higher-impact alerts
MAX_AGE_HOURS = 6             # ignore headlines older than this
CALENDAR_CURRENCIES = {"USD"}  # add "EUR", "CNY", "GBP", "JPY" if you want
CALENDAR_IMPACTS = {"High"}    # add "Medium" for more events
CALENDAR_WARN_MINUTES = 30     # "upcoming" alert this many minutes before
SEEN_FILE = "seen.json"
IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {"User-Agent": "Mozilla/5.0 (XAUUSD-news-bot)"}

# Google News search queries (each one is a news "topic" for gold)
GOOGLE_QUERIES = [
    "gold price XAUUSD",
    "Federal Reserve interest rate decision",
    "Fed Powell speech",
    "US CPI inflation report",
    "US PCE inflation",
    "nonfarm payrolls jobs report",
    "US dollar index DXY",
    "US treasury yields",
    "central bank gold buying",
    "gold ETF holdings SPDR",
    "Middle East war escalation",
    "Russia Ukraine ceasefire",
    "Trump tariffs trade war",
    "China gold demand PBOC",
    "India gold demand import duty",
    "oil prices OPEC",
    "US debt ceiling government shutdown",
]

# Direct RSS feeds (URLs can change; remove any that fail)
RSS_FEEDS = {
    "Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "Kitco": "https://www.kitco.com/rss/KitcoNews.xml",
}

# Impact keywords: category -> (weight, [keywords])
IMPACT = {
    "FED / RATES": (4, ["fomc", "rate cut", "rate hike", "fed funds", "powell",
                        "dot plot", "fed minutes", "federal reserve", "jackson hole",
                        "hawkish", "dovish", "quantitative"]),
    "INFLATION": (4, ["cpi", "pce", "ppi", "inflation", "core inflation"]),
    "JOBS / GROWTH": (3, ["nonfarm", "nfp", "payrolls", "jobless claims", "unemployment",
                          "adp", "jolts", "gdp", "ism", "retail sales", "recession"]),
    "USD / YIELDS": (3, ["dollar index", "dxy", "treasury yield", "10-year yield",
                         "real yield", "greenback"]),
    "GEOPOLITICS": (3, ["war", "missile", "strike", "invasion", "ceasefire", "sanctions",
                        "iran", "israel", "gaza", "ukraine", "russia", "taiwan",
                        "north korea", "escalat", "nuclear"]),
    "TRADE / POLICY": (3, ["tariff", "trade war", "debt ceiling", "shutdown",
                           "executive order", "downgrade"]),
    "GOLD DEMAND": (3, ["central bank buying", "gold reserves", "pboc", "gold etf",
                        "spdr", "gold holdings", "gold import", "gold demand",
                        "world gold council"]),
    "OIL / RISK": (2, ["oil price", "opec", "crude", "stock market crash", "vix",
                       "risk-off", "safe haven", "bank failure", "banking crisis"]),
    "GOLD DIRECT": (2, ["gold price", "xauusd", "gold rallies", "gold falls",
                        "gold hits", "record high", "spot gold", "bullion"]),
}


# ---------------- HELPERS ----------------
def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    # keep file small
    items = list(seen)[-5000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(items, f)


def key_of(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def normalize(title):
    t = re.sub(r"\s+-\s+[^-]+$", "", title)  # drop " - Source Name"
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def send(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                      "disable_web_page_preview": True},
                timeout=20,
            )
            if r.status_code != 200:
                print("Telegram error:", r.text)
        except Exception as e:
            print("Telegram send failed:", e)
    else:
        print(message)
        print("-" * 50)


AI_SYSTEM = (
    "You are a concise gold (XAUUSD) market analyst. Given a news item, reply in at most "
    "3 short lines: line 1 = 'Bullish', 'Bearish' or 'Neutral' for gold plus a confidence "
    "(low/medium/high); line 2 = why, in plain words; line 3 = what to watch next. "
    "Use only what the item states plus well-known market relationships "
    "(e.g. stronger USD/higher yields usually pressure gold, safe-haven demand lifts it). "
    "Never invent figures or facts. If the item is unclear or irrelevant, say Neutral and why."
)

_ai_calls = 0


def ai_summary(item_text):
    """Return a short AI take on the item, or '' if AI is off, capped, or fails."""
    global _ai_calls
    if not ANTHROPIC_API_KEY or _ai_calls >= AI_MAX_PER_RUN:
        return ""
    _ai_calls += 1
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "max_tokens": 200,
                "system": AI_SYSTEM,
                "messages": [{"role": "user", "content": item_text}],
            },
            timeout=30,
        )
        r.raise_for_status()
        parts = r.json().get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    except Exception as e:
        print("[warn] AI summary failed:", e)
        return ""


def with_ai(message, item_text):
    s = ai_summary(item_text)
    return f"{message}\n\n🤖 {s}" if s else message


def score_text(text):
    low = text.lower()
    total, cats = 0, []
    for cat, (weight, words) in IMPACT.items():
        if any(w in low for w in words):
            total += weight
            cats.append(cat)
    return total, cats


def label(score):
    if score >= 7:
        return "🔴 HIGH"
    if score >= 4:
        return "🟠 MEDIUM"
    return "🟡 LOW"


def parse_pubdate(s):
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d
        except ValueError:
            continue
    return None


# ---------------- HEADLINES ----------------
def fetch_rss(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    # RSS 2.0
    for it in root.iter("item"):
        out.append({
            "title": html.unescape((it.findtext("title") or "").strip()),
            "link": (it.findtext("link") or "").strip(),
            "date": parse_pubdate(it.findtext("pubDate")),
        })
    # Atom
    ns = "{http://www.w3.org/2005/Atom}"
    for it in root.iter(ns + "entry"):
        link_el = it.find(ns + "link")
        out.append({
            "title": html.unescape((it.findtext(ns + "title") or "").strip()),
            "link": link_el.get("href") if link_el is not None else "",
            "date": parse_pubdate(it.findtext(ns + "updated")),
        })
    return out


def collect_headlines():
    sources = []
    for q in GOOGLE_QUERIES:
        url = ("https://news.google.com/rss/search?q="
               f"{quote_plus(q + ' when:1d')}&hl=en-US&gl=US&ceid=US:en")
        sources.append((f"News: {q}", url))
    for name, url in RSS_FEEDS.items():
        sources.append((name, url))

    items = []
    for name, url in sources:
        try:
            for it in fetch_rss(url):
                it["source"] = name
                items.append(it)
        except Exception as e:
            print(f"[warn] {name} failed: {e}")
    return items


def process_headlines(seen):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    alerts = []
    for it in collect_headlines():
        title = it["title"]
        if not title:
            continue
        if it["date"] and it["date"] < cutoff:
            continue
        k = key_of("h:" + normalize(title))
        if k in seen:
            continue
        score, cats = score_text(title)
        seen.add(k)  # mark seen even if below threshold
        if score < MIN_SCORE:
            continue
        when = it["date"].astimezone(IST).strftime("%d %b %H:%M IST") if it["date"] else ""
        alerts.append((score, title, (
            f"{label(score)} | {' + '.join(cats)}\n"
            f"{title}\n{when}\n{it['link']}"
        )))
    alerts.sort(key=lambda x: -x[0])
    # AI summaries go to the highest-scoring items first (capped by AI_MAX_PER_RUN)
    out = []
    for score, title, msg in alerts:
        out.append(with_ai(msg, f"Headline: {title}") if score >= AI_MIN_SCORE else msg)
    return out


# ---------------- ECONOMIC CALENDAR ----------------
def process_calendar(seen):
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        events = r.json()
    except Exception as e:
        print("[warn] calendar failed:", e)
        return []

    now = datetime.now(timezone.utc)
    alerts = []
    for ev in events:
        if ev.get("country") not in CALENDAR_CURRENCIES:
            continue
        if ev.get("impact") not in CALENDAR_IMPACTS:
            continue
        try:
            t = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        except Exception:
            continue

        title, ident = ev.get("title", ""), f"{ev.get('title')}|{ev.get('date')}"
        t_ist = t.astimezone(IST).strftime("%a %d %b, %H:%M IST")
        fc, prev, actual = ev.get("forecast", ""), ev.get("previous", ""), ev.get("actual", "")

        # 1) upcoming warning
        mins = (t - now).total_seconds() / 60
        k1 = key_of("up:" + ident)
        if 0 < mins <= CALENDAR_WARN_MINUTES and k1 not in seen:
            seen.add(k1)
            alerts.append(
                f"⏰ UPCOMING in {int(mins)} min | {ev['country']} {ev['impact']} impact\n"
                f"{title}\n{t_ist}\nForecast: {fc or '-'} | Previous: {prev or '-'}"
            )

        # 2) released with actual value
        k2 = key_of("rel:" + ident)
        if actual and k2 not in seen and now >= t:
            seen.add(k2)
            msg = (
                f"📊 RELEASED | {ev['country']} {ev['impact']} impact\n"
                f"{title}\nActual: {actual} | Forecast: {fc or '-'} | Previous: {prev or '-'}"
            )
            if not ANTHROPIC_API_KEY:
                msg += "\n(Hotter USD data than forecast is usually bearish gold; weaker is bullish.)"
            alerts.append(with_ai(msg, (
                f"US economic release: {title}. Actual: {actual}, Forecast: {fc or 'n/a'}, "
                f"Previous: {prev or 'n/a'}. Currency: {ev['country']}."
            )))
    return alerts


# ---------------- MAIN ----------------
def run_once():
    global _ai_calls
    _ai_calls = 0
    seen = load_seen()
    first_run = len(seen) == 0
    messages = process_calendar(seen) + process_headlines(seen)
    save_seen(seen)
    if first_run:
        # avoid flooding with old stories on the very first run
        messages = messages[:10]
    for m in messages:
        send(m)
        time.sleep(1)
    print(f"[{datetime.now(IST):%H:%M:%S}] sent {len(messages)} alerts")


def main():
    if "--once" in sys.argv:
        run_once()
        return
    send("✅ XAUUSD news bot started.")
    while True:
        try:
            run_once()
        except Exception as e:
            print("Run error:", e)
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    main()
