"""Live diagnostic: connect with the same creds the bot uses, list current
state (balance, open orders, positions), and show exactly why each
candidate tier would or wouldn't get an order.

Run locally:  python diagnose.py
              python diagnose.py --post-test     # actually posts ONE small
                                                 # GTC limit BUY at 0.70 to
                                                 # see the real error from
                                                 # the exchange (or success)

Requires `.env` to be present with the same secrets as GH Actions.

Without --post-test, calls nothing destructive.
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
                    help="Attempt to post ONE GTC limit BUY at 0.70 for ~$4 worth "
                         "on the soonest market. Surfaces the real error from "
                         "post_order that the bot's try/except is swallowing. "
                         "If it succeeds, you'll see one new order in your account.")
    ap.add_argument("--patch-exchange", default=None,
                    help="Monkey-patch py-clob-client to use this exchange contract "
                         "address instead of the SDK's hardcoded 0x4bFb41d5... "
                         "Use the regular-exchange address from your allowance dict.")
    ap.add_argument("--patch-neg-risk-exchange", default=None,
                    help="Monkey-patch the NegRisk exchange address.")
    ap.add_argument("--patch-domain-name", default=None,
                    help="Override the EIP-712 domain name (default: 'Polymarket CTF Exchange').")
    ap.add_argument("--patch-domain-version", default=None,
                    help="Override the EIP-712 domain version (default: '1').")
    ap.add_argument("--patch-sig-type", type=int, default=None,
                    help="Override POLYMARKET_SIGNATURE_TYPE for this run only.")
    ap.add_argument("--patch-defer-exec", action="store_true",
                    help="Inject deferExec=false at top level of POST /order body "
                         "(JS SDK 5.8.1 includes this; Python SDK does not).")
    ap.add_argument("--v2-test", action="store_true",
                    help="Manually build, sign and POST a v2 Polymarket order, "
                         "bypassing the SDK's v1-only order builder. Uses the on-chain "
                         "ORDER_TYPEHASH and v2 struct fields (timestamp/metadata/builder).")
    args = ap.parse_args()
    if args.v2_test:
        load_dotenv()
        post_v2_order_test()
        return 0
    if args.patch_defer_exec:
        from py_clob_client import utilities as utl
        original_order_to_json = utl.order_to_json
        def patched(order, owner, orderType, post_only=False):
            body = original_order_to_json(order, owner, orderType, post_only)
            body["deferExec"] = False
            print(f"  [patch] body now has deferExec=false; keys={list(body.keys())}")
            return body
        utl.order_to_json = patched
        from py_clob_client import client as cli
        cli.order_to_json = patched
        print("  [patch] order_to_json wrapped to include deferExec=false")


def post_v2_order_test():
    """Manually build, sign and post a Polymarket v2 limit order, bypassing
    the SDK's v1-only order builder. Uses the actual ORDER_TYPEHASH from the
    deployed v2 contract, with the new fields (timestamp, metadata, builder)
    instead of the v1 fields (taker, expiration, nonce, feeRateBps).
    """
    import secrets
    import time
    import requests
    from eth_abi import encode as abi_encode
    from eth_account import Account
    from eth_utils import keccak

    # Build minimal client just to get L2 headers + creds
    client = bot.build_client()
    creds = client.creds
    funder = os.environ["POLYMARKET_FUNDER_ADDRESS"]
    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    eoa = Account.from_key(pk)

    # Pick the same target market as before: May 8
    event = bot.discover_event(["trump", "insult", "someone"], "will-trump-publicly-insult-someone-on-312")
    target = None
    for m in event.get("markets", []):
        if "May 8, 2026" in (m.get("question") or ""):
            target = m
            break
    if target is None:
        print("FATAL: May 8 market not found")
        return
    yes_tid = bot.yes_token_id(target)
    print(f"  target: {target.get('question')}")
    print(f"  yes_token_id: {yes_tid[:20]}...")

    # v2 contract constants (from on-chain eip712Domain() + Sourcify Hashing.sol)
    EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"  # regular CTF Exchange v2
    DOMAIN_NAME = "Polymarket CTF Exchange"
    DOMAIN_VERSION = "2"
    CHAIN_ID = 137
    ORDER_TYPEHASH = bytes.fromhex(
        "bb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589"
    )
    EIP712_DOMAIN_TYPEHASH = keccak(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    )

    # Order params (BUY 6.10 shares @ 0.70 = $4.27 like before)
    price = 0.70
    size_shares = 6.10
    maker_amt = int(size_shares * price * 1_000_000)   # USDC 6 decimals
    taker_amt = int(size_shares * 1_000_000)            # CTF 6 decimals
    side = 0  # BUY
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))
    salt = secrets.randbits(64)
    timestamp_ms = int(time.time() * 1000)
    metadata = b"\x00" * 32
    builder = b"\x00" * 32

    # Compute domain separator
    domain_sep = keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [EIP712_DOMAIN_TYPEHASH, keccak(DOMAIN_NAME.encode()),
         keccak(DOMAIN_VERSION.encode()), CHAIN_ID, EXCHANGE_V2],
    ))

    # Compute struct hash
    struct_hash = keccak(abi_encode(
        ["bytes32", "uint256", "address", "address", "uint256",
         "uint256", "uint256", "uint8", "uint8", "uint256",
         "bytes32", "bytes32"],
        [ORDER_TYPEHASH, salt, funder, eoa.address, int(yes_tid),
         maker_amt, taker_amt, side, sig_type, timestamp_ms,
         metadata, builder],
    ))

    digest = keccak(b"\x19\x01" + domain_sep + struct_hash)
    sig = Account._sign_hash(digest, private_key=pk)
    signature_hex = "0x" + sig.signature.hex().lstrip("0x")
    if not signature_hex.startswith("0x"):
        signature_hex = "0x" + signature_hex
    print(f"  v2 signature: {signature_hex[:34]}...{signature_hex[-8:]}")

    body_order = {
        "salt": salt,
        "maker": funder,
        "signer": eoa.address,
        "tokenId": str(yes_tid),
        "makerAmount": str(maker_amt),
        "takerAmount": str(taker_amt),
        "side": "BUY",
        "signatureType": sig_type,
        "timestamp": str(timestamp_ms),
        "metadata": "0x" + metadata.hex(),
        "builder": "0x" + builder.hex(),
        "signature": signature_hex,
    }
    body = {
        "deferExec": False,
        "order": body_order,
        "owner": creds.api_key,
        "orderType": "GTC",
        "postOnly": True,
    }

    # Build L2 headers via SDK
    from py_clob_client.headers.headers import create_level_2_headers
    from py_clob_client.http_helpers.helpers import POST
    from py_clob_client.signer import Signer
    from py_clob_client.endpoints import POST_ORDER
    from py_clob_client.clob_types import RequestArgs
    import json as _json

    serialized = _json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    request_args = RequestArgs(method="POST", request_path=POST_ORDER,
                               body=body, serialized_body=serialized)
    signer = Signer(private_key=pk, chain_id=CHAIN_ID)
    headers = create_level_2_headers(signer, creds, request_args)

    print(f"  POST body keys: {list(body.keys())}  order keys: {list(body_order.keys())}")
    r = requests.post("https://clob.polymarket.com/order", headers=headers,
                      data=serialized, timeout=30)
    print(f"  HTTP {r.status_code}")
    try:
        print(f"  body: {r.json()}")
    except Exception:
        print(f"  text: {r.text[:500]}")
    if args.patch_sig_type is not None:
        os.environ["POLYMARKET_SIGNATURE_TYPE"] = str(args.patch_sig_type)
        print(f"  [patch] POLYMARKET_SIGNATURE_TYPE override: {args.patch_sig_type} "
              f"(0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE)")

    # Patch EIP-712 domain name/version if requested
    if args.patch_domain_name or args.patch_domain_version:
        from py_order_utils.builders import base_builder as bb
        from poly_eip712_structs import make_domain
        original_get_domain = bb.BaseBuilder._get_domain_separator
        new_name = args.patch_domain_name or "Polymarket CTF Exchange"
        new_version = args.patch_domain_version or "1"
        def patched_domain(self, chain_id, verifying_contract):
            print(f"  [patch] EIP-712 domain: name={new_name!r} version={new_version!r} chain={chain_id} contract={verifying_contract}")
            return make_domain(name=new_name, version=new_version, chainId=str(chain_id), verifyingContract=verifying_contract)
        bb.BaseBuilder._get_domain_separator = patched_domain
        print(f"  [patch] domain override installed: name={new_name!r} version={new_version!r}")

    # Patch must happen BEFORE building the client so the order builder uses
    # the new addresses for EIP-712 domain.
    # Both py_clob_client.client and py_clob_client.order_builder.builder do
    # `from .config import get_contract_config` -- so we have to rebind the
    # name in EACH consumer module, not just the source module.
    if args.patch_exchange or args.patch_neg_risk_exchange:
        from py_clob_client import config as clob_cfg
        from py_clob_client import client as clob_client_mod
        from py_clob_client.order_builder import builder as clob_builder_mod
        original = clob_cfg.get_contract_config

        def patched_get_contract_config(chainID, neg_risk=False):
            cfg = original(chainID, neg_risk)
            if neg_risk and args.patch_neg_risk_exchange:
                cfg.exchange = args.patch_neg_risk_exchange
                print(f"  [patch] using neg_risk exchange={cfg.exchange}")
            elif (not neg_risk) and args.patch_exchange:
                cfg.exchange = args.patch_exchange
                print(f"  [patch] using regular exchange={cfg.exchange}")
            return cfg
        clob_cfg.get_contract_config = patched_get_contract_config
        clob_client_mod.get_contract_config = patched_get_contract_config
        clob_builder_mod.get_contract_config = patched_get_contract_config
        print(f"  [patch] installed get_contract_config override on 3 modules")

    load_dotenv()
    cfg = bot.load_config()
    print(f"== Diagnose for order_mode={cfg.order_mode!r} ==\n")

    # Derive EOA from private key and compare with expected funder/proxy
    try:
        from eth_account import Account
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        acct = Account.from_key(pk)
        print(f"EOA from PRIVATE_KEY: {acct.address}")
        print(f"FUNDER (proxy)      : {os.environ.get('POLYMARKET_FUNDER_ADDRESS')}")
        print(f"SIG_TYPE            : {os.environ.get('POLYMARKET_SIGNATURE_TYPE', '1')}")
        print(f"  (For POLY_PROXY wallets the EOA is the *admin* of the proxy.")
        print(f"   These two addresses are DIFFERENT by design -- that's normal.")
        print(f"   What matters is that this EOA is the registered owner of the proxy.)")
    except Exception as e:
        print(f"Could not derive EOA: {e}")

    # Build the real client — same path bot.py takes
    try:
        client = bot.build_client()
    except Exception as e:
        print(f"FATAL: build_client failed: {e}")
        return 1
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS")
    print(f"Funder: {funder}")

    # Balance + allowance
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        res = client.get_balance_allowance(params)
        print(f"Raw balance/allowance response: {res}")
        if isinstance(res, dict):
            raw_bal = res.get("balance")
            balance = int(raw_bal) / 1_000_000 if raw_bal else 0.0
            print(f"USDC balance: ${balance:.4f}")
            # The CLOB returns allowances per spender contract (Exchange,
            # CTF, NegRisk). Polymarket sets each to max uint256 by default
            # when you fund. Any 0 here means that contract can't pull USDC.
            allowances = res.get("allowances") or {}
            for spender, raw in allowances.items():
                try:
                    val = int(raw) / 1_000_000
                except Exception:
                    val = 0.0
                # 1e29 = effectively infinite (max uint256 / 1e6)
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
            ts = c["token_id"]
            test_args = OrderArgs(
                token_id=ts,
                price=0.70,
                size=10.0,
                side=BUY,
            )
            opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
            signed = client.create_order(test_args, opts)
            print(f"  create_order OK on {c['question'][:50]}... signed_keys={list(vars(signed).keys()) if hasattr(signed,'__dict__') else type(signed).__name__}")
        except Exception as e:
            print(f"  create_order FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()

    # --- DESTRUCTIVE: post a real order to surface the actual exchange error ---
    if args.post_test and active:
        c = active[0]
        print(f"\n--- POST-TEST: actually posting one GTC limit BUY ---")
        print(f"  market: {c['question']}")
        print(f"  size: 6.10 shares @ 0.70 = $4.27 (well above $5 min and within balance)")
        try:
            from py_clob_client.clob_types import PartialCreateOrderOptions, OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
            real_args = OrderArgs(
                token_id=c["token_id"],
                price=0.70,
                size=6.10,
                side=BUY,
            )
            opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
            signed = client.create_order(real_args, opts)
            print("  create_order: OK")
            print("  calling post_order(GTC, post_only=True)...")
            resp = client.post_order(signed, OrderType.GTC, post_only=True)
            print(f"  post_order RESPONSE: {resp}")
            print()
            print("  -> If you see {'success': True} or an order ID above, the bot's")
            print("     issue is something else (e.g., post_only rejection in some cases).")
            print("  -> If you see an error message, that IS what the bot's try/except")
            print("     was swallowing. We now know what to fix.")
        except Exception as e:
            print(f"  post_order RAISED: {type(e).__name__}: {e}")
            traceback.print_exc()

    return 0


if __name__ == "__main__":
    sys.exit(main())
