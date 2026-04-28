"""For each resolved daily-insult market, list the timestamp of the first
time the YES price crossed specific thresholds. Lets us see if dips
cluster in time (same date/hour across multiple markets) or are spread out.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SLUG = "will-trump-publicly-insult-someone-on"

THRESHOLDS = [0.75, 0.60, 0.50]


def yes_token_id(m: dict) -> str | None:
    outcomes = m.get("outcomes")
    tokens = m.get("clobTokenIds")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    for o, t in zip(outcomes or [], tokens or []):
        if str(o).strip().lower() == "yes":
            return t
    return None


def first_hit(hist, threshold):
    for t, p in hist:
        if p <= threshold:
            return t, p
    return None


def main():
    r = requests.get(f"{GAMMA}/events", params={"slug": SLUG}, timeout=15)
    events = r.json()
    event = events[0]

    rows = []
    for m in event.get("markets", []):
        if not m.get("closed"):
            continue
        q = m.get("question", "")
        tid = yes_token_id(m)
        if not tid:
            continue
        r2 = requests.get(
            f"{CLOB}/prices-history",
            params={"market": tid, "interval": "all", "fidelity": 60},
            timeout=30,
        )
        hist = [(float(h["t"]), float(h["p"])) for h in r2.json().get("history", [])]
        hits = {}
        for thr in THRESHOLDS:
            h = first_hit(hist, thr)
            if h:
                t, p = h
                et = datetime.fromtimestamp(t - 4 * 3600, tz=timezone.utc)
                hits[thr] = (et, p)
            else:
                hits[thr] = None
        rows.append((q, hits))

    print(f"{'Market':<45}  {'first <=75':<22}  {'first <=60':<22}  {'first <=50':<22}")
    print("-" * 120)
    for q, hits in rows:
        q_short = q.replace("Will Donald Trump publicly insult someone on ", "").replace(", 2026?", "")
        cols = [f"{q_short:<45}"]
        for thr in THRESHOLDS:
            h = hits[thr]
            if h is None:
                cols.append(f"{'(never)':<22}")
            else:
                et, p = h
                cols.append(f"{et.strftime('%b %d %H:%M ET'):<16}@{p:>4.2f}")
        print("  ".join(cols))


if __name__ == "__main__":
    main()
