"""
Polymarket "Will Trump publicly insult someone" bot.

Single-execution entrypoint. Intended to be invoked on a schedule
(GitHub Actions cron every ~5 min). Fetches the event's daily markets
and posts a scale-in ladder of GTC limit BUY orders on the YES side
of each daily sub-market, subject to time-cutoff and exposure caps.

Two execution modes (config: order_mode):
  - "limit"  (default): place GTC limit orders at each tier price.
                        Anti-cross guard prevents accidental taker fills.
                        Per-tier independent (NOT cumulative top-up).
  - "market" (legacy):  market FOK at the current ask up to cumulative
                        tier target. Kept for fallback / A/B testing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

import requests
import yaml
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
POLYGON_CHAIN_ID = 137


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bot")


# --- Config ---

@dataclass
class LadderTier:
    price: float
    pct_of_balance: float


@dataclass
class Config:
    event_slug_keywords: list[str]
    event_slug_fallback: str
    order_mode: str  # "limit" | "market"
    order_time_in_force: str  # "GTC" | "GTD"
    entry_ladder: list[LadderTier]
    min_order_usdc: float
    order_decimals: int
    tier_fill_tolerance: float
    skip_if_minutes_remaining_below: float
    max_total_exposure_pct: float
    max_active_markets: int


def load_config(path: str = "config.yaml") -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    ladder_raw = raw["entry_ladder"]
    # Sort shallow-first so tier 1 is the highest price (first entry on first dip).
    ladder = sorted(
        [LadderTier(price=float(t["price"]), pct_of_balance=float(t["pct_of_balance"])) for t in ladder_raw],
        key=lambda t: -t.price,
    )
    keywords = raw.get("event_slug_keywords")
    if keywords is None:
        # Back-compat: if old config has event_slug, derive keywords from it.
        slug = raw.get("event_slug", "")
        keywords = [w for w in slug.split("-") if w and len(w) > 2][:3]
    return Config(
        event_slug_keywords=[k.lower() for k in keywords],
        event_slug_fallback=raw.get("event_slug_fallback") or raw.get("event_slug", ""),
        order_mode=str(raw.get("order_mode", "limit")).lower(),
        order_time_in_force=str(raw.get("order_time_in_force", "GTC")).upper(),
        entry_ladder=ladder,
        min_order_usdc=float(raw["min_order_usdc"]),
        order_decimals=int(raw["order_decimals"]),
        tier_fill_tolerance=float(raw["tier_fill_tolerance"]),
        skip_if_minutes_remaining_below=float(raw.get("skip_if_minutes_remaining_below", 0)),
        max_total_exposure_pct=float(raw.get("max_total_exposure_pct", 1.0)),
        max_active_markets=int(raw.get("max_active_markets", 100)),
    )


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def minutes_until_resolution_day_end(question: str) -> float | None:
    """Parse 'Month Day, Year' out of the market question and return the
    minutes remaining until the end of that day in ET. Returns None if the
    date can't be parsed."""
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", question or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        # ET midnight of the day after: approximate EDT (-04:00) offset.
        # April-November is EDT; December-March is EST (-05:00). Live markets
        # in this event span April onwards, so EDT is the right default.
        day_end_utc = datetime(int(m.group(3)), mon, int(m.group(2)), 4, 0, tzinfo=timezone.utc) + timedelta(days=1)
    except Exception:
        return None
    return (day_end_utc.timestamp() - time.time()) / 60


def round_down(value: float, decimals: int) -> float:
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))


# --- Polymarket client ---

def build_client() -> ClobClient:
    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ["POLYMARKET_FUNDER_ADDRESS"]
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))

    client = ClobClient(
        host=CLOB_API,
        key=pk,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=sig_type,
        funder=funder,
    )

    api_key = os.environ.get("POLYMARKET_API_KEY")
    api_secret = os.environ.get("POLYMARKET_API_SECRET")
    api_pass = os.environ.get("POLYMARKET_API_PASSPHRASE")
    if api_key and api_secret and api_pass:
        client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass))
    else:
        client.set_api_creds(client.create_or_derive_api_creds())
    return client


