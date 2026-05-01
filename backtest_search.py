"""
Massive grid-search + bootstrap backtest for the Trump-insult bot.

Pipeline:
  1. Fetch every resolved "Will Trump publicly insult someone on <date>" market
     (with full hourly price history) and cache the dataset on disk so the
     search can iterate without hitting the API.
  2. Pre-compute, per market and per threshold step, the actual entry price
     the live bot would have achieved (first time the YES ask crossed at or
     below that threshold). Apply the same time-cutoff filter as the live
     bot (skip an entry if fewer than `skip_if_minutes_remaining_below`
     minutes remained until the ET end-of-day at the time of crossing).
  3. Enumerate millions of candidate ladders (1-4 tiers, fine grids over
     trigger price and per-tier allocation). Score each ladder's portfolio
     ROI on the historical sample using vectorized numpy ops.
  4. Bootstrap-resample the markets thousands of times for the top-K
     ladders to estimate the robustness of their ROI (median, p5, p95,
     winrate). Rank by a worst-case metric so we don't pick a strategy
     that happened to nail one outlier.
  5. Translate the winning USDC-per-tier ladder into the live-bot
     `pct_of_balance` format and print the suggested config.yaml block.

Run: python backtest_search.py [--no-fetch] [--bootstrap N] [--top K]
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

import numpy as np
import requests
import yaml


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
CACHE_PATH = "backtest_cache.json"

MARKET_KEYWORDS_ALL = ["trump", "insult", "someone"]
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


# --- Data fetching (mirrors backtest.py, kept self-contained) ---

def parse_json_field(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def iso_to_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def is_target_market(m):
    q = (m.get("question") or "").lower()
    return all(k in q for k in MARKET_KEYWORDS_ALL)


def yes_token_id(m):
    outcomes = parse_json_field(m.get("outcomes"))
    tokens = parse_json_field(m.get("clobTokenIds"))
    if len(outcomes) != len(tokens):
        return None
    for o, t in zip(outcomes, tokens):
        if str(o).strip().lower() == "yes":
            return t
    return None


def yes_resolved_true(m):
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


def parse_market_day_window(question):
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", question or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        start = datetime(int(m.group(3)), mon, int(m.group(2)), 4, 0, tzinfo=timezone.utc)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()
    except Exception:
        return None


def fetch_event_by_slug(slug):
    r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=20)
    r.raise_for_status()
    events = r.json()
    return events[0] if events else None


def discover_related_past_events():
    matches = []
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
            if all(k in slug for k in MARKET_KEYWORDS_ALL):
                matches.append(e)
        offset += page_size
        pages += 1
        if pages > 25:
            break
    return matches


def fetch_price_history(token_id, fidelity_minutes):
    r = requests.get(
        f"{CLOB_API}/prices-history",
        params={"market": token_id, "interval": "all", "fidelity": fidelity_minutes},
        timeout=30,
    )
    r.raise_for_status()
    return [(float(p["t"]), float(p["p"])) for p in r.json().get("history") or []]


def collect_dataset(event_slug, lookback_days, fidelity_minutes):
    cutoff = time.time() - lookback_days * 86400
    seen = set()
    out = []

    def take(markets, source_label):
        nonlocal out
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
            won = yes_resolved_true(m)
            if won is None:
                continue
            tid = yes_token_id(m)
            if not tid:
                continue
            try:
                hist = fetch_price_history(tid, fidelity_minutes)
            except Exception as e:
                print(f"  ! price history failed for {m.get('question')!r}: {e}")
                continue
            if not hist:
                continue
            out.append({
                "question": m.get("question"),
                "won": bool(won),
                "history": hist,
                "source": source_label,
            })
            seen.add(mid)
            added += 1
        return added

    print(f"Fetching current event '{event_slug}'...")
    cur = fetch_event_by_slug(event_slug)
    if cur:
        n = take(cur.get("markets", []), event_slug)
        print(f"  + {n} resolved daily markets from current event")

    print("Discovering past events with slug pattern trump+insult+someone...")
    related = discover_related_past_events()
    related = [e for e in related if e.get("slug") != event_slug]
    print(f"  found {len(related)} related events")
    for e in related:
        n = take(e.get("markets", []), e.get("slug"))
        if n:
            print(f"  + {n} resolved markets from '{e.get('slug')}'")

    return out


def load_or_build_cache(event_slug, lookback_days, fidelity_minutes, force_refetch):
    if not force_refetch and os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if (
            cache.get("event_slug") == event_slug
            and cache.get("lookback_days") == lookback_days
            and cache.get("fidelity_minutes") == fidelity_minutes
        ):
            print(f"Loaded cached dataset: {len(cache['markets'])} markets")
            return cache["markets"]
        print("Cache parameters changed, refetching...")

    markets = collect_dataset(event_slug, lookback_days, fidelity_minutes)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "event_slug": event_slug,
            "lookback_days": lookback_days,
            "fidelity_minutes": fidelity_minutes,
            "markets": markets,
        }, f)
    print(f"Cached {len(markets)} markets to {CACHE_PATH}")
    return markets


# --- Pre-computation: entry-price matrix ---

def build_entry_matrix(
    markets,
    threshold_grid,
    skip_if_minutes_remaining_below,
):
    """For each market m and threshold k, the actual fill price the bot would
    have achieved at the first time the YES price crossed at or below
    threshold_grid[k] -- subject to the time-cutoff safety filter.

    Returns:
      entry_prices: (n_markets, n_thresholds) float, NaN where no entry.
      won:          (n_markets,) bool
      questions:    list[str]
    """
    n = len(markets)
    k = len(threshold_grid)
    entry = np.full((n, k), np.nan, dtype=np.float64)
    won = np.zeros(n, dtype=bool)
    questions = []

    for i, m in enumerate(markets):
        questions.append(m["question"])
        won[i] = m["won"]
        hist = m["history"]
        window = parse_market_day_window(m["question"])
        eod = window[1] if window else None

        # Walk history once, recording the first crossing for every threshold.
        # Track "remaining min thresholds" via a pointer over a sorted list
        # so we don't rescan the whole grid on every tick.
        pending = list(range(k))  # indices into threshold_grid still open
        # process in time order
        for t, p in hist:
            still_pending = []
            for idx in pending:
                if p <= threshold_grid[idx]:
                    if eod is not None and skip_if_minutes_remaining_below > 0:
                        mins_left = (eod - t) / 60.0
                        if mins_left < skip_if_minutes_remaining_below:
                            # too close to EOD to safely enter at this level
                            continue
                    entry[i, idx] = p
                else:
                    still_pending.append(idx)
            pending = still_pending
            if not pending:
                break

    return entry, won, questions


# --- Vectorized batch evaluation ---

def evaluate_ladders_batched(
    ladder_alloc_matrix,
    triggered,
    inv_price,
    won,
    batch_size=20000,
):
    """
    ladder_alloc_matrix: (L, K) -- per-ladder allocation per threshold-index,
                                   zero where the ladder does not use that threshold.
    triggered:           (M, K) -- 1.0 where market m has a real entry at threshold k
    inv_price:           (M, K) -- 1/entry_price (0 where not triggered)
    won:                 (M,)   -- 1.0 if YES resolved true
    Returns:
      total_inv  (L,)
      total_pay  (L,)
      n_trades   (L,)  count of markets where ladder spent >0
      n_wins     (L,)
    """
    L = ladder_alloc_matrix.shape[0]
    M = triggered.shape[0]
    total_inv = np.zeros(L, dtype=np.float64)
    total_pay = np.zeros(L, dtype=np.float64)
    n_trades = np.zeros(L, dtype=np.int32)
    n_wins = np.zeros(L, dtype=np.int32)

    won_f = won.astype(np.float64)
    # Pre-cast for the matmul.
    trig_T = triggered.T.astype(np.float64)        # (K, M)
    inv_T = inv_price.T.astype(np.float64)         # (K, M)

    for start in range(0, L, batch_size):
        end = min(start + batch_size, L)
        L_chunk = ladder_alloc_matrix[start:end]   # (b, K)

        invested_per_market = L_chunk @ trig_T     # (b, M)
        shares_per_market = L_chunk @ inv_T        # (b, M)

        payoff_per_market = shares_per_market * won_f[None, :]
        total_inv[start:end] = invested_per_market.sum(axis=1)
        total_pay[start:end] = payoff_per_market.sum(axis=1)

        traded = invested_per_market > 1e-9
        n_trades[start:end] = traded.sum(axis=1)
        n_wins[start:end] = (traded & (won_f[None, :] > 0.5)).sum(axis=1)

    return total_inv, total_pay, n_trades, n_wins


# --- Ladder generation ---

def generate_ladders(
    price_grids_by_n_tiers,
    alloc_grids_by_n_tiers,
    n_thresholds,
    threshold_index,  # dict: rounded price -> column index in entry matrix
):
    """Yield dense allocation rows of length n_thresholds, one per ladder.

    Ladders are emitted as (alloc_row, descriptor) where descriptor is a
    list of (price, alloc) for reporting. Only valid ladders (strictly
    decreasing prices, distinct) are emitted.
    """
    for n_tiers, prices in price_grids_by_n_tiers.items():
        allocs = alloc_grids_by_n_tiers[n_tiers]
        # All combinations of n_tiers distinct prices, sorted descending.
        for price_combo in itertools.combinations(sorted(prices, reverse=True), n_tiers):
            cols = [threshold_index[round(p, 4)] for p in price_combo]
            for alloc_combo in itertools.product(allocs, repeat=n_tiers):
                row = np.zeros(n_thresholds, dtype=np.float64)
                for col, a in zip(cols, alloc_combo):
                    row[col] = a
                desc = list(zip(price_combo, alloc_combo))
                yield row, desc


def count_ladders(price_grids_by_n_tiers, alloc_grids_by_n_tiers):
    total = 0
    breakdown = {}
    for n_tiers, prices in price_grids_by_n_tiers.items():
        np_ = len(prices)
        na = len(alloc_grids_by_n_tiers[n_tiers])
        c = math.comb(np_, n_tiers) * (na ** n_tiers)
        breakdown[n_tiers] = c
        total += c
    return total, breakdown


# --- Bootstrap ---

def bootstrap_top(
    top_alloc_rows,         # (K_top, n_thresholds)
    triggered, inv_price, won,
    n_resamples,
    rng_seed=42,
):
    """For each top ladder, run n_resamples bootstrap resamples of the
    market sample and return per-ladder ROI / PnL-per-market percentiles.
    """
    rng = np.random.default_rng(rng_seed)
    M = triggered.shape[0]
    K_top = top_alloc_rows.shape[0]

    won_f = won.astype(np.float64)

    invested_pm = top_alloc_rows @ triggered.T.astype(np.float64)  # (K_top, M)
    shares_pm = top_alloc_rows @ inv_price.T.astype(np.float64)    # (K_top, M)
    payoff_pm = shares_pm * won_f[None, :]
    pnl_pm = payoff_pm - invested_pm                                # (K_top, M)

    rois = np.zeros((K_top, n_resamples), dtype=np.float64)
    pnl_per_market_arr = np.zeros((K_top, n_resamples), dtype=np.float64)
    win_counts = np.zeros((K_top, n_resamples), dtype=np.int32)
    trade_counts = np.zeros((K_top, n_resamples), dtype=np.int32)

    sample_size = M
    for r in range(n_resamples):
        idx = rng.integers(0, M, size=sample_size)
        inv_r = invested_pm[:, idx].sum(axis=1)
        pnl_r = pnl_pm[:, idx].sum(axis=1)
        roi = np.where(inv_r > 1e-9, pnl_r / inv_r, 0.0)
        rois[:, r] = roi
        pnl_per_market_arr[:, r] = pnl_r / sample_size
        traded = invested_pm[:, idx] > 1e-9
        wins = traded & (won_f[idx] > 0.5)[None, :]
        trade_counts[:, r] = traded.sum(axis=1)
        win_counts[:, r] = wins.sum(axis=1)

    out = {}
    for k in range(K_top):
        r = rois[k]
        ppm = pnl_per_market_arr[k]
        wc = win_counts[k].sum()
        tc = trade_counts[k].sum()
        out[k] = {
            "roi_mean": float(r.mean()),
            "roi_p5": float(np.percentile(r, 5)),
            "roi_median": float(np.median(r)),
            "roi_p95": float(np.percentile(r, 95)),
            "ppm_mean": float(ppm.mean()),
            "ppm_p5": float(np.percentile(ppm, 5)),
            "ppm_median": float(np.median(ppm)),
            "ppm_p95": float(np.percentile(ppm, 95)),
            "win_rate": (wc / tc) if tc else 0.0,
            "neg_share": float((r < 0).mean()),
        }
    return out


# --- Top-level ---

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true",
                    help="Use cached dataset; do not re-fetch.")
    ap.add_argument("--force-fetch", action="store_true",
                    help="Force re-fetching even if cache is fresh.")
    ap.add_argument("--bootstrap", type=int, default=5000,
                    help="Bootstrap resamples per top ladder (default 5000).")
    ap.add_argument("--top", type=int, default=30,
                    help="How many top ladders to bootstrap (default 30).")
    ap.add_argument("--time-cutoff", type=float, default=None,
                    help="Override skip_if_minutes_remaining_below; defaults to config value.")
    args = ap.parse_args(argv)

    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg_raw = yaml.safe_load(f)
    event_slug = cfg_raw["event_slug"]
    bt = cfg_raw["backtest"]
    lookback_days = int(bt["lookback_days"])
    fidelity_minutes = int(bt["fidelity_minutes"])
    time_cutoff = (
        args.time_cutoff
        if args.time_cutoff is not None
        else float(cfg_raw.get("skip_if_minutes_remaining_below", 0))
    )

    # 1. dataset
    markets = load_or_build_cache(
        event_slug=event_slug,
        lookback_days=lookback_days,
        fidelity_minutes=fidelity_minutes,
        force_refetch=args.force_fetch,
    )
    if args.no_fetch and not markets:
        print("No cached data. Run without --no-fetch first.")
        return 1
    if not markets:
        print("No markets fetched. Aborting.")
        return 1

    print(f"\nDataset: {len(markets)} resolved daily markets")
    n_yes = sum(1 for m in markets if m["won"])
    print(f"  resolved YES: {n_yes}")
    print(f"  resolved NO:  {len(markets) - n_yes}")
    print(f"  observed YES rate: {n_yes/len(markets)*100:.1f}%")

    # 2. entry matrix
    threshold_grid = np.round(np.arange(0.50, 0.991, 0.01), 4)  # 0.50..0.99
    threshold_index = {float(p): i for i, p in enumerate(threshold_grid)}

    print(f"\nBuilding entry matrix ({len(markets)} markets x {len(threshold_grid)} thresholds)...")
    print(f"  applying live time-cutoff filter: skip if < {time_cutoff:.0f} min remaining")
    entry, won, questions = build_entry_matrix(markets, threshold_grid, time_cutoff)
    triggered = (~np.isnan(entry)).astype(np.float64)
    inv_price = np.where(np.isnan(entry), 0.0, 1.0 / np.where(np.isnan(entry), 1.0, entry))
    print(f"  triggered cells: {int(triggered.sum())} / {triggered.size}")

    # 3. ladder grids
    fine_prices = [round(p, 4) for p in np.arange(0.50, 0.991, 0.01)]   # 50 prices
    med_prices = [round(p, 4) for p in np.arange(0.50, 0.991, 0.02)]    # 25 prices
    coarse_prices = [round(p, 4) for p in np.arange(0.50, 0.991, 0.03)] # 17 prices
    fine_allocs = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]      # 8
    med_allocs = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]                   # 6

    price_grids = {
        1: fine_prices,
        2: fine_prices,
        3: med_prices,
        4: coarse_prices,
    }
    alloc_grids = {
        1: fine_allocs,
        2: fine_allocs,
        3: fine_allocs,
        4: med_allocs,
    }

    total, breakdown = count_ladders(price_grids, alloc_grids)
    print(f"\nLadder grid: {total:,} configurations")
    for n_tiers, c in breakdown.items():
        print(f"  {n_tiers}-tier: {c:,}")

    # Materialize alloc matrix in chunks and evaluate batched.
    K = len(threshold_grid)
    print("\nEvaluating...")
    t0 = time.time()

    M = triggered.shape[0]
    keep_top = max(args.top * 50, 2000)  # large pool so we can re-rank by multiple metrics
    # Each entry: (roi, ppm, total_inv, total_pay, n_trades, n_wins, alloc_row, descriptor, sig)
    top_pool = []

    chunk_alloc_rows = np.zeros((50000, K), dtype=np.float64)
    chunk_descs = []
    chunk_n = 0
    total_evaluated = 0

    def flush_chunk():
        nonlocal chunk_n
        if chunk_n == 0:
            return
        rows = chunk_alloc_rows[:chunk_n]
        inv, pay, ntr, nwn = evaluate_ladders_batched(rows, triggered, inv_price, won)
        pnls = pay - inv
        ppms = pnls / M
        rois = np.where(inv > 1e-9, pnls / inv, -np.inf)
        # signature for dedupe: rounded (roi, ppm, ntr) — equivalent ladders collapse
        for i in range(chunk_n):
            sig = (round(float(rois[i]), 4), round(float(ppms[i]), 4), int(ntr[i]))
            top_pool.append((
                float(rois[i]),
                float(ppms[i]),
                float(inv[i]),
                float(pay[i]),
                int(ntr[i]),
                int(nwn[i]),
                rows[i].copy(),
                chunk_descs[i],
                sig,
            ))
        # Keep top keep_top by ppm (the metric that respects opportunity cost)
        top_pool.sort(key=lambda t: -t[1])
        del top_pool[keep_top:]
        chunk_n = 0
        chunk_descs.clear()

    for alloc_row, desc in generate_ladders(price_grids, alloc_grids, K, threshold_index):
        chunk_alloc_rows[chunk_n] = alloc_row
        chunk_descs.append(desc)
        chunk_n += 1
        total_evaluated += 1
        if chunk_n == chunk_alloc_rows.shape[0]:
            flush_chunk()
            if total_evaluated % 500000 == 0 or total_evaluated == 50000:
                elapsed = time.time() - t0
                rate = total_evaluated / elapsed if elapsed > 0 else 0
                print(f"  evaluated {total_evaluated:,} / {total:,}  ({rate:,.0f}/s, {elapsed:.1f}s elapsed)")
    flush_chunk()
    elapsed = time.time() - t0
    print(f"  done: {total_evaluated:,} ladders in {elapsed:.1f}s ({total_evaluated/elapsed:,.0f}/s)")

    # Dedupe by signature, keeping the simplest (fewest tiers) representative.
    by_sig = {}
    for entry in top_pool:
        sig = entry[8]
        prev = by_sig.get(sig)
        if prev is None or len(entry[7]) < len(prev[7]):
            by_sig[sig] = entry
    deduped = list(by_sig.values())
    # Re-rank by ppm desc, then roi desc
    deduped.sort(key=lambda t: (-t[1], -t[0]))
    print(f"  after dedupe by (roi, ppm, n_trades): {len(deduped)} unique ladders")

    # 4. bootstrap top-K
    K_top = min(args.top, len(deduped))
    top_for_boot = deduped[:K_top]
    print(f"\nBootstrapping top {K_top} ladders by PnL/market with {args.bootstrap} resamples...")
    top_alloc_matrix = np.array([t[6] for t in top_for_boot])
    boot = bootstrap_top(top_alloc_matrix, triggered, inv_price, won, args.bootstrap)

    def print_table(title, ranking_fn):
        print(f"\n=== {title} ===")
        print(f"{'rk':>3} {'ladder':<48} {'pnl/mkt':>9} {'ROI':>8} {'n_tr':>5} "
              f"{'b.ppm med':>10} {'b.ppm p5':>9} {'b.roi p5':>9} {'b.wr':>6}")
        ranked = sorted(range(K_top), key=ranking_fn)
        for rk, i in enumerate(ranked[:args.top], 1):
            roi, ppm, inv, pay, ntr, nwn, _, desc, _ = top_for_boot[i]
            b = boot[i]
            ladder_str = " | ".join(f"<={p:.2f}@{a*100:.0f}%" for p, a in desc)
            print(
                f"{rk:>3} {ladder_str:<48} {ppm:>+9.4f} {roi*100:>7.2f}% "
                f"{ntr:>5d} {b['ppm_median']:>+10.4f} {b['ppm_p5']:>+9.4f} "
                f"{b['roi_p5']*100:>+8.2f}% {b['win_rate']*100:>5.1f}%"
            )
        return ranked

    print_table(
        "TOP BY POINT PnL-per-market  [most $ per market available — primary metric]",
        lambda i: -top_for_boot[i][1],
    )
    print_table(
        "TOP BY BOOTSTRAP MEDIAN PnL-per-market  [resampling-robust]",
        lambda i: -boot[i]["ppm_median"],
    )
    print_table(
        "TOP BY POINT ROI  [highest return per dollar deployed; can pick low-fire ladders]",
        lambda i: -top_for_boot[i][0],
    )

    # Per-market detail for the top-by-ppm ladder
    winner = top_for_boot[0]
    win_alloc, win_desc = winner[6], winner[7]
    win_inv_pm = win_alloc @ triggered.T.astype(np.float64)
    win_shares_pm = win_alloc @ inv_price.T.astype(np.float64)
    win_pay_pm = win_shares_pm * won.astype(np.float64)
    win_pnl_pm = win_pay_pm - win_inv_pm
    print(f"\n=== PER-MARKET DETAIL FOR TOP-BY-PPM: " +
          " | ".join(f"<={p:.2f}@{a*100:.0f}%" for p, a in win_desc) + " ===")
    print(f"{'won':>4}  {'inv':>7}  {'pay':>7}  {'pnl':>8}  min_p   first_dip_below_0.80   question")
    order = np.argsort(-win_pnl_pm)
    for i in order:
        tag = "YES" if won[i] else "NO "
        m = markets[i]
        hist = m["history"]
        min_p = min(p for _, p in hist) if hist else float("nan")
        first_dip = next(((t, p) for t, p in hist if p <= 0.80), None)
        if first_dip is None:
            dip_str = "never"
        else:
            t, p = first_dip
            window = parse_market_day_window(m["question"])
            if window is not None:
                hrs = (window[1] - t) / 3600.0
                dip_str = f"{hrs:>5.1f}h before EOD @ {p:.3f}"
            else:
                dip_str = f"@{p:.3f}"
        print(f"  {tag}  {win_inv_pm[i]:>7.4f}  {win_pay_pm[i]:>7.4f}  {win_pnl_pm[i]:>+8.4f}  {min_p:.3f}  {dip_str:<22}  {questions[i]}")

    # 5. winner -> config snippet (live ladder uses pct_of_balance per market)
    print("\n=== SUGGESTED config.yaml entry_ladder (top by PnL/market) ===")
    print("# Allocations interpret as pct_of_balance per market (matches live bot).")
    print("entry_ladder:")
    sorted_desc = sorted(win_desc, key=lambda x: -x[0])
    cumulative = 0.0
    for price, alloc in sorted_desc:
        print(f"  - price: {price:.2f}")
        print(f"    pct_of_balance: {alloc:.2f}")
        cumulative += alloc
    print(f"# Cumulative deployment if all tiers fire: {cumulative*100:.0f}% of balance per market")

    # --- Reference ladders: hand-picked operating points to compare against the optimum ---
    print("\n=== REFERENCE LADDERS (hand-picked operating points) ===")
    print("Each line shows the same metrics on the cached dataset.")
    print(f"{'name':<35} {'pnl/mkt':>9} {'ROI':>8} {'n_tr':>5} {'inv':>8} {'pay':>8}  ladder")
    references = [
        ("current_live (0.95/0.85/0.75)",
         [(0.95, 0.20), (0.85, 0.30), (0.75, 0.30)]),
        ("old_live (0.80/0.70)",
         [(0.80, 0.10), (0.70, 0.20)]),
        ("hybrid_high+deep",
         [(0.92, 0.10), (0.80, 0.20), (0.65, 0.30), (0.55, 0.40)]),
        ("hybrid_balanced",
         [(0.90, 0.15), (0.75, 0.25), (0.60, 0.40)]),
        ("aggressive_high",
         [(0.93, 0.30), (0.85, 0.30), (0.70, 0.40)]),
        ("deep_only_optimum",
         [(0.58, 0.40), (0.56, 0.40), (0.52, 0.40)]),
    ]
    # Build alloc rows for these ladders. Round prices to nearest threshold-grid step.
    ref_rows = []
    ref_descs = []
    for name, tiers in references:
        row = np.zeros(K, dtype=np.float64)
        rendered = []
        for price, alloc in tiers:
            # snap to nearest threshold in grid
            nearest = float(threshold_grid[int(round((price - 0.50) / 0.01))])
            row[threshold_index[round(nearest, 4)]] += alloc
            rendered.append((nearest, alloc))
        ref_rows.append(row)
        ref_descs.append((name, rendered))
    ref_matrix = np.array(ref_rows)
    inv_r, pay_r, ntr_r, nwn_r = evaluate_ladders_batched(ref_matrix, triggered, inv_price, won)
    pnl_r = pay_r - inv_r
    ppm_r = pnl_r / M
    roi_r = np.where(inv_r > 1e-9, pnl_r / inv_r, 0.0)
    for i, (name, desc) in enumerate(ref_descs):
        ladder_str = " | ".join(f"<={p:.2f}@{a*100:.0f}%" for p, a in desc)
        print(f"{name:<35} {ppm_r[i]:>+9.4f} {roi_r[i]*100:>7.2f}% {ntr_r[i]:>5d} {inv_r[i]:>8.2f} {pay_r[i]:>8.2f}  {ladder_str}")

    # --- SURVIVAL ANALYSIS: Monte-Carlo wealth paths under stress ---
    # For each candidate ladder, simulate N independent trade-decisions at
    # different *assumed* true YES rates (the historical 93.8% may be lucky).
    # Wealth compounds geometrically: W(t+1) = W(t) * (1 + r_t) where
    # r_t is the per-unit return of the ladder on a sampled market history
    # paired with an independently sampled win/lose outcome.
    print("\n=== SURVIVAL ANALYSIS (geometric compounding, 1 trade decision per simulated day) ===")
    print("Each ladder is stressed under several true-YES-rate assumptions; price dips are sampled")
    print("from the historical pool but the OUTCOME is re-rolled at the assumed rate (so the rare-NO")
    print("dynamics aren't tied to the lucky 15/16 ratio).")

    # Ladders to stress-test
    survival_ladders = [
        ("current_live (0.92/0.80/0.65/0.55, cum 100%)",
         [(0.92, 0.10), (0.80, 0.20), (0.65, 0.30), (0.55, 0.40)]),
        ("conservative_proposal (0.80/0.70/0.60/0.50, cum 50%)",
         [(0.80, 0.05), (0.70, 0.10), (0.60, 0.15), (0.50, 0.20)]),
        ("half_kelly_deep (0.70/0.60/0.55, cum 30%)",
         [(0.70, 0.05), (0.60, 0.10), (0.55, 0.15)]),
        ("deep_only_optimum (0.58/0.56/0.52, cum 120%)",
         [(0.58, 0.40), (0.56, 0.40), (0.52, 0.40)]),
        ("ultra_safe (0.65/0.55, cum 20%)",
         [(0.65, 0.05), (0.55, 0.15)]),
    ]

    # Pre-compute per-market (invested, shares) for each ladder
    surv_results = []
    rng = np.random.default_rng(20260429)
    n_days = 365
    n_traj = 5000
    rates_to_test = [0.94, 0.85, 0.80, 0.75, 0.70]

    print(f"\nSimulating {n_traj:,} trajectories x {n_days} days for each (ladder, rate) pair.")
    print("Outputs per row: median final wealth (W=1.0 start), p5, p95, max drawdown median, P(ruin: W<0.2).\n")

    for name, tiers in survival_ladders:
        row = np.zeros(K, dtype=np.float64)
        for price, alloc in tiers:
            nearest = float(threshold_grid[int(round((price - 0.50) / 0.01))])
            row[threshold_index[round(nearest, 4)]] += alloc
        invested_pm = (row @ triggered.T.astype(np.float64))    # (M,)
        shares_pm = (row @ inv_price.T.astype(np.float64))      # (M,)

        rate_results = []
        for r in rates_to_test:
            day_idx = rng.integers(0, M, size=(n_traj, n_days))
            outcomes = (rng.random(size=(n_traj, n_days)) < r).astype(np.float64)
            inv_t = invested_pm[day_idx]                          # (T, D)
            shares_t = shares_pm[day_idx]
            payoff_t = shares_t * outcomes
            ret_t = payoff_t - inv_t                              # per-unit-wealth return
            # Cap one-day loss at 100% (can't go below zero); equivalent to capping ret at -1
            ret_t = np.clip(ret_t, -0.99, None)
            growth = 1.0 + ret_t
            log_growth = np.log(growth)
            cum_log = np.cumsum(log_growth, axis=1)
            wealth = np.exp(cum_log)                              # (T, D)
            final = wealth[:, -1]
            running_max = np.maximum.accumulate(wealth, axis=1)
            drawdown = (running_max - wealth) / running_max
            max_dd = drawdown.max(axis=1)
            ruin = (wealth.min(axis=1) < 0.20).mean()
            rate_results.append({
                "rate": r,
                "med_final": float(np.median(final)),
                "p5_final": float(np.percentile(final, 5)),
                "p95_final": float(np.percentile(final, 95)),
                "med_max_dd": float(np.median(max_dd)),
                "p_ruin_20": float(ruin),
                "ann_roi": float(np.median(final) ** (1.0 / (n_days / 365.0)) - 1.0),
            })
        surv_results.append((name, rate_results))

    # Print as one table per rate, ladder rows
    for r_idx, r in enumerate(rates_to_test):
        print(f"--- assumed true YES rate = {r*100:.0f}% ---")
        print(f"{'ladder':<55} {'med_W365':>9} {'p5_W365':>9} {'p95_W365':>9} {'med_DD':>8} {'P(ruin)':>9} {'ann_ROI':>9}")
        for name, rates in surv_results:
            d = rates[r_idx]
            print(f"{name:<55} {d['med_final']:>9.3f} {d['p5_final']:>9.3f} {d['p95_final']:>9.3f} "
                  f"{d['med_max_dd']*100:>7.1f}% {d['p_ruin_20']*100:>8.2f}% {d['ann_roi']*100:>+8.2f}%")
        print()

    print("Reading the table:")
    print("  - W365 is wealth after 365 simulated trade-days starting from 1.0.")
    print("    >1.0 = profit, <1.0 = loss. p5 is the unlucky-trajectory floor.")
    print("  - med_DD is the median peak-to-trough drawdown along the path.")
    print("  - P(ruin) is the % of trajectories where wealth ever fell below 0.2 (lost 80%+).")
    print("  - ann_ROI is the annualized return at the median trajectory.")

    print("\nCAVEAT: dataset is small (16 resolved markets). Bootstrap CIs are noisy. The survival sim")
    print("decouples win-rate from price dynamics; in practice they may correlate (markets sometimes")
    print("price the outcome correctly). Treat the assumed-rate column you trust as your decision input.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
