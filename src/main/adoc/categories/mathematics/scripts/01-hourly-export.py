#!/usr/bin/env python3
"""
Phase 2 - hourly export: the canonical 365-day hourly record.

Builds `hourly-energy.csv` (8 760 rows + header), the single source of truth
used by every downstream phase (3..7) and any later mathematics. It contains
the hourly *increase* of each cumulative counter over the analysis window,
scaled to kWh, plus the derived solar columns.

For each hour of the window (default 2025-09-01 -> 2026-09-01):
  * solar_total_kwh              opendtu AC YieldTotal hourly increment
  * grid_in_kwh / grid_out_kwh   zigbee meter hourly increments
  * tempo_{blue,white,red}_hp_hc teleinfo HP+HC increments per Tempo color
                                 (Wh -> kWh, both halves summed)
  * tempo_{blue,white,red}_{hp,hc} the six individual teleinfo registers
                                 (HP and HC separately, Wh -> kWh); these
                                 encode the exact HP/HC split used by the
                                 contract pricing (Phase 4)
  * auto_consumed_kwh            solar_total - grid_out
  * home_consumption_kwh         grid_in + auto_consumed

Method: one query_range(step=1h) per metric over the year (split per calendar
month so VictoriaMetrics' per-series sample cap is never hit), then hourly
deltas computed in Python with the scale_to_kwh factors from
energy-config.yaml. Raw hourly readings are cached to plain CSV by vmlib, so
a re-run is offline, deterministic and does not re-hit the database.

Missing hours are NOT interpolated: a slot whose two boundary readings are not
both present is written "nan" and counted in the per-column report, so gaps
stay visible instead of being silently zeroed. The derived columns inherit the
NaN of any input still missing.

Optional --solar-corrected PREFIX merges the Phase 0/2 solar catch-up
correction (PREFIX-hourly.csv from `make correct-solar`): the corrected
solar_total series replaces the raw one and auto-consumed / home consumption
are re-derived. Plain missing hours of the correction (empty corrected_kwh)
remain NaN, exactly as reported there.

Usage:
    01-hourly-export.py [-u VM_URL] [--config energy-config.yaml]
                        [--output scripts/output/hourly-energy.csv]
                        [--cache-dir scripts/cache] [--solar-corrected PREFIX]
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmlib

# Metrics whose hourly increments land explicit columns (DC channels are
# covered by the data-quality checks, not needed downstream).
EXPORT_METRICS = [
    "solar_total",
    "grid_in",
    "grid_out",
    "tempo_blue_hc", "tempo_blue_hp",
    "tempo_white_hc", "tempo_white_hp",
    "tempo_red_hc", "tempo_red_hp",
]

TEMPO_COLOR_KEYS = ["blue", "white", "red"]
TEMPO_COLUMNS = [f"tempo_{c}_hp_hc" for c in TEMPO_COLOR_KEYS]

OUTPUT_COLUMNS = [
    "utc_hour",
    "solar_total_kwh", "grid_in_kwh", "grid_out_kwh",
    "tempo_blue_hp_hc", "tempo_white_hp_hc", "tempo_red_hp_hc",
    "tempo_blue_hp", "tempo_blue_hc",
    "tempo_white_hp", "tempo_white_hc",
    "tempo_red_hp", "tempo_red_hc",
    "auto_consumed_kwh", "home_consumption_kwh",
]


def _add(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a + b


def load_solar_correction(prefix: str) -> dict[str, dict[str, float | None]]:
    """PREFIX-hourly.csv -> {metric: {utc_hour: corrected_kwh|None}}."""
    out: dict[str, dict[str, float | None]] = {}
    with open(f"{prefix}-hourly.csv", newline="") as f:
        for row in csv.DictReader(f):
            metric = row["metric"]
            corr = row["corrected_kwh"]
            out.setdefault(metric, {})[row["utc_hour"]] = (
                float(corr) if corr else None)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the canonical hourly-energy.csv record")
    ap.add_argument("-u", "--url", default=None,
                    help="VictoriaMetrics base URL (default from config)")
    ap.add_argument("--config", default="scripts/energy-config.yaml")
    ap.add_argument("--output", default="scripts/output/hourly-energy.csv")
    ap.add_argument("--cache-dir", default="scripts/cache")
    ap.add_argument("--solar-corrected", default=None, metavar="PREFIX",
                    help="merge the solar correction PREFIX-hourly.csv into "
                         "the record (raw VM data never used then)")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    cfg = vmlib.load_config(args.config)
    url = args.url or cfg.get("vm", {}).get("url", "http://localhost:8428")
    start, end, n_days = vmlib.window_days(cfg)
    n_hours = n_days * 24
    labels = vmlib.utc_hours_labels(start, n_days)
    cache = vmlib.CsvCache(args.cache_dir)

    print(f"VM: {url}")
    print(f"Window: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} "
          f"({n_days} days, {n_hours} hours)")

    print("Fetching hourly series (cached per metric)...")

    def increments(key: str) -> list[float | None]:
        m = cfg["metrics"][key]
        vals = cache.hourly(key, start, n_days, url=url,
                            selector=m["selector"])
        return [
            None if d is None else d * m["scale_to_kwh"]
            for d in vmlib.hourly_increments(vals, start, n_days)
        ]

    inc: dict[str, list[float | None]] = {}
    for key in EXPORT_METRICS:
        inc[key] = increments(key)

    solar = list(inc["solar_total"])
    if args.solar_corrected:
        corr = load_solar_correction(args.solar_corrected).get(
            "solar_total", {})
        merged = 0
        for i, label in enumerate(labels):
            v = corr.get(label)
            if v is not None:
                solar[i] = v
                merged += 1
        print(f"  corrected solar applied: {merged} hours replaced "
              f"(from {args.solar_corrected}-hourly.csv)")

    grid_in = inc["grid_in"]
    grid_out = inc["grid_out"]
    tempo = {c: [_add(inc[f"tempo_{c}_hp"][i], inc[f"tempo_{c}_hc"][i])
                 for i in range(n_hours)] for c in TEMPO_COLOR_KEYS}
    auto = [None if solar[i] is None or grid_out[i] is None
            else solar[i] - grid_out[i] for i in range(n_hours)]
    home = [_add(grid_in[i], auto[i]) for i in range(n_hours)]

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(OUTPUT_COLUMNS)
        for i in range(n_hours):
            values = {
                "solar_total_kwh": solar[i],
                "grid_in_kwh": grid_in[i],
                "grid_out_kwh": grid_out[i],
                "tempo_blue_hp_hc": tempo["blue"][i],
                "tempo_white_hp_hc": tempo["white"][i],
                "tempo_red_hp_hc": tempo["red"][i],
                "tempo_blue_hp": inc["tempo_blue_hp"][i],
                "tempo_blue_hc": inc["tempo_blue_hc"][i],
                "tempo_white_hp": inc["tempo_white_hp"][i],
                "tempo_white_hc": inc["tempo_white_hc"][i],
                "tempo_red_hp": inc["tempo_red_hp"][i],
                "tempo_red_hc": inc["tempo_red_hc"][i],
                "auto_consumed_kwh": auto[i],
                "home_consumption_kwh": home[i],
            }
            w.writerow([labels[i]] + [
                "nan" if values[c] is None else f"{values[c]:.4f}"
                for c in OUTPUT_COLUMNS[1:]
            ])

    # Report per column: hourly coverage + yearly total.
    print(f"\nWritten: {args.output} ({n_hours} rows)\n")
    print(f"{'column':24} {'total kWh':>12} {'missing hrs':>12}")
    summary = {c: (sum(v for v in col if v is not None),
                   sum(1 for v in col if v is None))
               for c, col in [
                   ("solar_total_kwh", solar),
                   ("grid_in_kwh", grid_in),
                   ("grid_out_kwh", grid_out),
                   ("tempo_blue_hp_hc", tempo["blue"]),
                   ("tempo_white_hp_hc", tempo["white"]),
                   ("tempo_red_hp_hc", tempo["red"]),
                   ("tempo_blue_hp", inc["tempo_blue_hp"]),
                   ("tempo_blue_hc", inc["tempo_blue_hc"]),
                   ("tempo_white_hp", inc["tempo_white_hp"]),
                   ("tempo_white_hc", inc["tempo_white_hc"]),
                   ("tempo_red_hp", inc["tempo_red_hp"]),
                   ("tempo_red_hc", inc["tempo_red_hc"]),
                   ("auto_consumed_kwh", auto),
                   ("home_consumption_kwh", home),
               ]}
    for col in OUTPUT_COLUMNS[1:]:
        total, missing = summary[col]
        print(f"{col:24} {total:12.2f} {missing:12d}")
    if any(missing for _, missing in summary.values()):
        print("\nRows with missing hours are written 'nan' (not interpolated);")
        print("see the Phase 0 hourly data-quality report for the gap frames.")

    return 0


if __name__ == "__main__":
    sys.exit(main())