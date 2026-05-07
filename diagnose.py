"""Live diagnostic: connect with the same creds the bot uses, list current
state (balance, open orders, positions), and show exactly why each
candidate tier would or wouldn't get an order.

Run locally:  python diagnose.py
Requires `.env` to be present with the same secrets as GH Actions.

This calls NOTHING destructive: no order create, no order cancel.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

import bot


def main() -> int:
    load_dotenv()
    cfg = bot.load_config()
    print(f"== Diagnose for order_mode={cfg.order_mode!r} ==\n")

    # Build the real client — same path bot.py takes
    try:
        client = bot.build_client()
    except Exception as e:
        print(f"FATAL: build_client failed: {e}")
        return 1
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS")
    print(f"Funder: {funder}")

    # Balance
    try:
        balance = bot.get_balance_usdc(client)
        print(f"USDC balance: ${balance:.4f}")
    except Exception as e:
        print(f"FATAL: balance fetch failed: {e}")
        return 1

    if balance < cfg.min_order_usdc:
        print(f"-> balance < min_order_usdc ({cfg.min_order_usdc}). Bot would exit early.")
        return 0

    # Open orders globally
    print("\n--- OPEN ORDERS (across all markets) ---")
    open_orders = bot.list_open_orders(client)
    print(f"Total open orders: {len(open_orders)}")
    for o in open_orders:
        print(f"  side={o.side} price={o.price:.4f} size_remaining={o.size_remaining:.2f} usdc=${o.remaining_usdc:.2f} token={o.token_id[:14]}... id={o.order_id[:14]}")

    # Event + candidates
    print("\n--- EVENT DISCOVERY ---")
    event = bot.discover_event(cfg.event_slug_keywords, cfg.event_slug_fallback)
    raw_markets = event.get("markets", []) or []
    print(f"Event '{event.get('title')}'  slug={event.get('slug')!r}  total markets={len(raw_markets)}")

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
        if mins_left is None or mins_left < cfg.skip_if_minutes_remaining_below:
            continue
        candidates.append({
            "question": q,
            "token_id": tid,
            "condition_id": m.get("conditionId", ""),
            "mins_left": mins_left,
        })
    candidates.sort(key=lambda c: c["mins_left"])
    active = candidates[: cfg.max_active_markets]
    print(f"Candidates after time-cutoff filter: {len(candidates)}; using top {len(active)}")

    # Per-market state
    print("\n--- PER-MARKET STATE ---")
    for c in active:
        token_orders = [o for o in open_orders if o.token_id == c["token_id"] and o.side == "BUY"]
        ask = bot.best_ask(client, c["token_id"])
        size, avg = bot.get_yes_position(funder, c["condition_id"])
        constraints = bot.get_market_constraints(c["condition_id"])
        print(f"\n[{c['question']}]  mins_left={c['mins_left']:.0f}")
        print(f"  best_ask={ask}  filled_position={size:.2f}sh @ avg {avg:.4f}")
        print(f"  constraints: tick={constraints['minimum_tick_size']}  min_size={constraints['minimum_order_size']}sh  neg_risk={constraints['neg_risk']}")
        print(f"  open BUYs on this token: {len(token_orders)}")
        for o in token_orders:
            print(f"    @ {o.price:.4f}  remaining {o.size_remaining:.2f}sh = ${o.remaining_usdc:.2f}")

        # What the bot WOULD do for each tier
        for tier in cfg.entry_ladder:
            tier_usdc = tier.pct_of_balance * balance
            verdict = []
            if tier_usdc < cfg.min_order_usdc:
                verdict.append(f"tier_usdc ${tier_usdc:.2f} < min_order ${cfg.min_order_usdc}")
            if ask is None:
                verdict.append("no ask")
            elif ask <= tier.price:
                verdict.append(f"ask {ask:.4f} <= tier {tier.price:.2f} -> ANTI-CROSS skip")
            desired_shares = bot.round_down(tier_usdc / tier.price, cfg.order_decimals)
            if desired_shares < constraints["minimum_order_size"]:
                verdict.append(f"size {desired_shares:.2f} < min {constraints['minimum_order_size']:.0f}")
            existing = bot.find_matching_order(token_orders, tier.price)
            if existing:
                verdict.append(f"existing @ {existing.price:.4f} ({existing.size_remaining:.2f}sh) -> KEEP")

            verdict_s = " | ".join(verdict) if verdict else f"WOULD PLACE {desired_shares:.2f}sh @ {tier.price:.2f} = ${tier_usdc:.2f}"
            print(f"  tier <={tier.price:.2f} ({tier.pct_of_balance*100:.0f}% = ${tier_usdc:.2f}): {verdict_s}")

    # Try a single dry order placement to verify create_order works at all
    print("\n--- TEST CREATE_ORDER (signs but does NOT post) ---")
    if active:
        c = active[0]
        try:
            from py_clob_client.clob_types import PartialCreateOrderOptions, OrderArgs
            from py_clob_client.order_builder.constants import BUY
            ts = c["token_id"] if "token_id" in c else None
            args = OrderArgs(
                token_id=ts,
                price=0.70,
                size=10.0,
                side=BUY,
            )
            opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
            signed = client.create_order(args, opts)
            print(f"  create_order OK on {c['question'][:50]}... signed_keys={list(vars(signed).keys()) if hasattr(signed,'__dict__') else type(signed).__name__}")
        except Exception as e:
            print(f"  create_order FAILED: {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
