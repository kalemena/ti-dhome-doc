#!/usr/bin/env python3
"""
VictoriaMetrics data-quality report for the mathematics/energy analysis
(Phase 0 discovery).

Reproduces, as reusable functions, the probes executed against the live VM:
  1. per-day max_over_time sampling of cumulative counters over a 1-year window,
  2. daily deltas (increments),
  3. Tempo identity check: Grid In vs sum of *available* Tempo counters per day
     (inactive White/Red days are expected, not faults),
  4. missing-day classification per counter + deficit by missing combination,
  5. optional monthly coverage probe per metric.

Usage:
    01-data-quality.py [-u VM_URL] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                       [--config energy-config.yaml] [--output data-quality.json]
                       [--threshold 0.05] [--workers 16] [--no-coverage]
"""

import argparse
import datetime
import json
import os
import sys
import urllib.request
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

DEFAULT_CONFIG = {
    "vm": {"url": "http://localhost:8428"},
    "metrics": {
        "solar_total": {
            "selector": 'opendtu_YieldTotal{type="AC"}',
            "scale_to_kwh": 1.0,
        },
        "grid_in": {
            "selector": 'sensors_zigbee_energy_b{location="C03~Garage~PowerMeter"}',
            "scale_to_kwh": 1.0,
        },
        "grid_out": {
            "selector": 'sensors_zigbee_energy_produced_b{location="C03~Garage~PowerMeter"}',
            "scale_to_kwh": 1.0,
        },
        "tempo_blue_hc": {
            "selector": 'sensors_teleinfo_BBRHCJB{location="main"}',
            "scale_to_kwh": 0.001,
        },
        "tempo_blue_hp": {
            "selector": 'sensors_teleinfo_BBRHPJB{location="main"}',
            "scale_to_kwh": 0.001,
        },
        "tempo_white_hc": {
            "selector": 'sensors_teleinfo_BBRHCJW{location="main"}',
            "scale_to_kwh": 0.001,
        },
        "tempo_white_hp": {
            "selector": 'sensors_teleinfo_BBRHPJW{location="main"}',
            "scale_to_kwh": 0.001,
        },
        "tempo_red_hc": {
            "selector": 'sensors_teleinfo_BBRHCJR{location="main"}',
            "scale_to_kwh": 0.001,
        },
        "tempo_red_hp": {
            "selector": 'sensors_teleinfo_BBRHPJR{location="main"}',
            "scale_to_kwh": 0.001,
        },
    },
    "window": {"start": "2025-09-01T00:00:00Z", "end": "2026-09-01T00:00:00Z"},
}

TEMPO_KEYS = [
    "tempo_blue_hc", "tempo_blue_hp",
    "tempo_white_hc", "tempo_white_hp",
    "tempo_red_hc", "tempo_red_hp",
]


def load_config(path: str | None) -> dict:
    if path and HAVE_YAML and os.path.exists(path):
        with open(path) as f:
            cfg = yaml.safe_load(f)
        print(f"Loaded config: {path}")
        return cfg
    if path and not HAVE_YAML:
        print("WARNING: PyYAML not available, using embedded default config "
              f"(file {path} ignored). Run `make setup` to install deps.")
    else:
        print("No config file provided, using embedded default config.")
    return dict(DEFAULT_CONFIG)


def parse_ts(value) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(
            tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def window_days(cfg: dict) -> tuple[datetime.datetime, datetime.datetime, int]:
    start = parse_ts(cfg["window"]["start"])
    end = parse_ts(cfg["window"]["end"])
    return start, end, (end - start).days


def boundary_times(start: datetime.datetime, n_days: int, offset_s: int = 1) -> list[int]:
    return [
        int((start + datetime.timedelta(days=i)).timestamp()) + offset_s
        for i in range(n_days + 1)
    ]


def query_max_over_time(url: str, selector: str, time_ts: int,
                        lookback: str = "2d") -> float | None:
    query = urllib.parse.urlencode({
        "query": f"max_over_time({selector}[{lookback}])",
        "time": time_ts,
    })
    with urllib.request.urlopen(f"{url}/api/v1/query?{query}", timeout=60) as r:
        result = json.load(r)["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def sample_daily_series(url: str, selector: str, times: list[int],
                        workers: int = 16) -> list[float | None]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda t: query_max_over_time(url, selector, t), times))


