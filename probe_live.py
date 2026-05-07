"""Snapshot all open daily sub-markets in the event right now: current best
ask, days until EOD, whether the new conservative ladder would fire, and
the order-book depth at relevant tier prices.

Pure read; no trading. Useful for sanity-checking the live state.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
import yaml


GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_json_field(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def yes_token_id(m):
    outcomes = parse_json_field(m.get("outcomes"))
    tokens = parse_json_field(m.get("clobTokenIds"))
    for o, t in zip(outcomes, tokens):
        if str(o).strip().lower() == "yes":
            return t
    return None


def parse_market_eod(question):
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", question or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        start = datetime(int(m.group(3)), mon, int(m.group(2)), 4, 0, tzinfo=timezone.utc)
        return start + timedelta(days=1)
    except Exception:
        return None


def book_summary(token_id):
    r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=15)
    r.raise_for_status()
    book = r.json() or {}
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    asks = sorted(asks, key=lambda x: float(x["price"]))
    bids = sorted(bids, key=lambda x: -float(x["price"]))
    best_ask = float(asks[0]["price"]) if asks else None
    best_bid = float(bids[0]["price"]) if bids else None
    return best_ask, best_bid, asks[:5], bids[:5]


def main():
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    ladder = cfg["entry_ladder"]
    cutoff_min = float(cfg.get("skip_if_minutes_remaining_below", 0))
    print(f"Live ladder (max per-market exposure cum {sum(t['pct_of_balance'] for t in ladder)*100:.0f}%):")
    for t in ladder:
        print(f"  <= {t['price']:.2f}  -> {t['pct_of_balance']*100:.0f}%")
    print(f"Time cutoff: skip if < {cutoff_min:.0f} min remaining (~{cutoff_min/60:.1f}h)")
    # Match how bot.py discovers the event: keywords with fallback
    keywords = cfg.get("event_slug_keywords") or []
    fallback = cfg.get("event_slug_fallback") or cfg.get("event_slug") or ""
    print(f"Keywords: {keywords}  fallback: {fallback}")
    print()

    import bot
    event = bot.discover_event([k.lower() for k in keywords], fallback)
    markets = event.get("markets", [])

    open_markets = []
    for m in markets:
        if m.get("closed") or m.get("archived") or m.get("active") is False:
            continue
        q = m.get("question") or ""
        eod = parse_market_eod(q)
        if eod is None:
            continue
        mins_left = (eod - datetime.now(timezone.utc)).total_seconds() / 60
        if mins_left < 0:
            continue  # already past EOD
        open_markets.append((mins_left, m, eod))

    open_markets.sort(key=lambda t: t[0])
    print(f"{len(open_markets)} OPEN daily sub-markets (sorted by time-to-EOD):\n")
    print(f"{'days_left':>9}  {'best_ask':>9} {'best_bid':>9}  {'spread':>7}  {'tiers_now':<22}  {'cutoff':<7}  question")

    deepest_qualifying = []
    for mins_left, m, eod in open_markets:
        q = m["question"]
        tid = yes_token_id(m)
        if not tid:
            continue
        try:
            best_ask, best_bid, _, _ = book_summary(tid)
        except Exception as e:
            print(f"  ! book error for {q!r}: {e}")
            continue
        days = mins_left / 1440
        spread = (best_ask - best_bid) if (best_ask and best_bid) else None
        tiers_active = [t for t in ladder if best_ask is not None and best_ask <= t["price"]]
        if tiers_active:
            tiers_str = " | ".join(f"<={t['price']:.2f}@{t['pct_of_balance']*100:.0f}%" for t in tiers_active)
        else:
            tiers_str = "(none — ask above all)"
        cutoff_state = "OK" if mins_left >= cutoff_min else "SKIPPED"
        ask_str = f"{best_ask:.4f}" if best_ask is not None else "  n/a"
        bid_str = f"{best_bid:.4f}" if best_bid is not None else "  n/a"
        spread_str = f"{spread:.4f}" if spread is not None else "  n/a"
        print(f"{days:>9.2f}  {ask_str:>9} {bid_str:>9}  {spread_str:>7}  {tiers_str:<22}  {cutoff_state:<7}  {q}")
        if tiers_active and mins_left >= cutoff_min:
            deepest_qualifying.append((q, best_ask, tiers_active[-1]))

    print()
    if deepest_qualifying:
        print(f"=> {len(deepest_qualifying)} markets WOULD currently fill at least tier 1 with the new ladder.")
        for q, ask, deepest in deepest_qualifying:
            print(f"   {q}: ask {ask:.3f} qualifies for tier <= {deepest['price']:.2f}")
    else:
        print("=> 0 markets currently qualify. Bot would not fire on this run.")
    print()

    # Show top of book on the cheapest 3 markets — gives a feel for liquidity at tier prices
    print("=== TOP-OF-BOOK detail for the 3 cheapest markets ===")
    sorted_by_ask = sorted(
        [(b_ask, q, tid) for mins_left, m, eod in open_markets
         if (q := m["question"]) and (tid := yes_token_id(m))
         and (b_ask := book_summary(tid)[0]) is not None],
        key=lambda x: x[0],
    )
    for b_ask, q, tid in sorted_by_ask[:3]:
        ask, bid, asks, bids = book_summary(tid)
        print(f"\n{q}  (best ask {ask:.4f}, best bid {bid:.4f})")
        print(f"  bids:")
        for b in bids:
            print(f"    {float(b['price']):.4f} x {float(b['size']):>12.2f}")
        print(f"  asks:")
        for a in asks:
            print(f"    {float(a['price']):.4f} x {float(a['size']):>12.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
