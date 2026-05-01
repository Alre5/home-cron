"""Dry-run the limit-mode placement logic without sending any orders.

Loads the same config, discovers the event, lists candidates, and prints
exactly what orders the bot WOULD place — anti-cross filter, idempotency,
exposure caps applied. Pure read; no signing, no posting.

Useful before deploying to verify the new logic against live state.

Run: POLYMARKET_PRIVATE_KEY=... POLYMARKET_FUNDER_ADDRESS=... python dry_run.py
The private key is only used to derive the proxy address for positions
lookup — no orders are signed or sent. If you don't have it set, pass
--funder to provide just the proxy address (positions data api only).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import requests

import bot


def load_balance_from_data_api(proxy: str) -> float | None:
    """Fallback: ask the public data-api what the proxy's USDC balance is.
    Avoids needing the private key for a dry run."""
    try:
        r = requests.get(
            "https://data-api.polymarket.com/value",
            params={"user": proxy},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            data = data[0]
        # data-api returns {value, ...} where value is the active position
        # MTM, not the cash balance. So this is approximate.
        return float(data.get("value")) if data else None
    except Exception as e:
        print(f"  data-api value fetch failed: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=None,
                    help="Override balance (USDC) for the simulation. If omitted, "
                         "tries the real client; falls back to a default $50.")
    ap.add_argument("--funder", default=None,
                    help="Override proxy address for positions lookup. "
                         "If omitted, uses POLYMARKET_FUNDER_ADDRESS env var.")
    args = ap.parse_args()

    cfg = bot.load_config()
    print(f"== Dry-run for order_mode={cfg.order_mode!r} ==")
    print(f"Ladder: {[(t.price, t.pct_of_balance) for t in cfg.entry_ladder]}")
    print(f"Time cutoff: {cfg.skip_if_minutes_remaining_below:.0f} min "
          f"(~{cfg.skip_if_minutes_remaining_below/60:.1f}h)")
    print(f"Max total exposure: {cfg.max_total_exposure_pct*100:.0f}% of balance")
    print(f"Max active markets: {cfg.max_active_markets}")
    print()

    # Balance + funder
    funder = args.funder or os.environ.get("POLYMARKET_FUNDER_ADDRESS")
    if not funder:
        print("ERROR: --funder or POLYMARKET_FUNDER_ADDRESS required.")
        return 1

    balance = args.balance
    if balance is None:
        try:
            client_for_balance = bot.build_client()
            balance = bot.get_balance_usdc(client_for_balance)
            print(f"Balance from CLOB: ${balance:.4f}")
        except Exception as e:
            print(f"  could not load real balance ({e}), using $50.00 default")
            balance = 50.0
    print(f"Simulated balance: ${balance:.2f}")
    print()

    # Event + markets
    event = bot.discover_event(cfg.event_slug_keywords, cfg.event_slug_fallback)
    raw_markets = event.get("markets", []) or []
    print(f"Event '{event.get('title', event.get('slug'))}' "
          f"-> {len(raw_markets)} sub-markets")

    # Build candidates
    candidates = []
    for m in raw_markets:
        if not bot.market_is_tradeable(m):
            continue
        q = m.get("question") or ""
        tid = bot.yes_token_id(m)
        if not tid:
            continue
        mins_left = bot.minutes_until_resolution_day_end(q)
        if mins_left is None:
            continue
        candidates.append({
            "question": q, "token_id": tid,
            "condition_id": m.get("conditionId", ""),
            "mins_left": mins_left,
        })
    candidates.sort(key=lambda c: c["mins_left"])

    print(f"\n{len(candidates)} tradeable markets total. Time-cutoff filter "
          f"(>= {cfg.skip_if_minutes_remaining_below:.0f} min):")
    passed = [c for c in candidates if c["mins_left"] >= cfg.skip_if_minutes_remaining_below]
    print(f"  -> {len(passed)} pass cutoff, top {cfg.max_active_markets} taken")
    active = passed[: cfg.max_active_markets]

    # Open orders + positions snapshot — needs CLOB creds OR we skip
    try:
        client = bot.build_client()
        open_orders = bot.list_open_orders(client)
        print(f"\nOpen orders globally: {len(open_orders)}")
    except Exception as e:
        print(f"\nCould not auth CLOB ({e}); proceeding with empty open-orders snapshot.")
        open_orders = []
        client = None

    cap_total = cfg.max_total_exposure_pct * balance
    print(f"Cap total: ${cap_total:.2f}")

    global_committed = sum(o.remaining_usdc for o in open_orders if o.side == "BUY")
    print(f"Already committed in open BUYs: ${global_committed:.2f}")

    def public_best_ask(token_id: str) -> float | None:
        try:
            r = requests.get("https://clob.polymarket.com/book", params={"token_id": token_id}, timeout=15)
            r.raise_for_status()
            book = r.json() or {}
            asks = book.get("asks") or []
            if not asks:
                return None
            return min(float(a["price"]) for a in asks)
        except Exception as e:
            print(f"  public book fetch failed for {token_id[:10]}: {e}")
            return None

    # Pre-fetch ask + per-token state
    for c in active:
        c["token_orders"] = [o for o in open_orders if o.token_id == c["token_id"] and o.side == "BUY"]
        if client is not None:
            c["ask"] = bot.best_ask(client, c["token_id"])
        else:
            c["ask"] = public_best_ask(c["token_id"])
        size, avg = bot.get_yes_position(funder, c["condition_id"])
        c["filled_size"] = size
        c["filled_avg"] = avg
        c["market_committed"] = sum(o.remaining_usdc for o in c["token_orders"]) + size * avg
        c["constraints"] = bot.get_market_constraints(c["condition_id"])

    print(f"\nROUND-ROBIN PLAN (tier 1 on all -> tier 2 on all -> ...):\n")
    placed_total = 0
    skipped_anti_cross = 0
    skipped_already = 0
    skipped_cap = 0
    skipped_per_market_cap = 0
    per_market_cap_usdc = sum(t.pct_of_balance for t in cfg.entry_ladder) * balance
    plan_per_market: dict[str, list[str]] = {c["question"]: [] for c in active}

    for tier in cfg.entry_ladder:
        tier_usdc = tier.pct_of_balance * balance
        if tier_usdc < cfg.min_order_usdc:
            print(f"  -- tier <={tier.price:.2f}: tier_usdc ${tier_usdc:.2f} < min_order, ENTIRE TIER SKIPPED")
            continue
        print(f"  -- tier <={tier.price:.2f} (${tier_usdc:.2f} per market) --")
        for c in active:
            q = c["question"]
            ask = c["ask"]
            if ask is None:
                continue
            if c["market_committed"] >= per_market_cap_usdc * (1 - cfg.tier_fill_tolerance):
                plan_per_market[q].append(f"  tier <={tier.price:.2f}: per-market cap reached, skip")
                skipped_per_market_cap += 1
                continue
            if ask <= tier.price:
                plan_per_market[q].append(f"  tier <={tier.price:.2f}: ask {ask:.3f} <= tier, anti-cross skip")
                skipped_anti_cross += 1
                continue
            desired_size = bot.round_down(tier_usdc / tier.price, cfg.order_decimals)
            min_shares = c["constraints"]["minimum_order_size"]
            if desired_size < min_shares:
                plan_per_market[q].append(f"  tier <={tier.price:.2f}: size {desired_size:.2f} < market min {min_shares:.0f} shares, SKIP")
                continue
            existing = bot.find_matching_order(c["token_orders"], tier.price)
            if existing is not None:
                size_diff = abs(existing.size_remaining - desired_size) / max(desired_size, 1e-9)
                if size_diff < 0.10:
                    plan_per_market[q].append(f"  tier <={tier.price:.2f}: existing order matches, KEEP")
                    skipped_already += 1
                    continue
            order_cost = desired_size * tier.price
            if global_committed + order_cost > cap_total + 1e-6:
                plan_per_market[q].append(f"  tier <={tier.price:.2f}: would exceed cap ${cap_total:.2f}, SKIP")
                skipped_cap += 1
                continue
            plan_per_market[q].append(f"  tier <={tier.price:.2f}: PLACE {desired_size:.2f} sh = ${order_cost:.2f}")
            placed_total += 1
            global_committed += order_cost
            c["market_committed"] += order_cost

    print(f"\n{'q':<60} {'mins/EOD':>10} {'ask':>7}")
    for c in active:
        days = c["mins_left"] / 1440
        ask_str = f"{c['ask']:.3f}" if c["ask"] is not None else "n/a"
        print(f"{c['question'][:59]:<60} {days:>9.2f}d {ask_str:>7}")
        for line in plan_per_market[c["question"]]:
            print(line)
    print(f"\nDRY-RUN SUMMARY: would_place={placed_total} skipped_anti_cross={skipped_anti_cross} "
          f"skipped_already_open={skipped_already} skipped_cap={skipped_cap} skipped_per_market_cap={skipped_per_market_cap}")
    print(f"Final projected committed: ${global_committed:.2f} / cap ${cap_total:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
