#!/usr/bin/env python3
"""
Phase 5 - hourly solar / grid profile (readme "Can we optimize Solar").

Reads the Phase 2 canonical record (`hourly-energy.csv`) offline and answers,
for the whole year, "what happens at hour H of the day":

  * solar_kwh             opendtu solar produced in that hour (sum of the
                          year's `solar_total_kwh` at that hour-of-day);
  * grid_in_kwh           zigbee Grid In in that hour;
  * grid_in_hp/hc_kwh     same Grid In split by the *Tempo* High/Low windows
                          (`hp_hours` of the tempo tariff in energy-config.yaml,
                          default UTC 06:00 -> 22:00): each present hour lands
                          entirely in HP or HC;
  * grid_out_kwh          zigbee Grid Out in that hour;
  * auto_consumed_kwh     auto-consumed solar (solar - grid_out) per hour;
  * home_consumption_kwh  grid_in + auto-consumed per hour;
  * surplus_kwh           balance Solar - Grid In per hour (negative hours are
                          the grid-draw hours of the day);

Each hour-of-day slot also reports how many of its 365 slots carried data
(`*_present_hours`, missing = 365 - present, never interpolated).

*Seasonal highlights:* the year is also split into the 4 meteorological
seasons configured in `energy-config.yaml` (`seasons`, UTC months), and the
same solar / grid-in / grid-out / auto-consumed / home-consumption metrics are
computed per season to compare where grid draw and solar production sit over
the year.

This supersedes functionally the legacy `mathematics-solar.py` (kept in the
category) and extends it from 3 metrics to the full solar/teleinfo/zigbee set.

Output:
  * `hourly-profile.csv`  the 24-hour-of-day profile (24 rows + header);
  * `hourly-profile.json` the structured result (24-hour profile + totals +
    + seasonal highlights), same basename as `--output`;
  * optional `--grafana`  wide table (one row per metric, one column per
    hour) ready for a Grafana Table panel.

Usage:
    04-hourly-profile.py [--config energy-config.yaml]
                         [--input scripts/output/hourly-energy.csv]
                         [--output scripts/output/hourly-profile.csv]
                         [--grafana scripts/output/hourly-profile-grafana.csv]
"""

import argparse
import csv
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmlib

HOURS_PER_DAY = 24
DEFAULT_SEASONS = {
    "autumn": [9, 10, 11],
    "winter": [12, 1, 2],
    "spring": [3, 4, 5],
    "summer": [6, 7, 8],
}

CSV_COLUMNS = [
    "hour",
    "solar_kwh", "grid_in_kwh", "grid_in_hp_kwh", "grid_in_hc_kwh",
    "grid_out_kwh", "auto_consumed_kwh", "home_consumption_kwh",
    "surplus_kwh", "solar_present_hours", "grid_present_hours",
]

GRAFANA_METRICS = [
    "solar_kwh", "grid_in_hp_kwh", "grid_in_hc_kwh", "grid_in_kwh",
    "grid_out_kwh", "auto_consumed_kwh", "home_consumption_kwh",
    "surplus_kwh",
]


def _norm_hp_hours(cfg: dict) -> list[list[int]]:
    """Tempo High-period UTC windows, from the tempo tariff `hp_hours`."""
    value = cfg.get("tariffs", {}).get("tempo", {}).get("hp_hours")
    if not value:
        return [[6, 22]]
    if isinstance(value[0], (int, float)):
        return [[int(value[0]), int(value[1])]]
    return [[int(w[0]), int(w[1])] for w in value]


def is_hp(hour: int, hp_hours: list[list[int]]) -> bool:
    return any(s <= hour < e for s, e in hp_hours)


def _sum_present(values: list[float]) -> tuple[float, int]:
    present = [v for v in values if not math.isnan(v)]
    return sum(present), len(present)


