"""Diagnostic: dump the raw price history for one resolved market so the user
can see every data point with a real timestamp and decide for themselves.

Run: python verify_data.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
EVENT_SLUG = "will-trump-publicly-insult-someone-on"


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


def main() -> int:
    target_question_substr = sys.argv[1] if len(sys.argv) > 1 else "April 12, 2026"

    r = requests.get(f"{GAMMA}/events", params={"slug": EVENT_SLUG}, timeout=15)
    r.raise_for_status()
    events = r.json()
    if not events:
        print("Event not found")
        return 1
    event = events[0]

    target = None
    for m in event.get("markets", []):
        if target_question_substr.lower() in (m.get("question") or "").lower():
            target = m
            break
    if target is None:
        print(f"No market matched {target_question_substr!r}")
        return 1

    print(f"Market:     {target.get('question')}")
    print(f"Slug:       {target.get('slug')}")
    print(f"URL:        https://polymarket.com/event/{EVENT_SLUG}/{target.get('slug')}")
    print(f"closed:     {target.get('closed')}")
    print(f"endDate:    {target.get('endDate')}")
    print(f"volumeClob: {target.get('volumeClob')}")
    print(f"volumeClob24hr: {target.get('volumeClob24hr')}")
    print(f"liquidity:  {target.get('liquidity')}")
    print()

    tid = yes_token_id(target)
    print(f"YES token_id: {tid}")
    print()

    r2 = requests.get(
        f"{CLOB}/prices-history",
        params={"market": tid, "interval": "all", "fidelity": 60},
        timeout=30,
    )
    r2.raise_for_status()
    hist = r2.json().get("history", [])
    print(f"Price history points: {len(hist)} (hourly fidelity)")
    print()
    print(f"{'#':>3}  {'UTC timestamp':<20}  {'ET date':<20}  {'YES price':>10}")
    for i, point in enumerate(hist):
        t = float(point["t"])
        p = float(point["p"])
        utc = datetime.fromtimestamp(t, tz=timezone.utc)
        # ET = UTC-4 during EDT (April)
        et = datetime.fromtimestamp(t - 4 * 3600, tz=timezone.utc)
        print(f"{i:>3}  {utc.strftime('%Y-%m-%d %H:%M'):<20}  {et.strftime('%Y-%m-%d %H:%M'):<20}  {p:>10.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
