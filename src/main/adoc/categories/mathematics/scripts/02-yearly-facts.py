#!/usr/bin/env python3
"""
Phase 3 - yearly facts: the static "time facts" of the analysis.

Reads the Phase 2 canonical record (`hourly-energy.csv`) offline and emits
`facts.csv`: the yearly total of every counter (solar, grid, the three Tempo
colors x HP+HC) plus the derived auto-consumed solar and home consumption.
The identity check Grid In vs sum(Tempo) is recomputed per day over the
*available* counters: on inactive White/Red days those counters are silent
(no data) and the Blue counters cover the consumption, exactly as established
in Phase 0.

No DB query is made: totals and the identity are rebuilt from the hourly
increments of the record, so the phase is offline, fast and deterministic.

Facts computed (per the window of the record, default 2025-09-01 -> 2026-09-01):
  solar_total_kwh          opendtu AC yearly increase
  grid_in_kwh / grid_out_kwh  zigbee meter yearly increases
  tempo_{blue,white,red}_hp_hc  teleinfo yearly increase per Tempo color
                           (the record carries HP+HC combined per color)
  auto_consumed_kwh        = solar - grid_out (per hour, then summed)
  home_consumption_kwh     = grid_in + auto_consumed (per hour, then summed)
  identity                 Grid In vs sum(available Tempo counters) per day,
                           PASS/FAIL against `identity_tolerance`
                           (default 5 %).

Missing hours are not interpolated: each column is summed over its present
hours and the count of missing hours is reported, so gaps stay visible.

Usage:
    02-yearly-facts.py [--config energy-config.yaml]
                       [--input scripts/output/hourly-energy.csv]
                       [--output scripts/output/facts.csv] [--tolerance 0.05]
"""

import argparse
import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmlib

TEMPO_COLUMNS = ["tempo_blue_hp_hc", "tempo_white_hp_hc", "tempo_red_hp_hc"]

# metric column (in the record) -> row name in facts.csv
FACT_METRICS = [
    ("solar_total_kwh", "solar_total_kwh", "Solar Total (AC)"),
    ("grid_in_kwh", "grid_in_kwh", "Grid In"),
    ("grid_out_kwh", "grid_out_kwh", "Grid Out"),
]
FACT_METRICS += [(c, c, f"Tempo {c.split('_')[1]} HP+HC")
                 for c in TEMPO_COLUMNS]
FACT_METRICS += [
    ("auto_consumed_kwh", "auto_consumed_kwh", "Auto-consumed Solar = Solar - Grid Out"),
    ("home_consumption_kwh", "home_consumption_kwh", "Home Consumption = Grid In + Auto-consumed"),
]


def _present(values: list[float]) -> list[float]:
    return [v for v in values if not math.isnan(v)]


def yearly_total(record: dict[str, list], column: str) -> tuple[float, int]:
    values = _present(record[column])
    return sum(values), len(record[column]) - len(values)