def daily_deltas(values: list[float | None]) -> list[float | None]:
    return [
        None if values[i - 1] is None or values[i] is None
        else values[i] - values[i - 1]
        for i in range(1, len(values))
    ]


def identity_check(url: str, cfg: dict, start: datetime.datetime,
                   n_days: int, workers: int = 16,
                   grid_delta: list[float | None] | None = None,
                   tempo_delta: dict[str, list[float | None]] | None = None
                   ) -> dict:
    metrics = cfg["metrics"]
    grid = metrics["grid_in"]
    if grid_delta is None:
        times = boundary_times(start, n_days)
        series = sample_daily_series(url, grid["selector"], times, workers)
        grid_delta = daily_deltas(series)

    tempo = {k: metrics[k] for k in TEMPO_KEYS}
    if tempo_delta is None:
        times = boundary_times(start, n_days)
        tempo_delta = {}
        for key, m in tempo.items():
            series = sample_daily_series(url, m["selector"], times, workers)
            tempo_delta[key] = daily_deltas(series)

    tot_grid = 0.0
    tot_tempo = 0.0
    incomplete_days = []
    diverging_days = []
    monthly: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])

    for i in range(n_days):
        g = grid_delta[i]
        if g is None:
            continue
        s = 0.0
        missing = []
        for key, dv in tempo_delta.items():
            if dv[i] is None:
                missing.append(key)
                continue
            s += dv[i] * tempo[key]["scale_to_kwh"]
        day = (start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        tot_grid += g
        tot_tempo += s
        if missing:
            incomplete_days.append({"date": day, "grid_kwh": round(g, 2),
                                    "missing": missing})
        elif abs(g - s) > 0.05 * max(g, 0.001):
            diverging_days.append({"date": day, "grid_kwh": round(g, 2),
                                   "tempo_kwh": round(s, 2)})
        mo = day[:7]
        monthly[mo][0] += g
        monthly[mo][1] += s

    diff = tot_grid - tot_tempo
    return {
        "grid_in_total_kwh": round(tot_grid, 2),
        "tempo_sum_total_kwh": round(tot_tempo, 2),
        "diff_kwh": round(diff, 2),
        "diff_pct": round(diff / tot_grid * 100, 2) if tot_grid else None,
        "incomplete_days": incomplete_days,
        "diverging_days": diverging_days,
        "monthly": {k: [round(v[0], 2), round(v[1], 2)] for k, v in
                    sorted(monthly.items())},
    }


def missing_classification(grid_delta: list[float | None],
                           tempo_scale: dict[str, float],
                           tempo_delta: dict[str, list[float | None]],
                           start: datetime.datetime) -> dict:
    per_counter = Counter()
    deficit = defaultdict(float)
    for i in range(len(grid_delta)):
        g = grid_delta[i]
        if g is None:
            continue
        missing = [k for k in tempo_delta if tempo_delta[k][i] is None]
        present = sum(tempo_delta[k][i] * tempo_scale[k] for k in tempo_delta
                      if tempo_delta[k][i] is not None)
        if missing:
            for k in missing:
                per_counter[k] += 1
            combo = "_".join(sorted(missing))
            deficit[combo] += g - present
    return {
        "silent_days_per_counter": dict(per_counter),
        "deficit_by_missing": {k: round(v, 2) for k, v in
                               sorted(deficit.items(), key=lambda x: -x[1])},
    }


def coverage_probe(url: str, cfg: dict, start: datetime.datetime,
                   n_days: int, samples: int = 13) -> dict:
    metrics = cfg["metrics"]
    step = n_days // samples
    sample_ts = []
    for i in range(samples):
        d = start + datetime.timedelta(days=i * step + step // 2)
        sample_ts.append(int(d.timestamp()))
    out = {}
    for key, m in metrics.items():
        vals = [query_max_over_time(url, m["selector"], ts)
                for ts in sample_ts]
        have = [v for v in vals if v is not None]
        out[key] = {
            "covered_samples": len(have),
            "total_samples": samples,
            "coverage_pct": round(len(have) / samples * 100, 1),
            "first": have[0] if have else None,
            "last": have[-1] if have else None,
        }
    return out


def print_report(report: dict) -> None:
    id = report["identity"]
    print("\n=== identity: Grid In vs sum(Tempo) per day ===")
    print(f"Grid In year total    : {id['grid_in_total_kwh']:9.2f} kWh")
    print(f"Sum Tempo (any subset): {id['tempo_sum_total_kwh']:9.2f} kWh")
    print(f"Diff                  : {id['diff_kwh']:+9.2f} kWh "
          f"({id['diff_pct']:+.2f}%)")
    print("\nmonth:    grid /  tempo    (kWh)")
    for mo, (g, s) in id["monthly"].items():
        print(f"  {mo}  {g:7.2f}  {s:7.2f}   diff {g - s:+6.2f}")
    print(f"\nincomplete days (some tempo counter silent): "
          f"{len(id['incomplete_days'])}")
    print(f"days diverging >5% with complete data: {len(id['diverging_days'])}")
    for d in id["diverging_days"][:10]:
        print("  ", d["date"], d["grid_kwh"], d["tempo_kwh"])

    miss = report["missing"]
    print("\n=== silent days per tempo counter ===")
    for k, v in miss["silent_days_per_counter"].items():
        print(f"  {k:12} {v}")
    print("deficit kWh by which counters silent together:")
    for k, v in miss["deficit_by_missing"].items():
        print(f"  silent={k:42} {v:+8.2f}")

    if "coverage" in report:
        print("\n=== coverage probe ===")
        for k, v in report["coverage"].items():
            print(f"  {k:14} {v['covered_samples']:>3}/{v['total_samples']} "
                  f"({v['coverage_pct']:5.1f}%)  {v['first']} -> {v['last']}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Data-quality report for the energy analysis window")
    ap.add_argument("-u", "--url", default=None,
                    help="VictoriaMetrics base URL (default from config)")
    ap.add_argument("--start", default=None,
                    help="window start YYYY-MM-DD (default from config)")
    ap.add_argument("--end", default=None,
                    help="window end YYYY-MM-DD (default from config)")
    ap.add_argument("--config", default="scripts/energy-config.yaml",
                    help="path to energy-config.yaml")
    ap.add_argument("--output", default="scripts/output/data-quality.json",
                    help="JSON report output path")
    ap.add_argument("--threshold", type=float, default=0.05,
                    help="identity divergence threshold (default 0.05)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--no-coverage", action="store_true",
                    help="skip the monthly coverage probe")
    args = ap.parse_args()

    cfg = load_config(args.config)
    url = args.url or cfg.get("vm", {}).get("url", "http://localhost:8428")
    start, end, n_days = window_days(cfg)
    if args.start:
        start = datetime.datetime.fromisoformat(f"{args.start}T00:00:00+00:00")
    if args.end:
        end = datetime.datetime.fromisoformat(f"{args.end}T00:00:00+00:00")
        n_days = (end - start).days

    print(f"VM: {url}")
    print(f"Window: {start:%Y-%m-%d} -> {end:%Y-%m-%d} ({n_days} days)")
    print("Sampling daily max_over_time per counter...")

    times = boundary_times(start, n_days)
    grid = cfg["metrics"]["grid_in"]["selector"]
    tempo = {k: cfg["metrics"][k] for k in TEMPO_KEYS}

    grid_delta = daily_deltas(sample_daily_series(
        url, grid, times, workers=args.workers))
    tempo_delta = {}
    for key, m in tempo.items():
        series = sample_daily_series(url, m["selector"], times,
                                     workers=args.workers)
        tempo_delta[key] = daily_deltas(series)

    identity = identity_check(url, cfg, start, n_days,
                              workers=args.workers,
                              grid_delta=grid_delta,
                              tempo_delta=tempo_delta)

    tempo_scale = {k: tempo[k]["scale_to_kwh"] for k in tempo}
    missing = missing_classification(grid_delta, tempo_scale, tempo_delta,
                                     start)

    report = {"window": {"start": start.isoformat(), "end": end.isoformat()},
              "url": url, "identity": identity, "missing": missing}
    if not args.no_coverage:
        print("Monthly coverage probe...")
        report["coverage"] = coverage_probe(url, cfg, start, n_days)

    print_report(report)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved: {args.output}")

    passed = (identity["diff_pct"] is None
              or abs(identity["diff_pct"]) <= args.threshold * 100)
    print(f"Identity check: {'PASS' if passed else 'FAIL'} "
          f"(|{identity['diff_pct']}%| <= {args.threshold * 100}%)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())