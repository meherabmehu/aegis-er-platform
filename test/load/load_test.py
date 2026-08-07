"""Lightweight load tester for the AEGIS-ER API.

Sends a sustained stream of random incidents and reports latencies.
Usage:
    AEGIS_API=http://localhost:8000 CONCURRENCY=50 DURATION=30 python load_test.py
"""
from __future__ import annotations

import os
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request, error
import json

API = os.environ.get("AEGIS_API", "http://localhost:8000").rstrip("/")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "50"))
DURATION = float(os.environ.get("DURATION", "30"))
RNG = random.Random(42)


def post(path: str, body: dict) -> tuple[int, float]:
    data = json.dumps(body).encode()
    t0 = time.perf_counter()
    req = request.Request(API + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=5) as resp:
            resp.read()
            code = resp.status
    except error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    return code, time.perf_counter() - t0


def generate_incident():
    lat = RNG.uniform(22.0, 25.5)
    lon = RNG.uniform(88.5, 92.5)
    return {
        "type": RNG.choices(
            ["medical", "crash", "fire", "flood", "collapse", "rescue"],
            weights=[0.45, 0.2, 0.12, 0.08, 0.08, 0.07])[0],
        "severity": RNG.choices([1, 2, 3, 4, 5], weights=[0.2, 0.25, 0.3, 0.2, 0.05])[0],
        "affected_count": max(1, int(RNG.lognormvariate(0.5, 0.8))),
        "time_sensitivity_min": RNG.choice([5, 8, 10, 15, 20]),
        "notes": "",
        "region_id": "bd",
        "location": {"lat": lat, "lon": lon},
        "weather": RNG.choices(["clear", "rain", "storm", "fog"], weights=[0.7, 0.15, 0.08, 0.07])[0],
        "road_status": RNG.choices(["open", "congested", "closed"], weights=[0.9, 0.07, 0.03])[0],
        "hazard": None,
    }


def worker(stop_at: float, stats: dict, lock: threading.Lock):
    while time.time() < stop_at:
        code, dt = post("/api/incidents", generate_incident())
        with lock:
            stats["total"] += 1
            stats["latencies"].append(dt)
            if code == 200:
                stats["ok"] += 1
            else:
                stats["err"] += 1


def main():
    print(f"Load testing {API} for {DURATION}s with concurrency {CONCURRENCY} ...")
    stats = {"total": 0, "ok": 0, "err": 0, "latencies": []}
    lock = threading.Lock()
    stop_at = time.time() + DURATION
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(worker, stop_at, stats, lock) for _ in range(CONCURRENCY)]
        for f in as_completed(futs):
            f.result()
    elapsed = time.perf_counter() - t0
    lats = stats["latencies"]
    lats_sorted = sorted(lats)
    p50 = lats_sorted[int(len(lats_sorted)*0.5)] if lats_sorted else 0
    p95 = lats_sorted[int(len(lats_sorted)*0.95)] if lats_sorted else 0
    p99 = lats_sorted[int(len(lats_sorted)*0.99)] if lats_sorted else 0
    print("=" * 60)
    print(f"  Duration:         {elapsed:.1f}s")
    print(f"  Total requests:   {stats['total']}")
    print(f"  Success / Error:  {stats['ok']} / {stats['err']}")
    print(f"  Throughput:       {stats['total']/elapsed:.1f} req/s")
    print(f"  Latency mean:     {statistics.mean(lats)*1000:.1f} ms" if lats else "  no latencies")
    print(f"  Latency p50/p95/p99: {p50*1000:.1f} / {p95*1000:.1f} / {p99*1000:.1f} ms")
    print("=" * 60)


if __name__ == "__main__":
    main()