# --- Event discovery ---

def discover_event(keywords: list[str], fallback_slug: str) -> dict:
    """Find an OPEN event whose slug contains every keyword. Falls back to
    the explicit slug if the search returns nothing.
    Auto-discovery prevents the recurring failure where Polymarket rotates
    the slug suffix when re-creating a daily event each month.
    """
    keys = [k.lower() for k in keywords]
    matches: list[dict] = []
    offset = 0
    page_size = 200
    pages = 0
    while True:
        try:
            r = requests.get(
                f"{GAMMA_API}/events",
                params={
                    "closed": "false",
                    "order": "startDate",
                    "ascending": "false",
                    "limit": page_size,
                    "offset": offset,
                },
                timeout=20,
            )
            r.raise_for_status()
        except Exception as e:
            log.warning("event search page %d failed: %s", pages, e)
            break
        data = r.json() or []
        if not data:
            break
        for e in data:
            slug = (e.get("slug") or "").lower()
            if all(k in slug for k in keys):
                matches.append(e)
        offset += page_size
        pages += 1
        if pages > 25:
            break

    # Among matches, prefer the one with the most OPEN sub-markets (the live
    # daily-insult event has 30+; stale ones have 0).
    def score(ev: dict) -> int:
        ms = ev.get("markets", []) or []
        return sum(1 for m in ms if not m.get("closed") and not m.get("archived"))
    matches.sort(key=score, reverse=True)
    if matches and score(matches[0]) > 0:
        chosen = matches[0]
        log.info(
            "Discovered event slug=%r (%d open sub-markets) via keywords %s",
            chosen.get("slug"), score(chosen), keys,
        )
        return chosen

    if fallback_slug:
        log.warning("Auto-discovery returned no open event matching %s; falling back to slug=%r", keys, fallback_slug)
        r = requests.get(f"{GAMMA_API}/events", params={"slug": fallback_slug}, timeout=15)
        r.raise_for_status()
        events = r.json()
        if events:
            return events[0]
    raise RuntimeError(f"Could not find an open event for keywords={keys} (fallback={fallback_slug!r})")


# --- Helpers ---

