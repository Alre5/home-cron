"""
Backtest the "buy YES below threshold" strategy against past daily
"Will Trump publicly insult someone on <date>" markets.

For each historical market:
  1. Pull YES price history from the CLOB.
  2. For each threshold in config (e.g. 0.75, 0.80), find the first time
     the YES price crossed at/below it.
  3. Simulate buying `position_size_usdc` worth of YES at that price.
  4. Payoff = 1 USDC/share if YES resolved true, else 0.

Prints a per-threshold summary: trades taken, winrate, invested, returned, ROI.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
import yaml


MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_market_day_window(question: str) -> tuple[float, float] | None:
    """Return (start_ts, end_ts) of the ET resolution day mentioned in the question."""
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", question or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        # ET day: midnight ET == 04:00 UTC during EDT (April-Nov) or 05:00 during EST.
        # We approximate with EDT offset (-4) since backtest window sits in spring.
        start = datetime(int(m.group(3)), mon, int(m.group(2)), 4, 0, tzinfo=timezone.utc)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()
    except Exception:
        return None


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Identify daily "Will Trump publicly insult someone on <date>" markets only.
# The "someone" token excludes named-target variants like
# "Will Trump publicly insult Megyn Kelly by April 30" which have
# different price dynamics and are NOT what the bot trades.
MARKET_KEYWORDS_ALL = ["trump", "insult", "someone"]


@dataclass
class Tier:
    price: float
    usdc: float


@dataclass
class TieredStrategy:
    name: str
    tiers: list[Tier]


@dataclass
class BacktestConfig:
    thresholds: list[float]
    lookback_days: int
    position_size_usdc: float
    fidelity_minutes: int
    tiered_strategies: list[TieredStrategy]


def load_cfg(path: str = "config.yaml") -> tuple[BacktestConfig, str]:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    bt = raw["backtest"]
    strategies = []
    for s in bt.get("tiered_strategies", []):
        tiers = [Tier(price=float(t["price"]), usdc=float(t["usdc"])) for t in s["tiers"]]
        strategies.append(TieredStrategy(name=s["name"], tiers=tiers))
    return BacktestConfig(
        thresholds=[float(t) for t in bt["thresholds"]],
        lookback_days=int(bt["lookback_days"]),
        position_size_usdc=float(bt["position_size_usdc"]),
        fidelity_minutes=int(bt["fidelity_minutes"]),
        tiered_strategies=strategies,
    ), raw["event_slug"]


def parse_json_field(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def iso_to_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def is_target_market(m: dict) -> bool:
    q = (m.get("question") or "").lower()
    return all(k in q for k in MARKET_KEYWORDS_ALL)


def fetch_event_by_slug(slug: str) -> dict | None:
    r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=20)
    r.raise_for_status()
    events = r.json()
    return events[0] if events else None


def discover_related_past_events(keyword_chain: list[str]) -> list[dict]:
    """Paginate closed events and return any whose slug contains every keyword."""
    matches: list[dict] = []
    offset = 0
    page_size = 200
    pages = 0
    while True:
        r = requests.get(
            f"{GAMMA_API}/events",
            params={
                "closed": "true",
                "order": "endDate",
                "ascending": "false",
                "limit": page_size,
                "offset": offset,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json() or []
        if not data:
            break
        for e in data:
            slug = (e.get("slug") or "").lower()
            if all(k in slug for k in keyword_chain):
                matches.append(e)
        offset += page_size
        pages += 1
        if pages > 25:  # safety cap
            break
    return matches


def collect_resolved_markets(event_slug: str, lookback_days: int) -> list[dict]:
    """Pull resolved daily-insult markets from the current event plus any past
    events with a similar slug pattern."""
    cutoff = time.time() - lookback_days * 86400
    seen: set = set()
    out: list[dict] = []

    def take(markets: list[dict]) -> int:
        added = 0
        for m in markets or []:
            mid = m.get("id") or m.get("conditionId")
            if mid in seen:
                continue
            if not m.get("closed"):
                continue
            end_ts = iso_to_ts(m.get("endDate"))
            if end_ts is None or end_ts < cutoff:
                continue
            if not is_target_market(m):
                continue
            seen.add(mid)
            out.append(m)
            added += 1
        return added

    current = fetch_event_by_slug(event_slug)
    if current:
        n = take(current.get("markets", []))
        print(f"Current event '{event_slug}': {n} resolved daily markets")

    related = discover_related_past_events(["trump", "insult", "someone"])
    if related:
        print(f"Found {len(related)} related past events by slug pattern")
    for e in related:
        if e.get("slug") == event_slug:
            continue
        n = take(e.get("markets", []))
        if n:
            print(f"  + {n} from past event '{e.get('slug')}'")

    return out


def yes_token_id(m: dict) -> str | None:
    outcomes = parse_json_field(m.get("outcomes"))
    tokens = parse_json_field(m.get("clobTokenIds"))
    if len(outcomes) != len(tokens):
        return None
    for o, t in zip(outcomes, tokens):
        if str(o).strip().lower() == "yes":
            return t
    return None


def yes_resolved_true(m: dict) -> bool | None:
    prices = parse_json_field(m.get("outcomePrices"))
    outcomes = parse_json_field(m.get("outcomes"))
    if len(prices) != len(outcomes):
        return None
    for o, p in zip(outcomes, prices):
        if str(o).strip().lower() == "yes":
            try:
                return float(p) >= 0.99
            except Exception:
                return None
    return None


def fetch_price_history(token_id: str, fidelity_minutes: int) -> list[tuple[float, float]]:
    r = requests.get(
        f"{CLOB_API}/prices-history",
        params={"market": token_id, "interval": "all", "fidelity": fidelity_minutes},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    hist = data.get("history") or []
    return [(float(p["t"]), float(p["p"])) for p in hist]


def first_entry_at_or_below(history: list[tuple[float, float]], threshold: float) -> tuple[float, float] | None:
    for t, p in history:
        if p <= threshold:
            return t, p
    return None


@dataclass
class ThresholdStats:
    threshold: float
    trades: int = 0
    wins: int = 0
    skipped_no_entry: int = 0
    invested: float = 0.0
    returned: float = 0.0
    per_market_pnl: list[tuple[str, float, float, float, bool]] = None  # (question, entry_price, invested, pnl, won)

    def __post_init__(self):
        if self.per_market_pnl is None:
            self.per_market_pnl = []

    @property
    def pnl(self) -> float:
        return self.returned - self.invested

    @property
    def roi_pct(self) -> float:
        return (self.pnl / self.invested * 100) if self.invested > 0 else 0.0

    @property
    def winrate_pct(self) -> float:
        return (self.wins / self.trades * 100) if self.trades > 0 else 0.0


def simulate_tiered(markets_data: list[tuple[str, list[tuple[float, float]], bool]], strategy: TieredStrategy) -> dict:
    """Run the given scale-in strategy across all markets.

    Returns per-strategy totals and per-market breakdown.
    """
    invested = 0.0
    returned = 0.0
    trades = 0
    wins = 0
    per_market = []
    for q, hist, won in markets_data:
        m_invested = 0.0
        m_shares = 0.0
        tiers_hit: list[tuple[float, float, float]] = []
        for tier in strategy.tiers:
            entry = first_entry_at_or_below(hist, tier.price)
            if entry is None:
                continue
            _, price = entry
            shares = tier.usdc / price
            m_invested += tier.usdc
            m_shares += shares
            tiers_hit.append((tier.price, price, tier.usdc))
        if m_invested == 0:
            continue
        payoff = m_shares * (1.0 if won else 0.0)
        invested += m_invested
        returned += payoff
        trades += 1
        if won:
            wins += 1
        per_market.append({"q": q, "won": won, "invested": m_invested, "returned": payoff, "tiers": tiers_hit})
    return {
        "name": strategy.name,
        "invested": invested,
        "returned": returned,
        "pnl": returned - invested,
        "roi_pct": ((returned - invested) / invested * 100) if invested else 0.0,
        "trades": trades,
        "wins": wins,
        "winrate_pct": (wins / trades * 100) if trades else 0.0,
        "per_market": per_market,
    }


def run_backtest(cfg: BacktestConfig, event_slug: str) -> None:
    print(f"Fetching resolved markets (lookback={cfg.lookback_days}d)...")
    markets = collect_resolved_markets(event_slug, cfg.lookback_days)
    print(f"Found {len(markets)} resolved daily markets.\n")

    if not markets:
        print("No historical markets found. Nothing to backtest.")
        return

    stats = {t: ThresholdStats(threshold=t) for t in cfg.thresholds}
    min_prices: list[tuple[str, float, bool]] = []  # (question, min_price, won)
    markets_data: list[tuple[str, list[tuple[float, float]], bool]] = []

    for i, m in enumerate(markets, 1):
        q = m.get("question") or m.get("slug") or m.get("id")
        tid = yes_token_id(m)
        if not tid:
            continue

        won = yes_resolved_true(m)
        if won is None:
            continue  # couldn't determine outcome

        try:
            hist = fetch_price_history(tid, cfg.fidelity_minutes)
        except Exception as e:
            print(f"[{i}/{len(markets)}] skip {q!r}: price history error: {e}")
            continue

        if not hist:
            continue

        # Use the full price history — the live bot watches every open
        # sub-market at all times, not just during its resolution day, and
        # trades at pre-resolution-day prices are real opportunities with
        # real (if modest) volume behind them.
        min_p = min(p for _, p in hist)
        min_prices.append((q, min_p, won))
        markets_data.append((q, hist, won))

        # Informational: how many hours before the ET end-of-resolution-day
        # did the YES price first cross each level? Useful for the time-cutoff
        # safety, not for the sim itself.
        window = parse_market_day_window(q)
        time_info = ""
        if window is not None:
            _, wend = window
            for level in (0.60, 0.50):
                hit = first_entry_at_or_below(hist, level)
                if hit is None:
                    time_info += f"  {int(level*100)}%: n/a"
                else:
                    t_hit, _ = hit
                    hrs = (wend - t_hit) / 3600
                    time_info += f"  {int(level*100)}%: {hrs:>5.1f}h before EOD"
        print(f"[{i}/{len(markets)}] {q!r} — {'YES' if won else 'NO'}  min={min_p:.3f}{time_info}")

        for thr in cfg.thresholds:
            s = stats[thr]
            entry = first_entry_at_or_below(hist, thr)
            if entry is None:
                s.skipped_no_entry += 1
                continue
            _, entry_price = entry
            shares = cfg.position_size_usdc / entry_price
            payoff = shares * (1.0 if won else 0.0)
            pnl = payoff - cfg.position_size_usdc
            s.trades += 1
            if won:
                s.wins += 1
            s.invested += cfg.position_size_usdc
            s.returned += payoff
            s.per_market_pnl.append((q, entry_price, cfg.position_size_usdc, pnl, won))

    print("\n=== BACKTEST SUMMARY ===")
    print(f"Position size per trade: {cfg.position_size_usdc:.2f} USDC")
    print(f"Markets examined: {len(markets)}\n")
    print(f"{'Threshold':>10} {'Trades':>8} {'Wins':>6} {'WinRate':>9} {'Invested':>11} {'Returned':>11} {'PnL':>10} {'ROI':>8}")
    for thr in cfg.thresholds:
        s = stats[thr]
        print(
            f"{thr:>10.2f} {s.trades:>8d} {s.wins:>6d} "
            f"{s.winrate_pct:>8.1f}% {s.invested:>11.2f} {s.returned:>11.2f} "
            f"{s.pnl:>10.2f} {s.roi_pct:>7.1f}%"
        )
        if s.skipped_no_entry:
            print(f"             (skipped {s.skipped_no_entry} markets — price never hit threshold)")

    print("\n=== MIN-PRICE DISTRIBUTION ===")
    print("How often did the YES price dip to/below each level during the market's life?")
    print(f"(Based on {len(min_prices)} markets)\n")
    buckets = [0.90, 0.80, 0.75, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20]
    for lvl in buckets:
        n = sum(1 for _, p, _ in min_prices if p <= lvl)
        n_won = sum(1 for _, p, w in min_prices if p <= lvl and w)
        pct = (n / len(min_prices) * 100) if min_prices else 0
        wr = (n_won / n * 100) if n else 0
        print(f"  <= {lvl:.2f}  :  {n:>3d} / {len(min_prices):<3d}  ({pct:>5.1f}%)   winrate among those: {wr:>5.1f}%")

    print("\nPer-market min price (sorted low to high):")
    for q, p, won in sorted(min_prices, key=lambda x: x[1]):
        tag = "YES" if won else "NO "
        print(f"  min={p:.3f}  resolved {tag}  {q}")

    if cfg.tiered_strategies:
        print("\n=== TIERED / SCALE-IN STRATEGIES ===")
        header = f"{'Strategy':<25} {'Trades':>7} {'Wins':>5} {'WinRate':>9} {'Invested':>11} {'Returned':>11} {'PnL':>10} {'ROI':>8}"
        print(header)
        results = [simulate_tiered(markets_data, s) for s in cfg.tiered_strategies]
        for r in results:
            print(
                f"{r['name']:<25} {r['trades']:>7d} {r['wins']:>5d} "
                f"{r['winrate_pct']:>8.1f}% {r['invested']:>11.2f} {r['returned']:>11.2f} "
                f"{r['pnl']:>10.2f} {r['roi_pct']:>7.1f}%"
            )
        for r in results:
            print(f"\n  -- {r['name']} per-market --")
            for m in r["per_market"]:
                tag = "WIN " if m["won"] else "LOSS"
                tiers_str = ", ".join(f"@{tr_price:.2f}=>${amt}@{entry:.3f}" for tr_price, entry, amt in m["tiers"])
                pnl = m["returned"] - m["invested"]
                print(f"    {tag} inv=${m['invested']:.2f} ret=${m['returned']:.2f} pnl={pnl:+.2f}  [{tiers_str}]  {m['q']}")


def main() -> int:
    cfg, event_slug = load_cfg()
    run_backtest(cfg, event_slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
