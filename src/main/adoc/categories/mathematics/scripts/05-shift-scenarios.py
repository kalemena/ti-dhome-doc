#!/usr/bin/env python3
"""
Phase 6 - shift scenarios: "what if we move some consumption?".

Reads the Phase 2 canonical record (`hourly-energy.csv`) offline and models,
at hourly resolution, "what if" moves of part of the year's billed energy from
costlier hours to cheaper hours. Every scenario is re-priced under the 5
contracts of energy-config.yaml, with the *baseline* (no move, Tempo) as the
reference for the savings table.

Scenario families (assumptions live in `energy-config.yaml`
`shift_scenarios`, not in this script):

  * `hp_to_hc`: move a fraction of each Tempo High-cost (HP) register into the
    Low-cost (HC) register of the same Tempo color. Energy is conserved;
    only the period register moves (Tempo's HP window is 6am-10pm). One
    scenario per configured fraction (default 25 / 50 / 75 %).

  * `to_solar`: move consumption to the midday solar window (default UTC
    10:00 -> 16:00, the hours with solar surplus). The moved energy first
    displaces the solar currently exported to the grid (export is billed 0
    EUR, so the billed grid draw drops by exactly the displaced kWh); the
    uncovered remainder becomes new grid draw at the destination hour
    (Tempo HP today). `source` selects which registers move: `hp`
    (High-cost registers only) or `all` (every register). One scenario per
    (source, fraction).

  * `devices`: optional per-device consumers (Car, Water heater, ...). A
    device names a `column` of the record; when the column exists, a scenario
    moves `shiftable_fraction` of that device's hourly energy to the solar
    window using the same displacement model. No column exists today, so the
    device list is inert (the plumbing is exercised only when a metric is
    configured).

Model:

  * The Tempo color of each day is inferred from the six registers: the color
    whose registers accrued that day (White/Red are silent on their inactive
    spring/summer days - Phase 0).
  * The HP/HC window of each day is inferred from the observed HP vs HC
    counter increments (DST-aware, ~4h-20h UTC summer, ~5h-21h UTC winter).
  * The billed grid draw for an hour is the register that accrued it
    (day color x period); a shift moves that register's kWh.
  * `to_solar` displacement is capped by the window's export
    (`grid_out_kwh`), so the saving stays bounded by the solar actually sent
    to the grid (about 237 kWh/year) - add batteries to exceed it (Phase 7).

Missing hours are not interpolated: a register-hour is shiftable only when
its counter is present, and a destination hour can displace only exported
energy it actually recorded.

Output: `scripts/output/scenarios.json` + console table of yearly costs per
contract and savings vs baseline Tempo.

Usage:
    05-shift-scenarios.py [--config energy-config.yaml]
                          [--input scripts/output/hourly-energy.csv]
                          [--output scripts/output/scenarios.json]
"""

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmlib

COLORS = ["blue", "white", "red"]
DEFAULT_WINDOW = [[10, 16]]


# --------------------------------------------------------------------------
# Day geometry: Tempo color + HP/HC windows inferred from the record
# --------------------------------------------------------------------------

def hour_of(label: str) -> int:
    return int(label[11:13])


def infer_day_colors(record: dict[str, list]) -> dict[str, str]:
    """Active Tempo color per day = the register family that accrued it.

    On White/Red inactive days those registers are flat (0.000 increments) or
    absent, so the day's color is the one with the largest accrued sum. A day
    with no accrual at all falls back to blue (cover).
    """
    acc: dict[str, dict[str, float]] = {}
    for i, label in enumerate(record["utc_hour"]):
        day = label[:10]
        bucket = acc.setdefault(day, {})
        for reg in vmlib.TEMPO_METRIC_KEYS:
            v = record[reg][i]
            if not math.isnan(v):
                color = reg.split("_")[1]
                bucket[color] = bucket.get(color, 0.0) + v
    return {
        day: (max((c for c in COLORS if bucket.get(c, 0.0) > 0),
                  key=lambda c: bucket.get(c, 0.0), default="blue"))
        for day, bucket in acc.items()
    }


def infer_hp_windows(record: dict[str, list],
                     day_color: dict[str, str]) -> dict[str, list[list[int]]]:
    """Per-day [start, end) HP hour windows, from the HP>HC observations.

    The HP run is the set of hours where the day's color accrued more HP than
    HC; the window spans min -> max+1 over those hours (contiguous in
    practice, 4-20h UTC summer / 5-21h UTC winter). Fallback 4-20h.
    """
    runs: dict[str, set[int]] = {}
    for i, label in enumerate(record["utc_hour"]):
        day = label[:10]
        color = day_color[day]
        hp = record[f"tempo_{color}_hp"][i]
        hc = record[f"tempo_{color}_hc"][i]
        if not math.isnan(hp) and not math.isnan(hc) and hp > hc:
            runs.setdefault(day, set()).add(hour_of(label))
    out: dict[str, list[list[int]]] = {}
    for day, hours in runs.items():
        s, e = min(hours), max(hours) + 1
        out[day] = [[s, e]]
    return out