def parse_json_field(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def yes_token_id(market: dict) -> str | None:
    outcomes = parse_json_field(market.get("outcomes"))
    token_ids = parse_json_field(market.get("clobTokenIds"))
    if len(outcomes) != len(token_ids):
        return None
    for outcome, tid in zip(outcomes, token_ids):
        if outcome.strip().lower() == "yes":
            return tid
    return None


def market_is_tradeable(market: dict) -> bool:
    if market.get("closed") or market.get("archived"):
        return False
    if market.get("active") is False:
        return False
    return True


def best_ask(client: ClobClient, token_id: str) -> float | None:
    try:
        book = client.get_order_book(token_id)
    except Exception as e:
        log.warning("Could not fetch orderbook for %s: %s", token_id[:10], e)
        return None
    asks = getattr(book, "asks", None) or []
    if not asks:
        return None
    return min(float(a.price) for a in asks)


# Cache market constraints (tick size, min order size) per condition_id —
# these are static for a market's lifetime; fetching every loop is wasteful.
_MARKET_CONSTRAINTS_CACHE: dict[str, dict] = {}


def get_market_constraints(condition_id: str) -> dict:
    """Return {minimum_order_size, minimum_tick_size, neg_risk} for a market.
    Public CLOB endpoint; no auth needed. Cached process-locally.
    """
    if condition_id in _MARKET_CONSTRAINTS_CACHE:
        return _MARKET_CONSTRAINTS_CACHE[condition_id]
    try:
        r = requests.get(f"{CLOB_API}/markets/{condition_id}", timeout=15)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        log.warning("market constraints fetch failed for %s: %s", condition_id[:12], e)
        data = {}
    out = {
        "minimum_order_size": float(data.get("minimum_order_size") or 5),
        "minimum_tick_size": float(data.get("minimum_tick_size") or 0.01),
        "neg_risk": bool(data.get("neg_risk") or False),
    }
    _MARKET_CONSTRAINTS_CACHE[condition_id] = out
    return out


def get_balance_usdc(client: ClobClient) -> float:
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    res = client.get_balance_allowance(params)
    raw = res.get("balance") if isinstance(res, dict) else res
    return int(raw) / 1_000_000


def get_yes_position(proxy_address: str, condition_id: str) -> tuple[float, float]:
    """Return (size_shares, avg_fill_price) for the YES outcome of this market."""
    try:
        r = requests.get(f"{DATA_API}/positions", params={"user": proxy_address}, timeout=15)
        r.raise_for_status()
        positions = r.json()
    except Exception as e:
        log.warning("Positions lookup failed: %s", e)
        return 0.0, 0.0
    for p in positions:
        if p.get("conditionId", "").lower() != condition_id.lower():
            continue
        if (p.get("outcome") or "").strip().lower() != "yes":
            continue
        size = float(p.get("size", 0) or 0)
        avg = float(p.get("avgPrice", 0) or 0)
        return size, avg
    return 0.0, 0.0


# --- Order management (limit mode) ---

@dataclass
class OpenOrderRow:
    order_id: str
    token_id: str
    price: float
    size_shares: float       # original size
    size_remaining: float    # unfilled
    side: str                # "BUY" | "SELL"

    @property
    def remaining_usdc(self) -> float:
        return self.price * self.size_remaining


def list_open_orders(client: ClobClient) -> list[OpenOrderRow]:
    """Return all open orders for the authenticated user."""
    try:
        raw = client.get_orders()
    except Exception as e:
        log.warning("get_orders failed: %s", e)
        return []
    out = []
    for o in raw or []:
        try:
            side = (o.get("side") or "").upper()
            price = float(o.get("price"))
            size = float(o.get("original_size") or o.get("originalSize") or 0)
            filled = float(o.get("size_matched") or o.get("sizeMatched") or 0)
            remaining = max(size - filled, 0.0)
            tid = o.get("asset_id") or o.get("market") or ""
            oid = o.get("id") or o.get("orderID") or ""
            out.append(OpenOrderRow(
                order_id=str(oid),
                token_id=str(tid),
                price=price,
                size_shares=size,
                size_remaining=remaining,
                side=side,
            ))
        except Exception as e:
            log.warning("could not parse open order %s: %s", o, e)
    return out


def cancel_order_safe(client: ClobClient, order_id: str) -> bool:
    try:
        client.cancel(order_id=order_id)
        return True
    except Exception as e:
        log.warning("cancel %s failed: %s", order_id[:12], e)
        return False


# --- Polymarket v2 order signing (bypasses py-clob-client) ---
#
# As of mid-2026 Polymarket migrated to v2 of the CTF Exchange contracts.
# py-clob-client 0.34.6 (and JS SDK 5.8.1) still build v1 orders, which the
# v2 contracts reject with `order_version_mismatch` because the Order struct
# has different fields:
#   v1: salt, maker, signer, taker, tokenId, makerAmount, takerAmount,
#       expiration, nonce, feeRateBps, side, signatureType
#   v2: salt, maker, signer, tokenId, makerAmount, takerAmount, side,
#       signatureType, timestamp, metadata, builder
# We bypass the SDK and build/sign v2 orders manually below.
#
# Verified via Sourcify (chain 137):
#   regular Exchange v2:  0xE111180000d2663C0091e4f400237545B87B996B
#   NegRisk Exchange v2:  0xe2222d279d744050d28e00520010520000310F59
# eip712Domain():  name="Polymarket CTF Exchange"  version="2"  chainId=137
# ORDER_TYPEHASH = keccak("Order(uint256 salt,address maker,address signer,
#   uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,
#   uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)")
EXCHANGE_V2_REGULAR = "0xE111180000d2663C0091e4f400237545B87B996B"
EXCHANGE_V2_NEG_RISK = "0xe2222d279d744050d28e00520010520000310F59"
DOMAIN_NAME_V2 = "Polymarket CTF Exchange"
DOMAIN_VERSION_V2 = "2"
ORDER_TYPEHASH_V2 = bytes.fromhex(
    "bb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589"
)


def post_gtc_limit_buy(
    client: ClobClient,
    token_id: str,
    price: float,
    size_shares: float,
    tick_size: float = 0.01,
    neg_risk: bool = False,
) -> dict | None:
    """Place a GTC limit BUY using Polymarket v2 order signing.

    Builds the order struct, computes the EIP-712 digest, signs with the
    user's EOA private key, and posts directly to /order with the L2 auth
    headers from the SDK. post_only=True so the exchange rejects if our
    bid would cross the spread (belt-and-braces with our anti-cross check).
    """
    import secrets
    import time as _time
    import json as _json
    from eth_abi import encode as _abi_encode
    from eth_account import Account
    from eth_utils import keccak as _keccak
    from py_clob_client.clob_types import RequestArgs
    from py_clob_client.endpoints import POST_ORDER
    from py_clob_client.headers.headers import create_level_2_headers
    from py_clob_client.signer import Signer

    try:
        ticks = round(price / tick_size)
        rounded_price = round(ticks * tick_size, 6)
        # Quantize size to share decimals (2 by config; the exchange accepts up to 6).
        size_shares = round(size_shares, 4)
        maker_amt = int(round(size_shares * rounded_price * 1_000_000))
        taker_amt = int(round(size_shares * 1_000_000))

        funder = os.environ["POLYMARKET_FUNDER_ADDRESS"]
        pk = os.environ["POLYMARKET_PRIVATE_KEY"]
        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))
        eoa = Account.from_key(pk)

        exchange = EXCHANGE_V2_NEG_RISK if neg_risk else EXCHANGE_V2_REGULAR
        chain_id = POLYGON_CHAIN_ID

        salt = secrets.randbits(64)
        timestamp_ms = int(_time.time() * 1000)
        metadata_b = b"\x00" * 32
        builder_b = b"\x00" * 32

        eip712_domain_typehash = _keccak(
            b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        )
        domain_sep = _keccak(_abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [eip712_domain_typehash, _keccak(DOMAIN_NAME_V2.encode()),
             _keccak(DOMAIN_VERSION_V2.encode()), chain_id, exchange],
        ))
        struct_hash = _keccak(_abi_encode(
            ["bytes32", "uint256", "address", "address", "uint256",
             "uint256", "uint256", "uint8", "uint8", "uint256",
             "bytes32", "bytes32"],
            [ORDER_TYPEHASH_V2, salt, funder, eoa.address, int(token_id),
             maker_amt, taker_amt, 0, sig_type, timestamp_ms,
             metadata_b, builder_b],
        ))
        digest = _keccak(b"\x19\x01" + domain_sep + struct_hash)
        sig = Account._sign_hash(digest, private_key=pk)
        signature_hex = sig.signature.hex()
        if not signature_hex.startswith("0x"):
            signature_hex = "0x" + signature_hex

        body_order = {
            "salt": salt,
            "maker": funder,
            "signer": eoa.address,
            "tokenId": str(token_id),
            "makerAmount": str(maker_amt),
            "takerAmount": str(taker_amt),
            "side": "BUY",
            "signatureType": sig_type,
            "timestamp": str(timestamp_ms),
            "metadata": "0x" + metadata_b.hex(),
            "builder": "0x" + builder_b.hex(),
            "signature": signature_hex,
        }
        body = {
            "deferExec": False,
            "order": body_order,
            "owner": client.creds.api_key,
            "orderType": "GTC",
            "postOnly": True,
        }

        serialized = _json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        request_args = RequestArgs(
            method="POST", request_path=POST_ORDER,
            body=body, serialized_body=serialized,
        )
        signer_obj = Signer(private_key=pk, chain_id=chain_id)
        headers = create_level_2_headers(signer_obj, client.creds, request_args)
        r = requests.post(
            f"{CLOB_API}/order", headers=headers, data=serialized, timeout=30,
        )
        if r.status_code != 200:
            log.error(
                "post v2 limit BUY %.2f x %.4f on %s HTTP %d: %s",
                price, size_shares, token_id[:10], r.status_code, r.text[:300],
            )
            return None
        return r.json()
    except Exception as e:
        log.exception("post v2 limit BUY %.2f x %.4f on %s failed: %s", price, size_shares, token_id[:10], e)
        return None


