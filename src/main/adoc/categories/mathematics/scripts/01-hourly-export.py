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
  * electric_car_kwh             car counter hourly increment (phase 1 of the
  * water_heater_kwh             water heater counter hourly increment (phase 2)
  * heaters_kwh                  heaters counter hourly increment (phase 3)
                                 the three high consumers of the garage power
                                 meter C01; their consumption is a subset of
                                 the grid draw, not an addition to it
  * auto_consumed_kwh            solar_total - grid_out
  * home_consumption_kwh         grid_in + auto_consumed
  * {metric}_counter_kwh         the twelve cumulative counters above, read at
                                  the end of the hour (closing boundary of the
                                  slot, same kWh scaling as the increments), so
                                  counter[i] - counter[i-1] == increment[i]

Method: one query_range(step=1h) per metric over the year (split per calendar
month so VictoriaMetrics' per-series sample cap is never hit), then hourly
deltas computed in Python with the scale_to_kwh factors from
energy-config.yaml. Raw hourly readings are cached to plain CSV by vmlib, so
a re-run is offline, deterministic and does not re-hit the database.

Missing hours are NOT interpolated: a slot whose two boundary readings are not
both present is written "nan" and counted in the per-column report, so gaps
stay visible instead of being silently zeroed. The derived columns inherit the
NaN of any input still missing.

Usage:
    01-hourly-export.py [-u VM_URL] [--config energy-config.yaml]
                        [--output scripts/output/hourly-energy.csv]
                        [--cache-dir scripts/cache]
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
    "electric_car", "water_heater", "heaters",
]

TEMPO_COLOR_KEYS = ["blue", "white", "red"]
TEMPO_COLUMNS = [f"tempo_{c}_hp_hc" for c in TEMPO_COLOR_KEYS]

# High consumers behind the same garage power meter as grid in/out (C01): one
# output column each, named after the metric key.
CONSUMER_KEYS = ["electric_car", "water_heater", "heaters"]
CONSUMER_COLUMNS = [f"{k}_kwh" for k in CONSUMER_KEYS]

INCREMENT_COLUMNS = [
    "solar_total_kwh", "grid_in_kwh", "grid_out_kwh",
    "tempo_blue_hp_hc", "tempo_white_hp_hc", "tempo_red_hp_hc",
    "tempo_blue_hp", "tempo_blue_hc",
    "tempo_white_hp", "tempo_white_hc",
    "tempo_red_hp", "tempo_red_hc",
    *CONSUMER_COLUMNS,
    "auto_consumed_kwh", "home_consumption_kwh",
]

# Cumulative reading of each physical counter, at the boundary closing the hour
# of the row (so the first difference of a counter column is its increment
# column). Same scaling as the increments: teleinfo registers stay in kWh.
COUNTER_COLUMNS = [f"{k}_counter_kwh" for k in EXPORT_METRICS]

OUTPUT_COLUMNS = ["utc_hour", *INCREMENT_COLUMNS, *COUNTER_COLUMNS]


def _add(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a + b


def _cell(value: float | None) -> str:
    return "nan" if value is None else f"{value:.4f}"


def _total(value: float | None) -> str:
    return "nan" if value is None else f"{value:.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the canonical hourly-energy.csv record")
    ap.add_argument("-u", "--url", default=None,
                    help="VictoriaMetrics base URL (default from config)")
    ap.add_argument("--config", default="scripts/energy-config.yaml")
    ap.add_argument("--output", default="scripts/output/hourly-energy.csv")
    ap.add_argument("--cache-dir", default="scripts/cache")
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

    hour_ends = vmlib.hour_boundaries(start, n_days)[1:]

    def series(key: str) -> tuple[list[float | None], list[float | None]]:
        """(hourly increments, end-of-hour counter readings) of one metric."""
        m = cfg["metrics"][key]
        vals = cache.hourly(key, start, n_days, url=url,
                            selector=m["selector"])
        scale = m["scale_to_kwh"]
        increments = [
            None if d is None else d * scale
            for d in vmlib.hourly_increments(vals, start, n_days)
        ]
        counters = [None if ts not in vals else vals[ts] * scale
                    for ts in hour_ends]
        return increments, counters

    inc: dict[str, list[float | None]] = {}
    ctr: dict[str, list[float | None]] = {}
    for key in EXPORT_METRICS:
        inc[key], ctr[key] = series(key)

    solar = inc["solar_total"]
    grid_in = inc["grid_in"]
    grid_out = inc["grid_out"]
    tempo = {c: [_add(inc[f"tempo_{c}_hp"][i], inc[f"tempo_{c}_hc"][i])
                 for i in range(n_hours)] for c in TEMPO_COLOR_KEYS}
    auto = [None if solar[i] is None or grid_out[i] is None
            else solar[i] - grid_out[i] for i in range(n_hours)]
    home = [_add(grid_in[i], auto[i]) for i in range(n_hours)]

    # Every output column, in output order, as a full hourly series.
    columns: dict[str, list[float | None]] = {
        "solar_total_kwh": solar,
        "grid_in_kwh": grid_in,
        "grid_out_kwh": grid_out,
        **{f"tempo_{c}_hp_hc": tempo[c] for c in TEMPO_COLOR_KEYS},
        "tempo_blue_hp": inc["tempo_blue_hp"],
        "tempo_blue_hc": inc["tempo_blue_hc"],
        "tempo_white_hp": inc["tempo_white_hp"],
        "tempo_white_hc": inc["tempo_white_hc"],
        "tempo_red_hp": inc["tempo_red_hp"],
        "tempo_red_hc": inc["tempo_red_hc"],
        **{col: inc[key] for key, col in zip(CONSUMER_KEYS, CONSUMER_COLUMNS)},
        "auto_consumed_kwh": auto,
        "home_consumption_kwh": home,
        **{col: ctr[key] for key, col in zip(EXPORT_METRICS, COUNTER_COLUMNS)},
    }

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(OUTPUT_COLUMNS)
        for i in range(n_hours):
            w.writerow([labels[i]] + [_cell(columns[c][i])
                                       for c in OUTPUT_COLUMNS[1:]])

    # Report per column: hourly coverage + yearly total.
    print(f"\nWritten: {args.output} ({n_hours} rows)\n")
    print(f"{'column':24} {'total kWh':>12} {'missing hrs':>12}")
    summary = {c: (sum(v for v in col if v is not None),
                   sum(1 for v in col if v is None))
               for c, col in columns.items()}
    for col in INCREMENT_COLUMNS:
        total, missing = summary[col]
        print(f"{col:24} {total:12.2f} {missing:12d}")
    if any(missing for _, missing in summary.values()):
        print("\nRows with missing hours are written 'nan' (not interpolated);")
        print("see the Phase 0 hourly data-quality report for the gap frames.")

    # Counter columns: no total (a sum of readings is meaningless), so report
    # the reading at both ends of the window, its change and the decreases.
    print(f"\n{'column':24} {'first kWh':>12} {'last kWh':>12} "
          f"{'change kWh':>12} {'resets':>8} {'missing hrs':>12}")
    for col in COUNTER_COLUMNS:
        vals = columns[col]
        present = [v for v in vals if v is not None]
        first, last = (present[0], present[-1]) if present else (None, None)
        resets = sum(1 for before, after in zip(vals, vals[1:])
                     if before is not None and after is not None
                     and after < before)
        change = None if first is None else last - first
        print(f"{col:24} {_total(first):>12} {_total(last):>12} "
              f"{_total(change):>12} {resets:8d} "
              f"{len(vals) - len(present):12d}")

    return 0


if __name__ == "__main__":
    sys.exit(main())