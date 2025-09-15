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
  5. solar consistency check: AC vs sum(DC channels), counter resets, flat
     days, and "exported more than produced" anomalies,
  6. optional monthly coverage probe per metric,
  7. optional HTML report rendering.

Usage:
    01-data-quality.py [-u VM_URL] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                       [--config energy-config.yaml] [--output data-quality.json]
                       [--checks all|identity|solar]
                       [--threshold 0.05] [--workers 16] [--html report.html]
                       [--no-coverage]
"""

import argparse
import datetime
import html
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
        "solar_dc0": {
            "selector": 'opendtu_YieldTotal{type="DC",channel="0"}',
            "scale_to_kwh": 1.0,
        },
        "solar_dc1": {
            "selector": 'opendtu_YieldTotal{type="DC",channel="1"}',
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
    "known_anomalies": {
        "solar_flat_day": ["2026-05-24"],
    },
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


def sample_deltas(url: str, selector: str, times: list[int],
                  workers: int = 16) -> list[float | None]:
    return daily_deltas(sample_daily_series(url, selector, times, workers))


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
        grid_delta = sample_deltas(url, grid["selector"],
                                   boundary_times(start, n_days), workers)

    tempo = {k: metrics[k] for k in TEMPO_KEYS}
    if tempo_delta is None:
        times = boundary_times(start, n_days)
        tempo_delta = {key: sample_deltas(url, m["selector"], times, workers)
                       for key, m in tempo.items()}

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


def solar_check(url: str, cfg: dict, start: datetime.datetime, n_days: int,
                workers: int = 16, margin_kwh: float = 0.05) -> dict:
    metrics = cfg["metrics"]
    times = boundary_times(start, n_days)
    ac = sample_deltas(url, metrics["solar_total"]["selector"], times, workers)
    dc0 = sample_deltas(url, metrics["solar_dc0"]["selector"], times, workers)
    dc1 = sample_deltas(url, metrics["solar_dc1"]["selector"], times, workers)
    grid_out = sample_deltas(url, metrics["grid_out"]["selector"], times,
                             workers)

    known_days = set(cfg.get("known_anomalies", {}).get("solar_flat_day", []))

    missing = []
    negative = []
    flat = []
    imports = []
    unknown_imports = []
    dla = []
    monthly: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    tot_ac = tot_dc0 = tot_dc1 = tot_out = 0.0

    for i in range(n_days):
        day = (start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        a = ac[i]
        o = grid_out[i]
        if a is None:
            missing.append(day)
        else:
            if a < 0:
                negative.append({"date": day, "delta_kwh": round(a, 3)})
            elif a == 0:
                flat.append(day)
            tot_ac += a
            if o is None:
                dla.append(day)
            elif o > 0 and a < o - margin_kwh:
                imports.append({"date": day, "solar_kwh": round(a, 2),
                                "export_kwh": round(o, 2)})
        if o is not None:
            tot_out += o
        mo = day[:7]
        monthly[mo][0] += a if a is not None else 0.0
        monthly[mo][1] += o if o is not None else 0.0
        if dc0[i] is not None:
            tot_dc0 += dc0[i]
        if dc1[i] is not None:
            tot_dc1 += dc1[i]

    sign = lambda x: (x >= margin_kwh or x <= -margin_kwh)
    ac_dc_diff = tot_ac - (tot_dc0 + tot_dc1)
    unknown_imports = [d for d in imports if d["date"] not in known_days]
    unknown_flat = [d for d in flat if d not in known_days]
    ok = (not missing and not negative and not unknown_imports
          and not unknown_flat and not sign(ac_dc_diff))

    return {
        "ok": ok,
        "solar_total_kwh": round(tot_ac, 2),
        "solar_dc0_kwh": round(tot_dc0, 2),
        "solar_dc1_kwh": round(tot_dc1, 2),
        "ac_vs_dc_sum_diff_kwh": round(ac_dc_diff, 2),
        "grid_out_total_kwh": round(tot_out, 2),
        "missing_days": missing,
        "negative_days": negative,
        "flat_days": flat,
        "export_exceeds_production_days": imports,
        "unknown_anomaly_days": unknown_imports + unknown_flat,
        "monthly": {k: [round(v[0], 2), round(v[1], 2)] for k, v in
                    sorted(monthly.items())},
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


def format_day_list(days: list[str], limit: int = 12) -> str:
    if not days:
        return "-"
    shown = days[:limit]
    suffix = f" (+{len(days) - limit} more)" if len(days) > limit else ""
    return ", ".join(shown) + suffix


def print_report(report: dict, threshold_pct: float) -> None:
    if "identity" in report:
        id_ = report["identity"]
        print("\n=== identity: Grid In vs sum(Tempo) per day ===")
        print(f"Grid In year total    : {id_['grid_in_total_kwh']:9.2f} kWh")
        print(f"Sum Tempo (any subset): {id_['tempo_sum_total_kwh']:9.2f} kWh")
        print(f"Diff                  : {id_['diff_kwh']:+9.2f} kWh "
              f"({id_['diff_pct']:+.2f}%)")
        print("\nmonth:    grid /  tempo    (kWh)")
        for mo, (g, s) in id_["monthly"].items():
            print(f"  {mo}  {g:7.2f}  {s:7.2f}   diff {g - s:+6.2f}")
        print(f"\nincomplete days (some tempo counter silent): "
              f"{len(id_['incomplete_days'])} ({format_day_list([d['date'] for d in id_['incomplete_days']], 5)})")
        print(f"days diverging >{threshold_pct}% with complete data: "
              f"{len(id_['diverging_days'])}")
        for d in id_["diverging_days"][:10]:
            print("  ", d["date"], d["grid_kwh"], d["tempo_kwh"])

        miss = report["missing"]
        print("\n=== silent days per tempo counter ===")
        for k, v in miss["silent_days_per_counter"].items():
            print(f"  {k:12} {v}")
        print("deficit kWh by which counters silent together:")
        for k, v in miss["deficit_by_missing"].items():
            print(f"  silent={k:42} {v:+8.2f}")

    if "solar" in report:
        s = report["solar"]
        print("\n=== solar consistency ===")
        print(f"AC total      : {s['solar_total_kwh']:9.2f} kWh")
        print(f"DC0 total     : {s['solar_dc0_kwh']:9.2f} kWh")
        print(f"DC1 total     : {s['solar_dc1_kwh']:9.2f} kWh")
        print(f"AC-(DC0+DC1)  : {s['ac_vs_dc_sum_diff_kwh']:+9.2f} kWh")
        print(f"Grid Out total: {s['grid_out_total_kwh']:9.2f} kWh "
              f"(~{s['grid_out_total_kwh'] / s['solar_total_kwh'] * 100:.0f}% exported)" if s['solar_total_kwh'] else "")
        print(f"missing days: {len(s['missing_days'])} ({format_day_list(s['missing_days'])})")
        print(f"negative deltas (resets): {len(s['negative_days'])} "
              f"({format_day_list([d['date'] for d in s['negative_days']])})")
        print(f"flat days: {len(s['flat_days'])} ({format_day_list(s['flat_days'])})")
        print(f"export > production days: {len(s['export_exceeds_production_days'])}")
        for d in s["export_exceeds_production_days"]:
            print("   ", d["date"], "solar", d["solar_kwh"], "export", d["export_kwh"])
        print(f"unexplained anomalies: {len(s['unknown_anomaly_days'])} "
              f"({format_day_list(s['unknown_anomaly_days'])})")
        print("\nmonth:   solar /    out / auto-consumed")
        for mo, (a, o) in s["monthly"].items():
            print(f"  {mo}  {a:7.2f}  {o:7.2f}  {a - o:8.2f}")

    if "coverage" in report:
        print("\n=== coverage probe ===")
        for k, v in report["coverage"].items():
            print(f"  {k:14} {v['covered_samples']:>3}/{v['total_samples']} "
                  f"({v['coverage_pct']:5.1f}%)  {v['first']} -> {v['last']}")


def render_html(report: dict, path: str, threshold_pct: float) -> None:
    e = html.escape
    rows = []
    title = "Energy data-quality report"

    def table(headers, data):
        h = "".join(f"<th>{e(str(x))}</th>" for x in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>"
            for r in data)
        return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"

    if "identity" in report:
        id_ = report["identity"]
        rows.append(f"<h2>Identity: Grid In vs Tempo</h2>")
        rows.append(table(["Metric", "kWh"], [
            ["Grid In total", id_["grid_in_total_kwh"]],
            ["Tempo sum (any subset)", id_["tempo_sum_total_kwh"]],
            ["Diff", f"{id_['diff_kwh']:+} ({id_['diff_pct']:+}%)"],
        ]))
        rows.append("<h3>Monthly grid / tempo (kWh)</h3>")
        rows.append(table(["Month", "Grid", "Tempo", "Diff"],
                          [[mo, g, s, round(g - s, 2)]
                           for mo, (g, s) in id_["monthly"].items()]))
        rows.append(f"<p>Incomplete days: {len(id_['incomplete_days'])} &nbsp; "
                    f"Diverging ({threshold_pct}%) days: "
                    f"{len(id_['diverging_days'])}</p>")
        miss = report["missing"]
        rows.append("<h3>Silent days per tempo counter</h3>")
        rows.append(table(["Counter", "Silent days"], miss["silent_days_per_counter"].items()))

    if "solar" in report:
        s = report["solar"]
        rows.append("<h2>Solar consistency</h2>")
        rows.append(table(["Metric", "kWh"], [
            ["Solar AC", s["solar_total_kwh"]],
            ["Solar DC0", s["solar_dc0_kwh"]],
            ["Solar DC1", s["solar_dc1_kwh"]],
            ["AC - (DC0+DC1)", s["ac_vs_dc_sum_diff_kwh"]],
            ["Grid Out", s["grid_out_total_kwh"]],
        ]))
        rows.append("<p>Missing days: {} &nbsp; Resets: {} &nbsp; Flat days: "
                    "{} &nbsp; Export&gt;production: {} &nbsp; Unknown "
                    "anomalies: {}</p>".format(
                        len(s["missing_days"]), len(s["negative_days"]),
                        len(s["flat_days"]),
                        len(s["export_exceeds_production_days"]),
                        len(s["unknown_anomaly_days"])))
        rows.append("<h3>Monthly solar / export / auto-consumed (kWh)</h3>")
        rows.append(table(["Month", "Solar", "Grid Out", "Auto-consumed"],
                          [[mo, a, o, round(a - o, 2)]
                           for mo, (a, o) in s["monthly"].items()]))

    if "coverage" in report:
        rows.append("<h2>Coverage probe</h2>")
        rows.append(table(["Metric", "Coverage", "First", "Last"], [
            [k, f"{v['covered_samples']}/{v['total_samples']} "
                f"({v['coverage_pct']}%)", v["first"], v["last"]]
            for k, v in report["coverage"].items()]))

    w = report["window"]
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{e(title)}</title>
<style>
 body {{ font-family: sans-serif; margin: 2rem; }}
 h1 {{ border-bottom: 2px solid #333; padding-bottom: .3rem; }}
 h2 {{ margin-top: 2rem; }}
 table {{ border-collapse: collapse; margin: 1rem 0; }}
 th, td {{ border: 1px solid #aaa; padding: .3rem .7rem; text-align: left; }}
 th {{ background: #eee; }}
 p {{ color: #444; }}
</style></head><body>
<h1>{e(title)}</h1>
<p>VM: {e(report['url'])}<br>
Window: {e(w['start'])} -> {e(w['end'])}<br>
Generated: {e(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}</p>
{''.join(rows)}
</body></html>
"""
    with open(path, "w") as f:
        f.write(page)
    print(f"HTML report saved: {path}")


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
    ap.add_argument("--checks", choices=["all", "identity", "solar"],
                    default="all", help="which checks to run (default all)")
    ap.add_argument("--html", default=None,
                    help="also render an HTML report to this path")
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

    report = {"window": {"start": start.isoformat(), "end": end.isoformat()},
              "url": url}
    checks_ok: list[bool] = []

    run_identity = args.checks in ("all", "identity")
    run_solar = args.checks in ("all", "solar")

    if run_identity:
        times = boundary_times(start, n_days)
        grid_delta = sample_deltas(
            url, cfg["metrics"]["grid_in"]["selector"], times, args.workers)
        tempo_delta = {}
        for key in TEMPO_KEYS:
            tempo_delta[key] = sample_deltas(
                url, cfg["metrics"][key]["selector"], times, args.workers)

        identity = identity_check(url, cfg, start, n_days,
                                  workers=args.workers,
                                  grid_delta=grid_delta,
                                  tempo_delta=tempo_delta)
        tempo_scale = {k: cfg["metrics"][k]["scale_to_kwh"]
                       for k in TEMPO_KEYS}
        missing = missing_classification(grid_delta, tempo_scale,
                                         tempo_delta, start)
        report["identity"] = identity
        report["missing"] = missing
        identity_pass = (identity["diff_pct"] is None
                         or abs(identity["diff_pct"]) <= args.threshold * 100)
        checks_ok.append(identity_pass)
        print(f"\nIdentity threshold: |{identity['diff_pct']}%| <= "
              f"{args.threshold * 100}% -> "
              f"{'PASS' if identity_pass else 'FAIL'}")

    if run_solar:
        solar = solar_check(url, cfg, start, n_days, workers=args.workers)
        report["solar"] = solar
        checks_ok.append(solar["ok"])
        print(f"\nSolar consistency: {'PASS' if solar['ok'] else 'FAIL'}")

    if not args.no_coverage:
        print("Monthly coverage probe...")
        report["coverage"] = coverage_probe(url, cfg, start, n_days)

    print_report(report, args.threshold * 100)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved: {args.output}")

    if args.html:
        os.makedirs(os.path.dirname(args.html) or ".", exist_ok=True)
        render_html(report, args.html, args.threshold * 100)

    return 0 if all(checks_ok) else 1


if __name__ == "__main__":
    sys.exit(main())