# --- Strategy: limit-mode placement ---

def open_buy_orders_for_token(orders: list[OpenOrderRow], token_id: str) -> list[OpenOrderRow]:
    return [o for o in orders if o.token_id == token_id and o.side == "BUY"]


def find_matching_order(open_orders_for_token: list[OpenOrderRow], price: float, tol: float = 1e-3) -> OpenOrderRow | None:
    for o in open_orders_for_token:
        if abs(o.price - price) <= tol:
            return o
    return None


def market_committed_usdc(open_orders_for_token: list[OpenOrderRow], filled_size: float, filled_avg: float) -> float:
    """Total USDC committed to this market: open BUY orders + filled YES position."""
    open_usdc = sum(o.remaining_usdc for o in open_orders_for_token)
    filled_usdc = filled_size * filled_avg
    return open_usdc + filled_usdc


def run_once_limit(cfg: Config, client: ClobClient, funder: str) -> None:
    balance = get_balance_usdc(client)
    log.info("USDC balance: %.4f", balance)
    if balance < cfg.min_order_usdc:
        log.info("Balance below min_order_usdc, nothing to do.")
        return

    log.info(
        "Limit-mode ladder per market: %s  (cum %.0f%% if all tiers fill)",
        ", ".join(f"<= {t.price:.2f} -> {t.pct_of_balance*100:.1f}%" for t in cfg.entry_ladder),
        sum(t.pct_of_balance for t in cfg.entry_ladder) * 100,
    )

    event = discover_event(cfg.event_slug_keywords, cfg.event_slug_fallback)
    raw_markets = event.get("markets", []) or []
    log.info("Event '%s' has %d sub-markets", event.get("title", event.get("slug")), len(raw_markets))

    # Snapshot all open orders ONCE; we'll filter per market.
    open_orders = list_open_orders(client)
    log.info("Found %d open orders globally", len(open_orders))

    # Build a token_id -> mins_left map across ALL event markets (not just
    # tradeable ones) so we can decide whether existing open orders should
    # be cancelled if their market has entered the danger zone.
    token_to_mins_left: dict[str, float] = {}
    token_to_question: dict[str, str] = {}
    for m in raw_markets:
        tid = yes_token_id(m)
        if not tid:
            continue
        q = m.get("question") or m.get("slug") or ""
        ml = minutes_until_resolution_day_end(q)
        if ml is None:
            continue
        token_to_mins_left[tid] = ml
        token_to_question[tid] = q

    # Cancel any open BUY whose market is now within the time cutoff.
    # Rationale: a resting bid at 0.70 sitting through the last 8h of EOD
    # has asymmetric tail risk -- if Trump hasn't insulted by mid-evening,
    # the ask can collapse from 0.85 to 0.50 and our bid fills at 0.70 on a
    # market that is statistically very likely to resolve NO.
    # We cancel ONLY orders on tokens we recognise from this event, leaving
    # any unrelated orders alone.
    cancelled_stale = 0
    for o in list(open_orders):
        if o.side != "BUY":
            continue
        ml = token_to_mins_left.get(o.token_id)
        if ml is None:
            continue
        if ml < cfg.skip_if_minutes_remaining_below:
            log.info(
                "[%s] CANCEL stale order %s @ %.2f (%.4f sh, $%.2f) — only %.0f min left to EOD (< cutoff %.0f)",
                token_to_question.get(o.token_id, o.token_id[:10]),
                o.order_id[:14], o.price, o.size_remaining, o.remaining_usdc,
                ml, cfg.skip_if_minutes_remaining_below,
            )
            if cancel_order_safe(client, o.order_id):
                cancelled_stale += 1
                open_orders.remove(o)
    if cancelled_stale:
        log.info("Cancelled %d stale orders (markets within %0.f min of EOD).",
                 cancelled_stale, cfg.skip_if_minutes_remaining_below)

    # Build prioritized list of tradeable markets: filter by tradeable +
    # time-cutoff, sort by ascending mins-to-EOD, cap at max_active_markets.
    candidates = []
    for m in raw_markets:
        if not market_is_tradeable(m):
            continue
        q = m.get("question") or m.get("slug") or ""
        tid = yes_token_id(m)
        if not tid:
            continue
        mins_left = minutes_until_resolution_day_end(q)
        if mins_left is None:
            log.warning("[%s] could not parse resolution date, skipping", q)
            continue
        if mins_left < cfg.skip_if_minutes_remaining_below:
            continue
        candidates.append({
            "question": q,
            "token_id": tid,
            "condition_id": m.get("conditionId", ""),
            "mins_left": mins_left,
        })
    candidates.sort(key=lambda c: c["mins_left"])
    log.info(
        "Candidates after time-cutoff filter (>= %.0f min): %d",
        cfg.skip_if_minutes_remaining_below, len(candidates),
    )

    # Compute current global exposure across ALL markets (not just candidates,
    # so old open orders count too).
    global_committed = 0.0
    per_token_committed: dict[str, float] = {}
    for o in open_orders:
        if o.side != "BUY":
            continue
        per_token_committed[o.token_id] = per_token_committed.get(o.token_id, 0.0) + o.remaining_usdc
        global_committed += o.remaining_usdc
    # Add filled positions for candidate markets
    for c in candidates:
        size, avg = get_yes_position(funder, c["condition_id"])
        c["filled_size"] = size
        c["filled_avg"] = avg
        filled_usdc = size * avg
        per_token_committed[c["token_id"]] = per_token_committed.get(c["token_id"], 0.0) + filled_usdc
        global_committed += filled_usdc

    cap_total = cfg.max_total_exposure_pct * balance
    log.info(
        "Current exposure: $%.2f / cap $%.2f (%.0f%% of balance)",
        global_committed, cap_total, cfg.max_total_exposure_pct * 100,
    )

    # Restrict to top-N markets by ascending mins_left, ignoring those that
    # already have committed exposure (so we don't double-count the cap by
    # also opening new in less prioritised slots).
    active = candidates[: cfg.max_active_markets]
    log.info("Will work on top %d candidates by time-to-EOD", len(active))

    placed = 0
    cancelled = 0
    skipped_anti_cross = 0
    skipped_already_open = 0
    skipped_cap = 0
    skipped_per_market_cap = 0

    per_market_cap_usdc = sum(t.pct_of_balance for t in cfg.entry_ladder) * balance

    # Pre-fetch best ask + token_orders + market constraints for each active
    # market once (to avoid repeated CLOB calls in the round-robin loop).
    for c in active:
        c["token_orders"] = open_buy_orders_for_token(open_orders, c["token_id"])
        c["ask"] = best_ask(client, c["token_id"])
        c["market_committed"] = per_token_committed.get(c["token_id"], 0.0)
        c["constraints"] = get_market_constraints(c["condition_id"])

    # Round-robin across tiers: place tier 1 on ALL markets first, then tier 2, ...
    # This biases toward diversification rather than filling one market completely
    # before moving on, which matters when balance is small relative to the
    # cumulative per-market exposure.
    for tier in cfg.entry_ladder:
        tier_usdc = tier.pct_of_balance * balance
        if tier_usdc < cfg.min_order_usdc:
            log.info(
                "tier <=%.2f: tier_usdc $%.2f < min_order $%.2f, skip whole tier",
                tier.price, tier_usdc, cfg.min_order_usdc,
            )
            continue

        for c in active:
            q = c["question"]
            tid = c["token_id"]
            ask = c["ask"]
            token_orders = c["token_orders"]

            if ask is None:
                continue

            # Per-market cap: don't pile on if this market is already saturated.
            if c["market_committed"] >= per_market_cap_usdc * (1 - cfg.tier_fill_tolerance):
                skipped_per_market_cap += 1
                continue

            # Anti-cross
            if ask <= tier.price:
                skipped_anti_cross += 1
                log.info(
                    "[%s] tier <=%.2f: ask %.4f <= tier -> anti-cross skip",
                    q, tier.price, ask,
                )
                continue

            desired_size_shares = round_down(tier_usdc / tier.price, cfg.order_decimals)
            if desired_size_shares <= 0:
                continue

            min_shares = c["constraints"]["minimum_order_size"]
            if desired_size_shares < min_shares:
                log.info(
                    "[%s] tier <=%.2f: size %.2f sh < market min %.0f sh ($%.2f), skip "
                    "(increase balance, raise this tier's pct, or accept that small tiers won't post)",
                    q, tier.price, desired_size_shares, min_shares, tier_usdc,
                )
                continue

            existing = find_matching_order(token_orders, tier.price)
            if existing is not None:
                size_diff = abs(existing.size_remaining - desired_size_shares) / max(desired_size_shares, 1e-9)
                if size_diff < 0.10:
                    skipped_already_open += 1
                    log.info(
                        "[%s] tier <=%.2f: matching open order present (%.4f sh), keep",
                        q, tier.price, existing.size_remaining,
                    )
                    continue
                log.info(
                    "[%s] tier <=%.2f: open order diverges (%.4f vs %.4f), cancel+replace",
                    q, tier.price, existing.size_remaining, desired_size_shares,
                )
                if cancel_order_safe(client, existing.order_id):
                    cancelled += 1
                    global_committed -= existing.remaining_usdc
                    c["market_committed"] -= existing.remaining_usdc

            order_cost_usdc = desired_size_shares * tier.price
            if global_committed + order_cost_usdc > cap_total + 1e-6:
                skipped_cap += 1
                log.info(
                    "[%s] tier <=%.2f: would exceed global cap ($%.2f + $%.2f > $%.2f), skip",
                    q, tier.price, global_committed, order_cost_usdc, cap_total,
                )
                continue

            log.info(
                "[%s] PLACE GTC BUY %.4f sh @ %.2f ($%.2f)",
                q, desired_size_shares, tier.price, order_cost_usdc,
            )
            resp = post_gtc_limit_buy(
                client, tid, tier.price, desired_size_shares,
                tick_size=c["constraints"]["minimum_tick_size"],
                neg_risk=c["constraints"]["neg_risk"],
            )
            if resp is not None:
                placed += 1
                global_committed += order_cost_usdc
                c["market_committed"] += order_cost_usdc
                token_orders.append(OpenOrderRow(
                    order_id=str(resp.get("orderID") or resp.get("id") or ""),
                    token_id=tid,
                    price=tier.price,
                    size_shares=desired_size_shares,
                    size_remaining=desired_size_shares,
                    side="BUY",
                ))

    log.info(
        "Run complete. placed=%d cancelled=%d skipped_anti_cross=%d skipped_already_open=%d skipped_cap=%d skipped_per_market_cap=%d",
        placed, cancelled, skipped_anti_cross, skipped_already_open, skipped_cap, skipped_per_market_cap,
    )