def hourly_profile(record: dict[str, list],
                   hp_hours: list[list[int]]) -> list[dict]:
    """Per hour-of-day (0..23) sums of the record's metrics."""
    n_hours = len(record["utc_hour"])
    rows = []
    for h in range(HOURS_PER_DAY):
        idx = [i for i in range(n_hours)
               if int(record["utc_hour"][i][11:13]) == h]
        solar, n_solar = _sum_present([record["solar_total_kwh"][i]
                                       for i in idx])
        grid_in, n_grid = _sum_present([record["grid_in_kwh"][i]
                                        for i in idx])
        grid_in_hp = sum(record["grid_in_kwh"][i] for i in idx
                         if not math.isnan(record["grid_in_kwh"][i])
                         and is_hp(h, hp_hours))
        grid_in_hc = sum(record["grid_in_kwh"][i] for i in idx
                         if not math.isnan(record["grid_in_kwh"][i])
                         and not is_hp(h, hp_hours))
        grid_out, _ = _sum_present([record["grid_out_kwh"][i] for i in idx])
        auto, _ = _sum_present([record["auto_consumed_kwh"][i] for i in idx])
        home, _ = _sum_present([record["home_consumption_kwh"][i] for i in idx])
        rows.append({
            "hour": h,
            "solar_kwh": round(solar, 4),
            "grid_in_kwh": round(grid_in, 4),
            "grid_in_hp_kwh": round(grid_in_hp, 4),
            "grid_in_hc_kwh": round(grid_in_hc, 4),
            "grid_out_kwh": round(grid_out, 4),
            "auto_consumed_kwh": round(auto, 4),
            "home_consumption_kwh": round(home, 4),
            "surplus_kwh": round(solar - grid_in, 4),
            "solar_present_hours": n_solar,
            "grid_present_hours": n_grid,
        })
    return rows


def seasonal_profile(record: dict[str, list],
                     seasons: dict[str, list[int]]) -> list[dict]:
    """Per-season sums of the same metrics (meteorological months)."""
    n_hours = len(record["utc_hour"])
    out = []
    for name, months in seasons.items():
        idx = [i for i in range(n_hours)
               if int(record["utc_hour"][i][5:7]) in months]
        days = sorted({record["utc_hour"][i][:10] for i in idx})
        solar, _ = _sum_present([record["solar_total_kwh"][i] for i in idx])
        grid_in, _ = _sum_present([record["grid_in_kwh"][i] for i in idx])
        grid_out, _ = _sum_present([record["grid_out_kwh"][i] for i in idx])
        auto, _ = _sum_present([record["auto_consumed_kwh"][i] for i in idx])
        home, _ = _sum_present([record["home_consumption_kwh"][i] for i in idx])
        out.append({
            "season": name,
            "months": months,
            "days": len(days),
            "solar_kwh": round(solar, 4),
            "grid_in_kwh": round(grid_in, 4),
            "grid_out_kwh": round(grid_out, 4),
            "auto_consumed_kwh": round(auto, 4),
            "home_consumption_kwh": round(home, 4),
            "surplus_kwh": round(solar - grid_in, 4),
            "solar_to_grid_in_pct":
                round(solar / grid_in * 100, 2) if grid_in else None,
        })
    return out


def totals_from_rows(rows: list[dict]) -> dict[str, float]:
    """Sanity check: re-sum the 24-hour rows (must equal the yearly facts)."""
    keys = ["solar_kwh", "grid_in_kwh", "grid_out_kwh",
            "auto_consumed_kwh", "home_consumption_kwh"]
    return {k: round(sum(r[k] for r in rows), 4) for k in keys}


def write_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([r[c] for c in CSV_COLUMNS])