def hour_is_hp(record: dict[str, list], i: int, color: str,
               hp_windows: dict[str, list[list[int]]]) -> bool:
    """Whether hour i of the day-colored day accrued in the HP register.

    The HP>HC comparison is authoritative; when both registers are silent or
    equal at that hour (rounding artefacts around the switch), fall back to
    the day's inferred window.
    """
    hp = record[f"tempo_{color}_hp"][i]
    hc = record[f"tempo_{color}_hc"][i]
    if not math.isnan(hp) and not math.isnan(hc) and hp != hc:
        return hp > hc
    h = hour_of(record["utc_hour"][i])
    day = record["utc_hour"][i][:10]
    return any(s <= h < e for s, e in hp_windows.get(day, DEFAULT_WINDOW))


# --------------------------------------------------------------------------
# Scenario engine
# --------------------------------------------------------------------------

def hp_to_hc(tempo: dict[str, float], fraction: float) -> dict[str, float]:
    """Re-slot a fraction of each color's HP register into its HC register."""
    new = dict(tempo)
    for color in COLORS:
        moved = fraction * new[f"tempo_{color}_hp"]
        new[f"tempo_{color}_hp"] -= moved
        new[f"tempo_{color}_hc"] += moved
    return new


def norm_window(window) -> list[list[int]]:
    """Normalize a config window to a list of [start, end) UTC hour ranges."""
    if not window:
        return DEFAULT_WINDOW
    if isinstance(window[0], (int, float)):
        return [[int(window[0]), int(window[1])]]
    return [[int(w[0]), int(w[1])] for w in window]


def in_window(h: int, window: list[list[int]]) -> bool:
    return any(s <= h < e for s, e in window)


def to_solar(record: dict[str, list],
             tempo: dict[str, float],
             source: str,
             fraction: float,
             window: list[list[int]],
             day_color: dict[str, str],
             hp_windows: dict[str, list[list[int]]]) -> tuple[dict[str, float], dict]:
    """Move `fraction` of the `source` registers to the solar window hours.

    Returns the new six-register yearly split plus the movement accounting
    (moved / displaced export / uncovered new grid draw).
    """
    n = len(record["utc_hour"])
    source_keys = (["tempo_blue_hp", "tempo_white_hp", "tempo_red_hp"]
                   if source == "hp" else list(vmlib.TEMPO_METRIC_KEYS))

    # 1) per-hour moved amounts, from present counter increments only.
    moved = [0.0] * n
    for i in range(n):
        total = 0.0
        for reg in source_keys:
            v = record[reg][i]
            if not math.isnan(v) and v > 0:
                total += fraction * v
        moved[i] = total
    total_moved = sum(moved)

    # 2) destination hours: inside the solar window, with recorded export.
    candidates = []
    for i in range(n):
        if not in_window(hour_of(record["utc_hour"][i]), window):
            continue
        out = record["grid_out_kwh"][i]
        if not math.isnan(out) and out > 0:
            candidates.append(i)

    # 3) greedy displacement of export in chronological order (deterministic).
    remaining = total_moved
    displaced = 0.0
    for i in candidates:
        take = min(remaining, record["grid_out_kwh"][i])
        displaced += take
        remaining -= take
    leftover = total_moved - displaced   # becomes new grid draw at dest

    # 4) apply the register bookkeeping: sources shrink, leftover lands in
    #    the destination hour registers (day color, HP).
    new = dict(tempo)
    for reg in source_keys:
        new[reg] -= fraction * sum(
            record[reg][i] for i in range(n)
            if not math.isnan(record[reg][i]) and record[reg][i] > 0)
    if leftover > 0 and candidates:
        share = leftover / len(candidates)
        for i in candidates:
            day = record["utc_hour"][i][:10]
            color = day_color[day]
            period = "hp" if hour_is_hp(record, i, color, hp_windows) else "hc"
            new[f"tempo_{color}_{period}"] += share

    accounting = {
        "moved_kwh": round(total_moved, 4),
        "covered_by_solar_kwh": round(displaced, 4),
        "uncovered_new_grid_draw_kwh": round(leftover, 4),
    }
    return new, accounting