# --- Strategy: legacy market-FOK mode (kept for fallback) ---

def cumulative_ladder_targets(balance: float, ladder: list[LadderTier]) -> list[float]:
    out = []
    cum = 0.0
    for t in ladder:
        cum += t.pct_of_balance * balance
        out.append(cum)
    return out


def run_once_market(cfg: Config, client: ClobClient, funder: str) -> None:
    balance = get_balance_usdc(client)
    log.info("USDC balance: %.4f", balance)
    if balance < cfg.min_order_usdc:
        log.info("Balance below min_order_usdc, nothing to do.")
        return

    log.info(
        "Market-FOK-mode ladder: %s  (cumulative)",
        ", ".join(f"<= {t.price:.2f} -> {t.pct_of_balance*100:.1f}%" for t in cfg.entry_ladder),
    )
    targets = cumulative_ladder_targets(balance, cfg.entry_ladder)

    event = discover_event(cfg.event_slug_keywords, cfg.event_slug_fallback)
    markets = event.get("markets", []) or []
    log.info("Event '%s' has %d sub-markets", event.get("title", event.get("slug")), len(markets))

    remaining_balance = balance

    for m in markets:
        q = m.get("question") or m.get("slug") or m.get("id")
        if not market_is_tradeable(m):
            continue
        tid = yes_token_id(m)
        if not tid:
            continue
        ask = best_ask(client, tid)
        if ask is None:
            continue
        shallowest_trigger = cfg.entry_ladder[0].price
        if ask > shallowest_trigger:
            continue
        if cfg.skip_if_minutes_remaining_below > 0:
            mins_left = minutes_until_resolution_day_end(q)
            if mins_left is None or mins_left < cfg.skip_if_minutes_remaining_below:
                continue

        cid = m.get("conditionId", "")
        size, avg = get_yes_position(funder, cid)
        current_spent = size * avg
        deepest_idx = None
        for idx, tier in enumerate(cfg.entry_ladder):
            if ask <= tier.price:
                deepest_idx = idx
        if deepest_idx is None:
            continue
        target_spend = targets[deepest_idx]
        gap = target_spend - current_spent
        tol_abs = cfg.tier_fill_tolerance * target_spend
        if gap <= tol_abs:
            continue
        order_amount = round_down(gap, cfg.order_decimals)
        if order_amount < cfg.min_order_usdc:
            continue
        if order_amount > remaining_balance:
            if remaining_balance < cfg.min_order_usdc:
                break
            order_amount = round_down(remaining_balance, cfg.order_decimals)
        log.info("[%s] BUY tier %d -> %.2f USDC @ ask %.3f", q, deepest_idx + 1, order_amount, ask)
        try:
            args = MarketOrderArgs(token_id=tid, amount=order_amount)
            signed = client.create_market_order(args)
            resp = client.post_order(signed, OrderType.FOK)
            log.info("[%s] order response: %s", q, resp)
            remaining_balance -= order_amount
        except Exception as e:
            log.error("[%s] order failed: %s", q, e)


def main() -> int:
    load_dotenv()
    cfg = load_config()
    try:
        client = build_client()
        funder = os.environ["POLYMARKET_FUNDER_ADDRESS"]
        if cfg.order_mode == "limit":
            run_once_limit(cfg, client, funder)
        elif cfg.order_mode == "market":
            run_once_market(cfg, client, funder)
        else:
            log.error("Unknown order_mode: %r", cfg.order_mode)
            return 1
    except KeyError as e:
        log.error("Missing env var: %s", e)
        return 1
    except Exception as e:
        log.exception("Run failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
