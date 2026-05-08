"""Live diagnostic for the Polymarket bot.

Run locally:
  python diagnose.py              # read-only state dump
  python diagnose.py --post-test  # actually post ONE small GTC limit BUY
                                  # to verify the v2 signing path works end
                                  # to end. The order goes on the May 8
                                  # market at 0.70 for ~$4.27.

Requires `.env` with the same secrets as GH Actions.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from dotenv import load_dotenv

import bot


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-test", action="store_true",
                    help="Actually post one GTC limit BUY at 0.70 for ~$4.27 "
                         "via bot.post_gtc_limit_buy (v2 signing path).")
    args = ap.parse_args()

    load_dotenv()
    cfg = bot.load_config()
    print(f"== Diagnose for order_mode={cfg.order_mode!r} ==\n")

    # Identity
    try:
        from eth_account import Account
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        acct = Account.from_key(pk)
        print(f"EOA from PRIVATE_KEY: {acct.address}")
        print(f"FUNDER (proxy)      : {os.environ.get('POLYMARKET_FUNDER_ADDRESS')}")
        print(f"SIG_TYPE            : {os.environ.get('POLYMARKET_SIGNATURE_TYPE', '1')}")
    except Exception as e:
        print(f"Could not derive EOA: {e}")

    try:
        client = bot.build_client()
    except Exception as e:
        print(f"FATAL: build_client failed: {e}")
        return 1
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS")

    # Balance + allowance
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        res = client.get_balance_allowance(params)
        if isinstance(res, dict):
            raw_bal = res.get("balance")
            balance = int(raw_bal) / 1_000_000 if raw_bal else 0.0
            print(f"USDC balance: ${balance:.4f}")
            allowances = res.get("allowances") or {}
            for spender, raw in allowances.items():
                try:
                    val = int(raw) / 1_000_000
                except Exception:
                    val = 0.0
                tag = "OK (~max)" if val > 1e15 else f"${val:.2f}"
                print(f"  allowance for {spender}: {tag}")
        else:
            balance = int(res) / 1_000_000
            print(f"USDC balance: ${balance:.4f}")
    except Exception as e:
        print(f"FATAL: balance/allowance fetch failed: {e}")
        traceback.print_exc()
        return 1

    if balance < cfg.min_order_usdc:
        print(f"-> balance < min_order_usdc ({cfg.min_order_usdc}). Bot would exit early.")
        return 0

    # Open orders globally
    print("\n--- OPEN ORDERS (across all markets) ---")
    open_orders = bot.list_open_orders(client)
    print(f"Total open orders: {len(open_orders)}")
    for o in open_orders:
        print(f"  side={o.side} price={o.price:.4f} size_remaining={o.size_remaining:.2f} "
              f"usdc=${o.remaining_usdc:.2f} token={o.token_id[:14]}... id={o.order_id[:14]}")

    # Event + candidates
    print("\n--- EVENT DISCOVERY ---")
    event = bot.discover_event(cfg.event_slug_keywords, cfg.event_slug_fallback)
    raw_markets = event.get("markets", []) or []
    print(f"Event '{event.get('title')}'  slug={event.get('slug')!r}  total markets={len(raw_markets)}")

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
            "question": q, "token_id": tid,
            "condition_id": m.get("conditionId", ""),
            "mins_left": mins_left,
        })
    candidates.sort(key=lambda c: c["mins_left"])
    active = candidates[: cfg.max_active_markets]
    print(f"Candidates after time-cutoff filter: {len(candidates)}; using top {len(active)}")

    # Per-market: ask + position + tier verdict
    print("\n--- PER-MARKET STATE ---")
    for c in active:
        token_orders = [o for o in open_orders if o.token_id == c["token_id"] and o.side == "BUY"]
        ask = bot.best_ask(client, c["token_id"])
        size, avg = bot.get_yes_position(funder, c["condition_id"])
        constraints = bot.get_market_constraints(c["condition_id"])
        print(f"\n[{c['question']}]  mins_left={c['mins_left']:.0f}")
        print(f"  best_ask={ask}  filled_position={size:.2f}sh @ avg {avg:.4f}")
        print(f"  constraints: tick={constraints['minimum_tick_size']}  "
              f"min_size={constraints['minimum_order_size']}sh  neg_risk={constraints['neg_risk']}")
        print(f"  open BUYs on this token: {len(token_orders)}")
        for o in token_orders:
            print(f"    @ {o.price:.4f}  remaining {o.size_remaining:.2f}sh = ${o.remaining_usdc:.2f}")
        for tier in cfg.entry_ladder:
            tier_usdc = tier.pct_of_balance * balance
            verdict = []
            if tier_usdc < cfg.min_order_usdc:
                verdict.append(f"tier_usdc ${tier_usdc:.2f} < min_order ${cfg.min_order_usdc}")
            if ask is None:
                verdict.append("no ask")
            elif ask <= tier.price:
                verdict.append(f"ask {ask:.4f} <= tier -> ANTI-CROSS skip")
            desired_shares = bot.round_down(tier_usdc / tier.price, cfg.order_decimals)
            if desired_shares < constraints["minimum_order_size"]:
                verdict.append(f"size {desired_shares:.2f} < min {constraints['minimum_order_size']:.0f}")
            existing = bot.find_matching_order(token_orders, tier.price)
            if existing:
                verdict.append(f"existing @ {existing.price:.4f} ({existing.size_remaining:.2f}sh) -> KEEP")
            line = " | ".join(verdict) if verdict else f"WOULD PLACE {desired_shares:.2f}sh @ {tier.price:.2f} = ${tier_usdc:.2f}"
            print(f"  tier <={tier.price:.2f} ({tier.pct_of_balance*100:.0f}% = ${tier_usdc:.2f}): {line}")

    # Optional: actually post one order via bot.post_gtc_limit_buy (v2)
    if args.post_test and active:
        c = active[0]
        print(f"\n--- POST-TEST via bot.post_gtc_limit_buy (v2 signing) ---")
        print(f"  market: {c['question']}")
        print(f"  size: 6.10 sh @ 0.70 = $4.27")
        constraints = bot.get_market_constraints(c["condition_id"])
        resp = bot.post_gtc_limit_buy(
            client=client,
            token_id=c["token_id"],
            price=0.70,
            size_shares=6.10,
            tick_size=constraints["minimum_tick_size"],
            neg_risk=constraints["neg_risk"],
        )
        if resp is None:
            print("  RESULT: None (post failed; check logs above)")
        else:
            print(f"  RESULT: {resp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