def identity_check(record: dict[str, list],
                   tolerance: float) -> dict:
    """Grid In vs sum of *available* Tempo counters, computed per day.

    Each day contributes its available counters only: a color whose column is
    entirely empty that day is "inactive" (the White/Red silent days,
    covered by Blue) and is recorded as incomplete, not as a fault. Partial
    days (some missing hours in an otherwise active counter) still contribute
    their present hours; the divergence of full-coverage days is checked
    against the 5 % margin like Phase 0.
    """
    grid = record["grid_in_kwh"]
    tempo = {c: record[c] for c in TEMPO_COLUMNS}
    colors = list(TEMPO_COLUMNS)

    days: dict[str, dict[str, list[float]]] = {}
    for i, label in enumerate(record["utc_hour"]):
        day = label[:10]
        bucket = days.setdefault(day, {"grid": [], "tempo": {c: [] for c in colors}})
        if not math.isnan(grid[i]):
            bucket["grid"].append(grid[i])
        for c in colors:
            if not math.isnan(tempo[c][i]):
                bucket["tempo"][c].append(tempo[c][i])

    tolerance_pct = tolerance * 100
    tot_grid = 0.0
    tot_tempo = 0.0
    incomplete_days = []
    diverging_days = []

    for day, b in sorted(days.items()):
        grid_day = sum(b["grid"])
        if not b["grid"]:
            continue
        tot_grid += grid_day
        missing = []
        tempo_day = 0.0
        for c in colors:
            present = b["tempo"][c]
            if not present:
                missing.append(c)
                continue
            tempo_day += sum(present)
        tot_tempo += tempo_day
        if missing:
            incomplete_days.append({"date": day, "grid_kwh": round(grid_day, 2),
                                    "missing": missing})
        elif (len(b["grid"]) == 24
              and all(len(b["tempo"][c]) == 24 for c in colors)
              and abs(grid_day - tempo_day) > tolerance * max(grid_day, 0.001)):
            diverging_days.append({"date": day, "grid_kwh": round(grid_day, 2),
                                   "tempo_kwh": round(tempo_day, 2)})

    diff = tot_grid - tot_tempo
    diff_pct = diff / tot_grid * 100 if tot_grid else None
    return {
        "grid_in_total_kwh": round(tot_grid, 2),
        "tempo_sum_total_kwh": round(tot_tempo, 2),
        "diff_kwh": round(diff, 2),
        "diff_pct": round(diff_pct, 2) if diff_pct is not None else None,
        "pass": diff_pct is not None and abs(diff_pct) <= tolerance_pct,
        "tolerance_pct": tolerance_pct,
        "incomplete_days": incomplete_days,
        "diverging_days": diverging_days,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Yearly energy facts from the canonical hourly record")
    ap.add_argument("--config", default="scripts/energy-config.yaml")
    ap.add_argument("--input", default="scripts/output/hourly-energy.csv",
                    help="Phase 2 canonical record (hourly-energy.csv)")
    ap.add_argument("--output", default="scripts/output/facts.csv")
    ap.add_argument("--tolerance", type=float, default=None,
                    help="identity tolerance, fraction (default from config)")
    args = ap.parse_args()

    cfg = vmlib.load_config(args.config)
    tolerance = (args.tolerance if args.tolerance is not None
                 else cfg.get("identity_tolerance", 0.05))

    if not os.path.exists(args.input):
        print(f"error: {args.input} not found - run `make export` first")
        return 1

    record = vmlib.load_hourly_energy(args.input)
    required = ["utc_hour"] + [m[0] for m in FACT_METRICS]
    missing_cols = [c for c in required if c not in record]
    if missing_cols:
        print(f"error: input {args.input} lacks columns {missing_cols}")
        return 1

    n_hours = len(record["utc_hour"])
    start = record["utc_hour"][0] if n_hours else "?"
    end = record["utc_hour"][-1] if n_hours else "?"
    print(f"Source: {args.input} (offline, no DB query)")
    print(f"Window: {start} -> {end} ({n_hours} hours)\n")

    facts = {name: yearly_total(record, column) for column, name, _ in FACT_METRICS}
    identity = identity_check(record, tolerance)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    rows = [(name, total, missing, note)
            for column, name, note in FACT_METRICS
            for total, missing in [facts[name]]]

    print(f"{'metric':24} {'kWh':>12} {'missing h':>10}")
    for name, total, missing, _ in rows:
        print(f"{name:24} {total:12.4f} {missing:10d}")

    print("\nIdentity check (Grid In vs sum of available Tempo counters):")
    print(f"  Grid In : {identity['grid_in_total_kwh']:>10.2f} kWh")
    print(f"  Tempo   : {identity['tempo_sum_total_kwh']:>10.2f} kWh")
    print(f"  delta   : {identity['diff_kwh']:>10.2f} kWh  "
          f"({identity['diff_pct']} %)")
    status = "PASS" if identity["pass"] else "FAIL"
    print(f"  {status} (|delta| <= {identity['tolerance_pct']:.1f} % "
          f"tolerance)")
    print(f"  incomplete days (inactive color, covered by Blue): "
          f"{len(identity['incomplete_days'])}")
    if identity["diverging_days"]:
        print(f"  diverging full days > 5 %: "
              f"{len(identity['diverging_days'])}")
        print(f"    first: {identity['diverging_days'][0]}")

    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value_kwh", "missing_hours", "note"])
        for name, total, missing, note in rows:
            w.writerow([name, f"{total:.4f}", missing, note])
        w.writerow(["identity_grid_in_kwh", f"{identity['grid_in_total_kwh']:.4f}",
                    "", "Grid In sum over available days"])
        w.writerow(["identity_tempo_kwh", f"{identity['tempo_sum_total_kwh']:.4f}",
                    "", "sum of available Tempo counters per day"])
        w.writerow(["identity_diff_kwh", f"{identity['diff_kwh']:.4f}",
                    "", "grid_in - tempo"])
        w.writerow(["identity_diff_pct",
                    "" if identity["diff_pct"] is None
                    else f"{identity['diff_pct']:.2f}", "",
                    f"(grid_in - tempo) / grid_in * 100 (tolerance "
                    f"{identity['tolerance_pct']:.1f} %)"])
        w.writerow(["identity_status",
                    "PASS" if identity["pass"] else "FAIL", "", ""])

    print(f"\nWritten: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())