def price(contracts: dict[str, vmlib.Tariff],
          kwh: dict[str, float]) -> tuple[dict[str, dict], list[str]]:
    """Yearly cost per contract + ranking (ascending) for a register split."""
    costs = {name: t.cost(kwh) for name, t in contracts.items()}
    ranking = sorted(costs, key=lambda n: costs[n])
    priced = {name: {"cost": round(costs[name], 2),
                     "delta_vs_tempo_eur": None,
                     "delta_vs_tempo_pct": None}
              for name in costs}
    return priced, ranking


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="What-if shift scenarios re-priced under the 5 contracts")
    ap.add_argument("--config", default="scripts/energy-config.yaml")
    ap.add_argument("--input", default="scripts/output/hourly-energy.csv",
                    help="Phase 2 canonical record (hourly-energy.csv)")
    ap.add_argument("--output", default="scripts/output/scenarios.json")
    args = ap.parse_args()

    cfg = vmlib.load_config(args.config)

    if not os.path.exists(args.input):
        print(f"error: {args.input} not found - run `make export` first")
        return 1

    record = vmlib.load_hourly_energy(args.input)
    required = (["utc_hour", "grid_out_kwh"]
                + list(vmlib.TEMPO_METRIC_KEYS))
    missing_cols = [c for c in required if c not in record]
    if missing_cols:
        print(f"error: input {args.input} lacks columns {missing_cols}")
        return 1

    n_hours = len(record["utc_hour"])
    start = record["utc_hour"][0] if n_hours else "?"
    end = record["utc_hour"][-1] if n_hours else "?"
    print(f"Source: {args.input} (offline, no DB query)")
    print(f"Window: {start} -> {end} ({n_hours} hours)\n")

    scfg = cfg.get("shift_scenarios", {})
    tariffs = vmlib.load_tariffs(cfg)
    tempo = {m: sum(v for v in record[m] if not math.isnan(v))
             for m in vmlib.TEMPO_METRIC_KEYS}
    tempo_total = sum(tempo.values())
    baseline_cost = tariffs["tempo"].cost(tempo)

    print("Day geometry inferred from the record:")
    day_color = infer_day_colors(record)
    hp_windows = infer_hp_windows(record, day_color)
    w = list(hp_windows.values())[0][0] if hp_windows else None
    print(f"  HP hour windows: {w if w else 'n/a'} UTC (trend across days), "
          f"{len(day_color)} days classified")
    print(f"  active days per color: "
          f"{ {c: sum(1 for d in day_color.values() if d == c) for c in COLORS} }\n")

    baseline, _ranking = price(tariffs, tempo)
    scenarios = [{
        "name": "baseline",
        "description": "no move - current yearly consumption",
        "params": {},
        "moved_kwh": 0.0,
        "covered_by_solar_kwh": 0.0,
        "uncovered_new_grid_draw_kwh": 0.0,
        "tempo_kwh": {m: round(tempo[m], 4) for m in vmlib.TEMPO_METRIC_KEYS},
        "tempo_total_kwh": round(tempo_total, 4),
        "contracts": baseline,
    }]

    def add_scenario(name, description, params, kwh, accounting):
        priced, ranking = price(tariffs, kwh)
        for n in priced:
            delta = priced[n]["cost"] - round(baseline_cost, 2)
            priced[n]["delta_vs_tempo_eur"] = round(delta, 2)
            priced[n]["delta_vs_tempo_pct"] = (
                round(delta / baseline_cost * 100, 2) if baseline_cost else None)
        scenarios.append({
            "name": name,
            "description": description,
            "params": params,
            "moved_kwh": accounting.get("moved_kwh", 0.0),
            "covered_by_solar_kwh":
                accounting.get("covered_by_solar_kwh", 0.0),
            "uncovered_new_grid_draw_kwh":
                accounting.get("uncovered_new_grid_draw_kwh", 0.0),
            "tempo_kwh": {m: round(kwh[m], 4) for m in vmlib.TEMPO_METRIC_KEYS},
            "tempo_total_kwh": round(sum(kwh.values()), 4),
            "contracts": priced,
            "ranking": ranking,
            "best_contract": ranking[0],
        })

    # --- hp -> hc scenarios -------------------------------------------------
    for f in scfg.get("hp_to_hc_fractions", []):
        f = float(f)
        new = hp_to_hc(tempo, f)
        add_scenario(
            f"hp_to_hc_{int(f * 100)}",
            f"{f * 100:.0f} % of High-cost consumption delayed to Low-cost "
            f"hours (same Tempo color)",
            {"family": "hp_to_hc", "fraction": f},
            new, {"moved_kwh": sum(tempo[f"tempo_{c}_hp"]
                                   for c in COLORS) * f})

    # --- to solar scenarios -------------------------------------------------
    tos = scfg.get("to_solar", {})
    window = norm_window(tos.get("window_hours"))
    window_txt = " + ".join(f"{s:02d}-{e:02d} UTC" for s, e in window)
    print(f"Solar shift window: {window_txt}")
    for source, f in tos.get("source_fractions", {}).items():
        f = float(f)
        new, acc = to_solar(record, tempo, source, f, window,
                            day_color, hp_windows)
        add_scenario(
            f"to_solar_{source}_{int(f * 100)}",
            f"{f * 100:.0f} % of {'High-cost' if source == 'hp' else 'all'} "
            f"consumption moved to solar hours ({window_txt})",
            {"family": "to_solar", "source": source, "fraction": f,
             "window_hours_utc": window},
            new, acc)

    # --- per-device scenarios (optional, inert when no column exists) ------
    for dev, dev_cfg in scfg.get("devices", {}).items():
        column = dev_cfg.get("column")
        fraction = float(dev_cfg.get("shiftable_fraction", 1.0))
        if not column or column not in record:
            print(f"note: device '{dev}' column '{column}' not in the "
                  f"record - scenario skipped")
            continue
        n = n_hours
        dev_moved = [0.0] * n
        for i in range(n):
            v = record[column][i]
            if not math.isnan(v) and v > 0:
                dev_moved[i] = fraction * v
        total_moved = sum(dev_moved)
        candidates = [i for i in range(n)
                      if in_window(hour_of(record["utc_hour"][i]), window)
                      and not math.isnan(record["grid_out_kwh"][i])
                      and record["grid_out_kwh"][i] > 0]
        remaining, displaced = total_moved, 0.0
        for i in candidates:
            take = min(remaining, record["grid_out_kwh"][i])
            displaced += take
            remaining -= take
        leftover = total_moved - displaced
        new = dict(tempo)
        for i in range(n):
            if dev_moved[i] <= 0:
                continue
            day = record["utc_hour"][i][:10]
            color = day_color[day]
            period = "hp" if hour_is_hp(record, i, color, hp_windows) else "hc"
            new[f"tempo_{color}_{period}"] -= dev_moved[i]
        if leftover > 0 and candidates:
            share = leftover / len(candidates)
            for i in candidates:
                day = record["utc_hour"][i][:10]
                color = day_color[day]
                new[f"tempo_{color}_hp"] += share
        add_scenario(
            f"device_{dev}_to_solar",
            f"{fraction * 100:.0f} % of the '{dev}' consumption moved to "
            f"solar hours ({window_txt})",
            {"family": "device", "device": dev, "column": column,
             "shiftable_fraction": fraction, "window_hours_utc": window},
            new, {"moved_kwh": total_moved,
                  "covered_by_solar_kwh": displaced,
                  "uncovered_new_grid_draw_kwh": leftover})

    # --- report --------------------------------------------------------------
    names = [s["name"] for s in scenarios]
    print(f"Scenarios: {', '.join(names)}\n")
    print(f"{'scenario':28} {'tempo':>9} {'blue':>9} {'zenfix':>9} "
          f"{'zenfix-t':>9} {'base':>9} {'saving vs tempo':>16}")
    for s in scenarios:
        c = s["contracts"]
        saving = round(baseline_cost - c["tempo"]["cost"], 2)
        print(f"{s['name']:28} {c['tempo']['cost']:9.2f} "
              f"{c['blue']['cost']:9.2f} {c['zenfix']['cost']:9.2f} "
              f"{c['zenfix-tempo']['cost']:9.2f} {c['base']['cost']:9.2f} "
              f"{saving:14.2f} EUR")
        if s["moved_kwh"]:
            print(f"{'':28}   moved {s['moved_kwh']:8.1f} kWh, "
                  f"solar-covered {s['covered_by_solar_kwh']:7.1f}, "
                  f"new grid draw {s['uncovered_new_grid_draw_kwh']:7.1f}, "
                  f"best {s['best_contract']}")

    result = {
        "window": {"start": start, "end": end, "hours": n_hours},
        "source": args.input,
        "solar_shift_window_hours_utc": window,
        "baseline_tempo_cost": round(baseline_cost, 2),
        "baseline": {
            "tempo_kwh": {m: round(tempo[m], 4) for m in vmlib.TEMPO_METRIC_KEYS},
            "tempo_total_kwh": round(tempo_total, 4),
            "contracts": {n: c["cost"] for n, c in baseline.items()},
        },
        "scenarios": scenarios,
    }
    vmlib.write_json(args.output, result)
    print(f"\nWritten: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())