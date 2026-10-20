"""Measure local incremental quote processing; no network or orders.

Run: python3 benchmark_streaming.py --pairs=1000 --updates=20000
"""

from __future__ import annotations

import argparse
import json
import time

from streaming import Comparison, LiveQuoteEngine, QuoteUpdate


def benchmark(pairs: int, updates: int) -> dict:
    if pairs < 1 or updates < 1:
        raise ValueError("pairs and updates must be positive")
    comparisons = [Comparison(
        str(index), ("kalshi", str(index), "team"),
        ("polymarket_intl", str(index), "team"),
        True, "synthetic_benchmark", "aligned", ()) for index in range(pairs)]
    engine = LiveQuoteEngine(comparisons)
    for comparison in comparisons:
        for key in (comparison.left, comparison.right):
            engine.apply(QuoteUpdate(key, bid="0.4", ask="0.5", bid_size="10", ask_size="10"))
    samples = []
    started = time.perf_counter_ns()
    for index in range(updates):
        update = QuoteUpdate(comparisons[index % pairs].left, bid="0.6", ask="0.7",
                             bid_size="10", ask_size="10", sequence=index + 1)
        before = time.perf_counter_ns()
        result = engine.apply(update)
        samples.append(time.perf_counter_ns() - before)
        if not result.applied:
            raise RuntimeError(result.reason)
    elapsed = time.perf_counter_ns() - started
    samples.sort()
    return {
        "scope": "local synthetic quote processing; excludes network and order execution",
        "pairs": pairs, "instruments": pairs * 2, "updates": updates,
        "apply_median_us": round(samples[len(samples) // 2] / 1000, 3),
        "apply_p95_us": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))] / 1000, 3),
        "apply_p99_us": round(samples[min(len(samples) - 1, int(len(samples) * 0.99))] / 1000, 3),
        "updates_per_second_including_message_construction": round(updates / (elapsed / 1e9)),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=1000)
    parser.add_argument("--updates", type=int, default=20000)
    arguments = parser.parse_args()
    print(json.dumps(benchmark(arguments.pairs, arguments.updates), indent=2))
