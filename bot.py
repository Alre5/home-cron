"""
Polymarket "Will Trump publicly insult someone" bot.

Single-execution entrypoint. Intended to be invoked on a schedule
(GitHub Actions cron every ~5 min). Fetches the event's daily markets,
and buys YES using a scale-in ladder: each tier triggers at a progressively
lower price and deploys a configured % of balance.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

import requests
import yaml
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderType,
)


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
    event_slug: str
    entry_ladder: list[LadderTier]
    min_order_usdc: float
    order_decimals: int
    tier_fill_tolerance: float
    skip_if_minutes_remaining_below: float


def load_config(path: str = "config.yaml") -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    ladder_raw = raw["entry_ladder"]
    # Sort shallow-first so tier 1 is the highest price (first entry on first dip).
    ladder = sorted(
        [LadderTier(price=float(t["price"]), pct_of_balance=float(t["pct_of_balance"])) for t in ladder_raw],
        key=lambda t: -t.price,
    )
    return Config(
        event_slug=raw["event_slug"],
        entry_ladder=ladder,
        min_order_usdc=float(raw["min_order_usdc"]),
        order_decimals=int(raw["order_decimals"]),
        tier_fill_tolerance=float(raw["tier_fill_tolerance"]),
        skip_if_minutes_remaining_below=float(raw.get("skip_if_minutes_remaining_below", 0)),
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
        from py_clob_client.clob_types import ApiCreds
        client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass))
    else:
        client.set_api_creds(client.create_or_derive_api_creds())
    return client


def fetch_event(slug: str) -> dict:
    r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=15)
    r.raise_for_status()
    events = r.json()
    if not events:
        raise RuntimeError(f"No event found for slug {slug!r}")
    return events[0]


def parse_json_field(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
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
    prices = [float(a.price) for a in asks]
    return min(prices)


def get_balance_usdc(client: ClobClient) -> float:
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    res = client.get_balance_allowance(params)
    raw = res.get("balance") if isinstance(res, dict) else res
    return int(raw) / 1_000_000


def get_yes_position(proxy_address: str, condition_id: str) -> tuple[float, float]:
    """Return (size_shares, avg_fill_price) for the YES outcome of this market."""
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": proxy_address},
            timeout=15,
        )
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


def cumulative_ladder_targets(balance: float, ladder: list[LadderTier]) -> list[float]:
    """Cumulative USDC target after filling each tier (in order)."""
    out = []
    cum = 0.0
    for t in ladder:
        cum += t.pct_of_balance * balance
        out.append(cum)
    return out


# --- Strategy ---

def run_once(cfg: Config) -> None:
    client = build_client()
    funder = os.environ["POLYMARKET_FUNDER_ADDRESS"]

    balance = get_balance_usdc(client)
    log.info("USDC balance: %.4f", balance)
    if balance < cfg.min_order_usdc:
        log.info("Balance below min_order_usdc, nothing to do.")
        return

    ladder_desc = ", ".join(f"<= {t.price:.2f} -> {t.pct_of_balance*100:.1f}% of balance" for t in cfg.entry_ladder)
    log.info("Entry ladder: %s", ladder_desc)

    targets = cumulative_ladder_targets(balance, cfg.entry_ladder)

    event = fetch_event(cfg.event_slug)
    markets = event.get("markets", [])
    log.info("Event '%s' has %d sub-markets", event.get("title", cfg.event_slug), len(markets))

    remaining_balance = balance

    for m in markets:
        q = m.get("question") or m.get("slug") or m.get("id")
        if not market_is_tradeable(m):
            continue

        tid = yes_token_id(m)
        if not tid:
            log.warning("[%s] no YES token id, skipping", q)
            continue

        ask = best_ask(client, tid)
        if ask is None:
            log.info("[%s] no asks on book, skipping", q)
            continue

        shallowest_trigger = cfg.entry_ladder[0].price
        if ask > shallowest_trigger:
            log.info("[%s] ask %.3f > %.3f (top of ladder), skip", q, ask, shallowest_trigger)
            continue

        if cfg.skip_if_minutes_remaining_below > 0:
            mins_left = minutes_until_resolution_day_end(q)
            if mins_left is None:
                log.warning("[%s] could not parse resolution date, not applying time cutoff", q)
            elif mins_left < cfg.skip_if_minutes_remaining_below:
                log.info(
                    "[%s] only %.1f min left until end of day (< %.0f), skip",
                    q, mins_left, cfg.skip_if_minutes_remaining_below,
                )
                continue

        cid = m.get("conditionId", "")
        size, avg = get_yes_position(funder, cid)
        current_spent = size * avg

        # Find the deepest tier whose price is still accessible.
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
            log.info("[%s] tier %d already filled (spent %.2f / target %.2f)", q, deepest_idx + 1, current_spent, target_spend)
            continue

        order_amount = round_down(gap, cfg.order_decimals)
        if order_amount < cfg.min_order_usdc:
            log.info("[%s] gap %.2f below min_order %.2f, skip", q, gap, cfg.min_order_usdc)
            continue

        if order_amount > remaining_balance:
            if remaining_balance < cfg.min_order_usdc:
                log.info("[%s] remaining balance %.2f below min_order, stop", q, remaining_balance)
                break
            order_amount = round_down(remaining_balance, cfg.order_decimals)
            log.info("[%s] capping order to remaining balance %.2f", q, order_amount)

        log.info(
            "[%s] BUY tier %d -> %.2f USDC @ ask %.3f (current pos %.2f shares @ %.3f avg)",
            q, deepest_idx + 1, order_amount, ask, size, avg,
        )
        try:
            order_args = MarketOrderArgs(token_id=tid, amount=order_amount)
            signed = client.create_market_order(order_args)
            resp = client.post_order(signed, OrderType.FOK)
            log.info("[%s] order response: %s", q, resp)
            remaining_balance -= order_amount
        except Exception as e:
            log.error("[%s] order failed: %s", q, e)


def main() -> int:
    load_dotenv()
    cfg = load_config()
    try:
        run_once(cfg)
    except KeyError as e:
        log.error("Missing env var: %s", e)
        return 1
    except Exception as e:
        log.exception("Run failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