def write_grafana(path: str, rows: list[dict]) -> None:
    """Wide table: one row per metric, one column per hour (Grafana-ready)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric"] + [f"hour{h:02d}" for h in range(HOURS_PER_DAY)])
        for metric in GRAFANA_METRICS:
            w.writerow([metric] + [round(r[metric], 4) for r in rows])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="24h solar/grid profile + seasonal highlights")
    ap.add_argument("--config", default="scripts/energy-config.yaml")
    ap.add_argument("--input", default="scripts/output/hourly-energy.csv",
                    help="Phase 2 canonical record (hourly-energy.csv)")
    ap.add_argument("--output", default="scripts/output/hourly-profile.csv",
                    help="24-row hourly-profile.csv (JSON derived from it)")
    ap.add_argument("--grafana", default=None, metavar="PATH",
                    help="optional wide table ready for a Grafana Table panel")
    args = ap.parse_args()

    cfg = vmlib.load_config(args.config)

    if not os.path.exists(args.input):
        print(f"error: {args.input} not found - run `make export` first")
        return 1

    record = vmlib.load_hourly_energy(args.input)
    required = ["utc_hour", "solar_total_kwh", "grid_in_kwh", "grid_out_kwh",
                "auto_consumed_kwh", "home_consumption_kwh"]
    missing_cols = [c for c in required if c not in record]
    if missing_cols:
        print(f"error: input {args.input} lacks columns {missing_cols}")
        return 1

    n_hours = len(record["utc_hour"])
    start = record["utc_hour"][0] if n_hours else "?"
    end = record["utc_hour"][-1] if n_hours else "?"
    print(f"Source: {args.input} (offline, no DB query)")
    print(f"Window: {start} -> {end} ({n_hours} hours)\n")

    hp_hours = _norm_hp_hours(cfg)
    seasons = cfg.get("seasons", DEFAULT_SEASONS)
    print(f"Tempo HP windows (UTC): {hp_hours}")
    print(f"Seasons: { {n: m for n, m in seasons.items()} }\n")

    hours = hourly_profile(record, hp_hours)
    seasonal = seasonal_profile(record, seasons)
    totals = totals_from_rows(hours)
    n_days = n_hours // HOURS_PER_DAY

    print(f"{'hour':>4} {'solar':>9} {'gridIn':>9} "
          f"{'HP':>8} {'HC':>8} {'gridOut':>9} {'auto':>8} "
          f"{'surplus':>9} {'solar p':>7}")
    for r in hours:
        print(f"{r['hour']:>4} {r['solar_kwh']:9.2f} "
              f"{r['grid_in_kwh']:9.2f} {r['grid_in_hp_kwh']:8.2f} "
              f"{r['grid_in_hc_kwh']:8.2f} {r['grid_out_kwh']:9.2f} "
              f"{r['auto_consumed_kwh']:8.2f} {r['surplus_kwh']:9.2f} "
              f"{r['solar_present_hours']:7d}")

    print(f"\nTotals re-summed from the 24 rows (check vs `make facts`):")
    for k, v in totals.items():
        print(f"  {k:20} {v:10.2f} kWh")

    print(f"\nSeasonal highlights (solar / grid in / grid out / "
          f"auto-consumed):")
    print(f"{'season':8} {'solar':>9} {'gridIn':>9} {'gridOut':>9} "
          f"{'auto':>8} {'home':>9} {'surplus':>9} {'solar/In%':>9}")
    for s in seasonal:
        ratio = (f"{s['solar_to_grid_in_pct']:>9.1f}"
                 if s["solar_to_grid_in_pct"] is not None else f"{'  n/a':>9}")
        print(f"{s['season']:8} {s['solar_kwh']:9.2f} "
              f"{s['grid_in_kwh']:9.2f} {s['grid_out_kwh']:9.2f} "
              f"{s['auto_consumed_kwh']:8.2f} {s['home_consumption_kwh']:9.2f} "
              f"{s['surplus_kwh']:9.2f} {ratio}")

    result = {
        "window": {"start": start, "end": end, "hours": n_hours,
                   "days": n_days},
        "source": args.input,
        "hp_hours_utc": hp_hours,
        "hours": hours,
        "totals": totals,
        "seasonal": seasonal,
    }

    write_csv(args.output, hours)
    print(f"\nWritten: {args.output} ({len(hours)} rows)")

    json_path = os.path.splitext(args.output)[0] + ".json"
    vmlib.write_json(json_path, result)
    print(f"Written: {json_path}")

    if args.grafana:
        write_grafana(args.grafana, hours)
        print(f"Written: {args.grafana} (Grafana-ready wide table)")

    return 0


if __name__ == "__main__":
    sys.exit(main())