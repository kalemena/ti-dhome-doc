#!/usr/bin/env python3
"""
Phase 7 - solar scaling and battery sizing (readme "Question - predictions").

Reads the Phase 2 canonical record (`hourly-energy.csv`) offline and models
"what if we add solar" and "what if we add batteries", at hourly resolution,
for each solar scale `k` in `solar_scenarios.scale_factors`.

Solar scaling
-------------
A synthetic profile is built by multiplying the measured hourly solar by `k`
(assumption: new panels have the same diurnal shape). The *displacement model*
is anchored on the measured meter:

    d_i            = (k - 1) * solar_i                (additional production)
    grid_in'  = grid_in  - min(d_i, grid_in)          one kWh of new solar
                                                      displaces one kWh of
                                                      grid draw, up to what
                                                      was actually drawn
    grid_out' = grid_out + max(0, d_i - grid_in')     the rest is exported
    auto'     = auto     + min(d_i, grid_in)          solar consumed at home

At k=1 the model reproduces the meter exactly, so the baseline identity with
Phase 4 is preserved. Every k is re-priced under the 5 contracts of
energy-config.yaml; the register that bills each hour is inferred from its
day Tempo color and HP/HC period (same geometry as Phase 6), and each hour's
grid-draw reduction is subtracted from that register (only over hours where
the register actually accrued, so missing hours stay untouched).

Battery simulation
------------------
Per (k, capacity) an hourly chronological simulation runs the battery with
`battery` config parameters (round-trip efficiency, max charge/discharge
power, optional daily-cycle cap). The battery charges from the solar surplus
that would otherwise be exported (no grid-charging arbitrage) and discharges
to cover the grid draw of later hours. For each (k, capacity) the output
lists the auto-consumption gain, the Grid In reduction (kWh and %) and the
yearly cost saving under each contract.

The `charge from surplus -> discharge to cover draw` split is one-directional
per hour: an hour with export pool only charges, an hour without only
discharges.

Optimal battery size
--------------------
A fine marginal sweep (0..sizing_max_kwh by sizing_step_kwh) records, per k,
the yearly saving of the last kWh of storage added (marginal saving per
added kWh, in EUR/kWh-year) measured under the reference contract ('best' =
cheapest contract at that k, or a named contract). The recommended capacity
per k is the largest sweep step whose marginal value is still >=
`marginal_saving_threshold_eur_per_kwh`; the marginal curve and the
recommendation are emitted to `--sizing-output` (battery-sizing.csv) and in
the JSON.

Outputs:
  * `scripts/output/solar-scenarios.json`     the k x capacity grid + break-even
  * `scripts/output/battery-sizing.csv`       marginal savings per k and capacity
  * summary tables on the console

Usage:
    06-solar-scenarios.py [--config energy-config.yaml]
                          [--input scripts/output/hourly-energy.csv]
                          [--output scripts/output/solar-scenarios.json]
                          [--sizing-output scripts/output/battery-sizing.csv]
"""

import argparse
import csv
import importlib.util
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmlib


def _load_phase6() -> "module":
    """Import the Phase 6 module for the day color / HP window geometry."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "05-shift-scenarios.py")
    if not os.path.exists(path):
        raise RuntimeError("05-shift-scenarios.py not found - Phase 6 must "
                           "land before Phase 7 (shared day geometry)")
    spec = importlib.util.spec_from_file_location("shift_scenarios_05", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sum_present(values: list[float]) -> float:
    return sum(v for v in values if not math.isnan(v))


def register_for_hours(record: dict[str, list],
                       geometry) -> tuple[list[str], list[bool]]:
    """Per-hour billing register (day color x HP/HC period).

    Returns the register key and whether that register accrued (is present)
    at the hour, so reductions only apply over hours the meter actually
    billed (the same basis as Phase 4).
    """
    day_color = geometry.infer_day_colors(record)
    hp_windows = geometry.infer_hp_windows(record, day_color)
    reg: list[str] = []
    present: list[bool] = []
    for i in range(len(record["utc_hour"])):
        day = record["utc_hour"][i][:10]
        color = day_color[day]
        period = ("hp" if geometry.hour_is_hp(record, i, color, hp_windows)
                  else "hc")
        key = f"tempo_{color}_{period}"
        reg.append(key)
        present.append(not math.isnan(record[key][i]))
    return reg, present


def scale_arrays(record: dict[str, list], k: float) -> dict[str, list[float]]:
    """Per-hour scaled solar + anchored displacement model at factor k.

    All output arrays have the record's length; missing solar/grid hours stay
    NaN-propagated (a missing hour contributes no displacement).
    """
    n = len(record["utc_hour"])
    P = record["solar_total_kwh"]
    gi = record["grid_in_kwh"]
    go = record["grid_out_kwh"]
    auto = record["auto_consumed_kwh"]

    extra = [0.0] * n
    reduction = [0.0] * n
    export_pool = [0.0] * n
    import_no_bat = [0.0] * n
    auto_no_bat = [0.0] * n

    for i in range(n):
        solar = P[i]
        grid = gi[i]
        if math.isnan(grid):
            continue
        d = 0.0 if math.isnan(solar) else max(0.0, (k - 1.0) * solar)
        disp = min(d, grid)
        extra[i] = d
        reduction[i] = disp
        export_pool[i] = (0.0 if math.isnan(go[i]) else go[i]
                          + max(0.0, d - disp))
        import_no_bat[i] = grid - disp
        auto_no_bat[i] = (0.0 if math.isnan(auto[i]) else auto[i]) + disp
    return {
        "extra_kwh": extra,
        "grid_in_reduction_kwh": reduction,
        "export_pool_kwh": export_pool,
        "grid_in_no_bat_kwh": import_no_bat,
        "auto_no_bat_kwh": auto_no_bat,
    }


def simulate_battery(imp: list[float], pool: list[float], capacity: float,
                     efficiency: float, max_charge: float, max_discharge: float,
                     max_daily_cycles: float | None) -> dict:
    """Hourly chronological battery model over the scaled arrays.

    Charges from the export pool (would-be surplus, so the efficiency loss is
    paid from the surplus: storing `s` kWh consumes `s / efficiency` kWh of
    the pool); discharges to cover grid draw up to power/soc. One direction
    per hour: pool hours charge, others can discharge. Returns the final
    per-hour grid draw, the total energy discharged and the battery cycling
    usage.
    """
    n = len(imp)
    soc = 0.0
    final = [0.0] * n
    total_charge = 0.0
    total_discharge = 0.0
    day_charged = 0.0
    last_day = None
    day_cap = None if max_daily_cycles is None else max_daily_cycles * capacity

    for i in range(n):
        day = i // 24
        if day != last_day:
            last_day = day
            day_charged = 0.0
        if pool[i] > 0.0 and soc < capacity:
            stored = min(pool[i] * efficiency, max_charge, capacity - soc)
            if day_cap is not None:
                stored = max(0.0, min(stored, day_cap - day_charged))
            soc += stored
            day_charged += stored
            total_charge += stored
            final[i] = imp[i]
        else:
            draw = imp[i]
            if draw > 0.0 and soc > 0.0:
                dch = min(draw, max_discharge, soc)
                soc -= dch
                draw -= dch
                total_discharge += dch
            final[i] = draw

    return {
        "grid_in_kwh": final,
        "total_charged_kwh": total_charge,
        "total_discharged_kwh": total_discharge,
        "cycles_used": (total_charge / capacity if capacity else 0.0),
    }


def simulate_battery_night(imp: list[float], pool: list[float],
                           day_surplus: list[float], hour_period: list[str],
                           capacity: float, efficiency: float, max_charge: float,
                           max_discharge: float,
                           max_daily_cycles: float | None) -> dict:
    """Battery model with night grid charging (Tempo HC arbitrage).

    Same hourly chronological engine that charges from the would-be export
    pool, extended with paid charging from the grid during the pre-dawn night
    hours (UTC hour < 6, Tempo Low-cost HC). The paid charging is capped per
    UTC day at the capacity the day's surplus solar will not fill
    (`capacity - day_surplus`), so the sun always gets the first word: grid
    kWh are bought only when the day cannot fill the battery for free.
    Discharge serves High-cost (HP) hours only, so stored kWh displace the
    costlier period instead of being burned on cheap HC draw.

    Each kWh *stored* costs `efficiency`-inverted kWh drawn from the grid
    (returned in `grid_draw_added`, billed at the night register). Returns
    the final per-hour grid draw, the paid kWh, and the cycling usage.
    """
    n = len(imp)
    soc = 0.0
    final = [0.0] * n
    grid_draw_added = [0.0] * n
    total_charge = 0.0
    total_discharge = 0.0
    total_grid = 0.0
    day_charged = 0.0
    day_grid = 0.0
    last_day = None
    day_cap = (None if max_daily_cycles is None
               else max_daily_cycles * capacity)

    for i in range(n):
        day = i // 24
        if day != last_day:
            last_day = day
            day_charged = 0.0
            day_grid = 0.0
        if pool[i] > 0.0 and soc < capacity:
            stored = min(pool[i] * efficiency, max_charge, capacity - soc)
            if day_cap is not None:
                stored = max(0.0, min(stored, day_cap - day_charged))
            soc += stored
            day_charged += stored
            total_charge += stored
            final[i] = imp[i]
        elif (i % 24 < 6 and soc < capacity
              and capacity - day_surplus[day] - day_grid > 0):
            stored = min(max_charge, capacity - soc,
                         capacity - day_surplus[day] - day_grid)
            if day_cap is not None:
                stored = max(0.0, min(stored, day_cap - day_charged))
            if stored > 0.0:
                soc += stored
                grid_draw_added[i] = stored / efficiency
                day_charged += stored
                day_grid += stored
                total_charge += stored
                total_grid += stored
            final[i] = imp[i]
        else:
            draw = imp[i]
            if draw > 0.0 and soc > 0.0 and hour_period[i] == "hp":
                dch = min(draw, max_discharge, soc)
                soc -= dch
                draw -= dch
                total_discharge += dch
            final[i] = draw

    return {
        "grid_in_kwh": final,
        "grid_draw_added_kwh": grid_draw_added,
        "total_charged_kwh": total_charge,
        "total_grid_stored_kwh": total_grid,
        "total_discharged_kwh": total_discharge,
        "cycles_used": (total_charge / capacity if capacity else 0.0),
    }


def register_reduction(baseline_reg: dict[str, float],
                       reduction_hour: list[float], hour_reg: list[str],
                       hour_present: list[bool]) -> dict[str, float]:
    """New register totals after subtracting per-hour grid-draw reductions.

    Only hours where the billing register actually accrued contribute, so the
    reductions never touch the missing (never billed) hours of a register and
    the k=1 case reproduces the Phase 4 baseline exactly.
    """
    reduced: dict[str, float] = {r: 0.0 for r in vmlib.TEMPO_METRIC_KEYS}
    for i, reg in enumerate(hour_reg):
        if hour_present[i]:
            reduced[reg] += reduction_hour[i]
    return {r: max(0.0, baseline_reg[r] - reduced[r])
            for r in vmlib.TEMPO_METRIC_KEYS}


def price_all(tariffs: dict[str, vmlib.Tariff],
              regs: dict[str, float]) -> tuple[dict[str, dict], str]:
    priced = {}
    for name, t in tariffs.items():
        cost = t.cost(regs)
        priced[name] = {"cost": round(cost, 2)}
    cheapest = min(priced, key=lambda n: priced[n]["cost"])
    return priced, cheapest


def baseline_facts(record: dict[str, list]) -> dict:
    solar = sum_present(record["solar_total_kwh"])
    auto = sum_present(record["auto_consumed_kwh"])
    gi = sum_present(record["grid_in_kwh"])
    go = sum_present(record["grid_out_kwh"])
    home = sum_present(record["home_consumption_kwh"])
    regs = {m: sum_present(record[m]) for m in vmlib.TEMPO_METRIC_KEYS}
    return {
        "solar_total_kwh": solar,
        "grid_in_kwh": gi,
        "grid_out_kwh": go,
        "auto_consumed_kwh": auto,
        "home_consumption_kwh": home,
        "auto_consumption_pct": round(auto / solar * 100, 2) if solar else None,
        "tempo_kwh": {m: round(v, 4) for m, v in regs.items()},
        "tempo_total_kwh": round(sum(regs.values()), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Solar scaling (x1/x2/x3) + battery sizing scenarios")
    ap.add_argument("--config", default="scripts/energy-config.yaml")
    ap.add_argument("--input", default="scripts/output/hourly-energy.csv",
                    help="Phase 2 canonical record (hourly-energy.csv)")
    ap.add_argument("--output", default="scripts/output/solar-scenarios.json")
    ap.add_argument("--sizing-output",
                    default="scripts/output/battery-sizing.csv")
    args = ap.parse_args()

    cfg = vmlib.load_config(args.config)
    scfg = cfg.get("solar_scenarios", {})
    scale_factors = [float(k) for k in scfg.get("scale_factors", [1.0])]
    batt_cfg = scfg.get("battery", {})
    capacities = [float(c) for c in batt_cfg.get("capacities_kwh", [])]
    efficiency = float(batt_cfg.get("round_trip_efficiency", 0.9))
    max_charge = float(batt_cfg.get("max_charge_power_kw",
                                    3.5 if capacities else 0.0))
    max_discharge = float(batt_cfg.get("max_discharge_power_kw", max_charge))
    max_daily_cycles = batt_cfg.get("max_daily_cycles")
    if max_daily_cycles is not None:
        max_daily_cycles = float(max_daily_cycles)
    sizing_step = float(batt_cfg.get("sizing_step_kwh", 1.0))
    sizing_max = float(batt_cfg.get("sizing_max_kwh", 20.0))
    threshold = float(scfg.get(
        "marginal_saving_threshold_eur_per_kwh", 0.30))
    ref_contract = scfg.get("marginal_reference_contract", "best")
    night_cfg = scfg.get("night_grid_charge", {})
    night_enabled = bool(night_cfg.get("enabled", False))
    night_capacities = [float(c) for c in night_cfg.get("capacities_kwh", [])]
    be = scfg.get("break_even", {})
    be_min = float(be.get("k_min", 1.0))
    be_max = float(be.get("k_max", 4.0))
    be_step = float(be.get("k_step", 0.1))
    be_table = [float(k) for k in be.get("table_factors", [])]

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

    tariffs = vmlib.load_tariffs(cfg)
    base = baseline_facts(record)
    base_regs = {m: float(base["tempo_kwh"][m]) for m in vmlib.TEMPO_METRIC_KEYS}
    baseline_priced, baseline_cheapest = price_all(tariffs, base_regs)

    geometry = _load_phase6()
    hour_reg, hour_present = register_for_hours(record, geometry)

    print("Baseline (measured, k=1):")
    print(f"  solar {base['solar_total_kwh']:9.2f} kWh | auto "
          f"{base['auto_consumed_kwh']:9.2f} ({base['auto_consumption_pct']} %) "
          f"| grid in {base['grid_in_kwh']:9.2f} | grid out "
          f"{base['grid_out_kwh']:9.2f}")
    print(f"  tempo registers: { {m: round(v, 4) for m, v in base_regs.items()} }")

    scaling_out = []
    for k in scale_factors:
        scaled = scale_arrays(record, k)
        auto = sum_present(scaled["auto_no_bat_kwh"])
        gi = sum_present(scaled["grid_in_no_bat_kwh"])
        go_pool = sum_present(scaled["export_pool_kwh"])
        solar = k * base["solar_total_kwh"]
        regs = register_reduction(base_regs, scaled["grid_in_reduction_kwh"],
                                  hour_reg, hour_present)
        priced, cheapest = price_all(tariffs, regs)
        tempo_optimal = cheapest == "tempo"

        gi_reduction = base["grid_in_kwh"] - gi
        gi_reduction_pct = (gi_reduction / base["grid_in_kwh"] * 100
                            if base["grid_in_kwh"] else None)

        batteries = []
        if capacities:
            imp = list(scaled["grid_in_no_bat_kwh"])
            pool = list(scaled["export_pool_kwh"])
            for cap in [0.0] + capacities:
                sim = simulate_battery(imp, pool, cap, efficiency,
                                       max_charge, max_discharge,
                                       max_daily_cycles)
                gi_bat = sum_present(sim["grid_in_kwh"])
                red_kwh = gi - gi_bat
                red_pct = red_kwh / gi * 100 if gi else None
                auto_bat = auto + sim["total_discharged_kwh"]
                auto_pct = (auto_bat / solar * 100 if solar else None)
                auto_pct_no_bat = (auto / solar * 100 if solar else None)
                regs_bat = register_reduction(
                    base_regs,
                    [scaled["grid_in_reduction_kwh"][i]
                     + (imp[i] - sim["grid_in_kwh"][i])
                     for i in range(n_hours)],
                    hour_reg, hour_present)
                priced_bat, _cheapest_bat = price_all(tariffs, regs_bat)
                contracts = {}
                for name, bcost in priced.items():
                    saving = bcost["cost"] - priced_bat[name]["cost"]
                    contracts[name] = {
                        "cost": priced_bat[name]["cost"],
                        "saving_vs_no_battery_eur": round(saving, 2),
                        "saving_vs_baseline_eur": round(
                            baseline_priced[name]["cost"]
                            - priced_bat[name]["cost"], 2),
                    }
                batteries.append({
                    "capacity_kwh": cap,
                    "auto_consumption_pct": round(auto_pct, 2),
                    "auto_consumption_gain_pp": round(auto_pct - auto_pct_no_bat, 2),
                    "grid_in_kwh": round(gi_bat, 2),
                    "grid_in_reduction_kwh": round(red_kwh, 2),
                    "grid_in_reduction_pct": round(red_pct, 2) if red_pct is not None else None,
                    "total_discharged_kwh": round(sim["total_discharged_kwh"], 2),
                    "total_charged_kwh": round(sim["total_charged_kwh"], 2),
                    "cycles_used": round(sim["cycles_used"], 1),
                    "contracts": contracts,
                })

        night_batteries = []
        if night_enabled and night_capacities:
            hour_period = ["hp" if r.endswith("_hp") else "hc"
                           for r in hour_reg]
            n_days = n_hours // 24
            day_surplus = [sum(pool[d * 24:(d + 1) * 24])
                           for d in range(n_days)]
            for cap in [0.0] + night_capacities:
                night = {
                    "capacity_kwh": cap,
                    "auto_consumption_pct": round(auto / solar * 100, 2)
                    if solar else None,
                    "auto_consumption_gain_pp": 0.0,
                    "grid_in_kwh": round(gi, 2),
                    "grid_in_reduction_kwh": 0.0,
                    "grid_in_reduction_pct": 0.0,
                    "grid_charged_kwh": 0.0,
                    "grid_charged_stored_kwh": 0.0,
                    "total_discharged_kwh": 0.0,
                    "total_charged_kwh": 0.0,
                    "cycles_used": 0.0,
                    "contracts": {n: {
                        "cost": priced[n]["cost"],
                        "saving_vs_no_battery_eur": 0.0,
                        "saving_vs_baseline_eur": round(
                            baseline_priced[n]["cost"] - priced[n]["cost"], 2)}
                        for n in priced},
                }
                if cap > 0.0:
                    sim = simulate_battery_night(
                        imp, pool, day_surplus, hour_period, cap, efficiency,
                        max_charge, max_discharge, max_daily_cycles)
                    gi_night = sum_present(sim["grid_in_kwh"])
                    red_kwh = gi - gi_night
                    red_pct = red_kwh / gi * 100 if gi else None
                    grid_drawn = sum_present(sim["grid_draw_added_kwh"])
                    auto_bat = auto + sim["total_discharged_kwh"]
                    auto_pct = (auto_bat / solar * 100 if solar else None)
                    auto_pct_no_bat = (auto / solar * 100 if solar else None)
                    grid_reduction_hour = [
                        scaled["grid_in_reduction_kwh"][i]
                        + (imp[i] - sim["grid_in_kwh"][i])
                        for i in range(n_hours)]
                    regs_night = register_reduction(
                        base_regs, grid_reduction_hour, hour_reg, hour_present)
                    added: dict[str, float] = {r: 0.0
                                               for r in vmlib.TEMPO_METRIC_KEYS}
                    for i, reg in enumerate(hour_reg):
                        if hour_present[i]:
                            added[reg] += sim["grid_draw_added_kwh"][i]
                    regs_night = {r: max(0.0, regs_night[r] + added[r])
                                  for r in vmlib.TEMPO_METRIC_KEYS}
                    priced_night, _ = price_all(tariffs, regs_night)
                    contracts = {}
                    for name, bcost in priced.items():
                        saving = bcost["cost"] - priced_night[name]["cost"]
                        contracts[name] = {
                            "cost": priced_night[name]["cost"],
                            "saving_vs_no_battery_eur": round(saving, 2),
                            "saving_vs_baseline_eur": round(
                                baseline_priced[name]["cost"]
                                - priced_night[name]["cost"], 2),
                        }
                    night.update({
                        "auto_consumption_pct": round(auto_pct, 2),
                        "auto_consumption_gain_pp": round(
                            auto_pct - auto_pct_no_bat, 2),
                        "grid_in_kwh": round(gi_night, 2),
                        "grid_in_reduction_kwh": round(red_kwh, 2),
                        "grid_in_reduction_pct": round(red_pct, 2)
                        if red_pct is not None else None,
                        "grid_charged_kwh": round(grid_drawn, 2),
                        "grid_charged_stored_kwh": round(
                            sim["total_grid_stored_kwh"], 2),
                        "total_discharged_kwh": round(
                            sim["total_discharged_kwh"], 2),
                        "total_charged_kwh": round(
                            sim["total_charged_kwh"], 2),
                        "cycles_used": round(sim["cycles_used"], 1),
                        "contracts": contracts,
                    })
                night_batteries.append(night)

        scaling_out.append({
            "k": k,
            "solar_total_kwh": round(solar, 2),
            "auto_consumed_kwh": round(auto, 2),
            "grid_in_kwh": round(gi, 2),
            "grid_out_kwh": round(go_pool, 2),
            "auto_consumption_pct": round(auto / solar * 100, 2) if solar else None,
            "grid_in_reduction_kwh": round(gi_reduction, 2),
            "grid_in_reduction_pct": round(gi_reduction_pct, 2) if gi_reduction_pct is not None else None,
            "tempo_kwh": {m: round(regs[m], 4) for m in vmlib.TEMPO_METRIC_KEYS},
            "tempo_total_kwh": round(sum(regs.values()), 4),
            "contracts": {n: {"cost": priced[n]["cost"],
                              "delta_vs_baseline_eur": round(
                                  priced[n]["cost"] - baseline_priced[n]["cost"], 2)}
                          for n in priced},
            "cheapest_contract": cheapest,
            "tempo_optimal": tempo_optimal,
            "batteries": batteries,
            "night_charge_batteries": night_batteries,
        })

    # ---- break-even scan: where does Tempo stop being the cheapest? ------
    be_factors = []
    for k in sorted({round(be_min + i * be_step, 6)
                     for i in range(int((be_max - be_min) / be_step) + 1)}):
        scaled = scale_arrays(record, k)
        regs = register_reduction(base_regs, scaled["grid_in_reduction_kwh"],
                                  hour_reg, hour_present)
        priced, cheapest = price_all(tariffs, regs)
        be_factors.append({
            "k": round(k, 3),
            "cheapest_contract": cheapest,
            "tempo_optimal": cheapest == "tempo",
            "tempo_cost_eur": round(priced["tempo"]["cost"], 2),
            "best_cost_eur": round(priced[cheapest]["cost"], 2),
        })
    first_non_tempo = next((r for r in be_factors
                            if not r["tempo_optimal"]), None)
    be_table_rows = [r for r in be_factors
                     if any(abs(r["k"] - f) < 1e-6 for f in be_table)]

    # ---- marginal battery sizing + recommendations ------------------------
    sizing_rows: list[dict] = []
    recommendations: dict[float, dict] = {}
    sweep_caps = [round(i * sizing_step, 4)
                  for i in range(int(round(sizing_max / sizing_step)) + 1)]

    for k in scale_factors:
        scaled = scale_arrays(record, k)
        imp = list(scaled["grid_in_no_bat_kwh"])
        pool = list(scaled["export_pool_kwh"])
        regs_no_bat = register_reduction(
            base_regs, scaled["grid_in_reduction_kwh"], hour_reg, hour_present)
        priced_no_bat, cheapest_no_bat = price_all(tariffs, regs_no_bat)
        ref = (cheapest_no_bat if ref_contract == "best" else ref_contract)
        if ref not in tariffs:
            print(f"error: marginal_reference_contract '{ref}' is not a "
                  f"configured tariff")
            return 1
        ref_base_cost = priced_no_bat[ref]["cost"]

        gi_no_bat_kwh = sum_present(imp)
        savings: dict[float, dict] = {}
        prev_cost = None
        for cap in sweep_caps:
            if cap == 0.0:
                cost = ref_base_cost
                red_kwh = 0.0
                discharged = 0.0
            else:
                sim = simulate_battery(imp, pool, cap, efficiency,
                                       max_charge, max_discharge,
                                       max_daily_cycles)
                gi_bat = sum_present(sim["grid_in_kwh"])
                red_kwh = gi_no_bat_kwh - gi_bat
                regs_bat = register_reduction(
                    base_regs,
                    [scaled["grid_in_reduction_kwh"][i]
                     + (imp[i] - sim["grid_in_kwh"][i])
                     for i in range(n_hours)],
                    hour_reg, hour_present)
                cost = tariffs[ref].cost(regs_bat)
                discharged = sim["total_discharged_kwh"]
            saving = ref_base_cost - cost
            marginal = None
            if cap > 0.0 and prev_cost is not None:
                marginal = (prev_cost - cost) / sizing_step
            savings[cap] = {
                "cost_eur": round(cost, 2),
                "saving_eur": round(saving, 2),
                "grid_in_reduction_kwh": round(red_kwh, 2),
                "discharged_kwh": round(discharged, 2),
                "marginal_eur_per_kwh": (round(marginal, 4)
                                         if marginal is not None else None),
            }
            sizing_rows.append({
                "k": k, "capacity_kwh": cap,
                "grid_in_reduction_kwh": round(red_kwh, 2),
                "discharged_kwh": round(discharged, 2),
                "cost_eur_reference": round(cost, 2),
                "saving_eur": round(saving, 2),
                "marginal_eur_per_kwh": (round(marginal, 4)
                                         if marginal is not None else ""),
            })
            prev_cost = cost

        rec = None
        for cap in sweep_caps:
            if cap <= 0.0:
                continue
            marg = savings[cap]["marginal_eur_per_kwh"]
            if marg is None:
                continue
            if marg < threshold:
                break
            rec = cap
        if rec is None:
            rec = sweep_caps[-1]
        recommendations[k] = {
            "capacity_kwh": rec,
            "reference_contract": ref,
            "marginal_saving_eur_per_kwh":
                savings[rec]["marginal_eur_per_kwh"],
            "saving_eur": savings[rec]["saving_eur"],
            "grid_in_reduction_kwh": savings[rec]["grid_in_reduction_kwh"],
            "threshold_eur_per_kwh": threshold,
            "note": (("at upper bound of the sizing sweep" if rec >= sizing_max
                      else "")),
        }

    # ---- console report -----------------------------------------------------
    print(f"\nSolar scaling (displacement model, re-priced under the "
          f"{len(tariffs)} contracts):")
    hdr = f"{'k':>4} {'solar':>9} {'auto%':>7} {'gridIn':>9} {'red':>7} " \
          f"{'cheapest':>12} {'tempo EUR':>9} {'tempo ok':>7}"
    print(hdr)
    for s in scaling_out:
        print(f"{s['k']:>4.1f} {s['solar_total_kwh']:9.2f} "
              f"{s['auto_consumption_pct']:>7.1f} {s['grid_in_kwh']:9.2f} "
              f"{s['grid_in_reduction_kwh']:7.1f} "
              f"{s['cheapest_contract']:>12} "
              f"{s['contracts']['tempo']['cost']:9.2f} "
              f"{'yes' if s['tempo_optimal'] else 'NO':>7}")
        if s["batteries"]:
            for b in s["batteries"]:
                tempo_saving = b["contracts"]["tempo"]["saving_vs_baseline_eur"]
                print(f"  battery {b['capacity_kwh']:>5.1f} kWh: "
                      f"auto +{b['auto_consumption_gain_pp']:5.1f}pp "
                      f"gridIn -{b['grid_in_reduction_kwh']:7.1f} kWh "
                      f"({b['grid_in_reduction_pct']:.1f} %) "
                      f"tempo saving {tempo_saving:7.2f} EUR "
                      f"({b['cycles_used']:.1f} cycles)")
        if s["night_charge_batteries"]:
            print("  night grid charge (+Tempo HC arbitrage):")
            for b in s["night_charge_batteries"]:
                tempo_saving = b["contracts"]["tempo"]["saving_vs_baseline_eur"]
                print(f"    battery {b['capacity_kwh']:>5.1f} kWh: "
                      f"grid bought {b['grid_charged_kwh']:7.1f} kWh, "
                      f"gridIn -{b['grid_in_reduction_kwh']:7.1f} kWh, "
                      f"tempo saving {tempo_saving:7.2f} EUR "
                      f"({b['cycles_used']:.1f} cycles)")

    print(f"\nBreak-even (Tempo stops being optimal):")
    print(f"{'k':>5} {'cheapest':>13} {'tempo EUR':>9} {'best EUR':>9}")
    for r in be_table_rows:
        print(f"{r['k']:>5.2f} {r['cheapest_contract']:>13} "
              f"{r['tempo_cost_eur']:9.2f} {r['best_cost_eur']:9.2f}")
    if first_non_tempo is not None:
        print(f"Tempo still the cheapest up to k = "
              f"{be_factors[be_factors.index(first_non_tempo) - 1]['k'] if be_factors.index(first_non_tempo) > 0 else 1.0}"
              f"; first k where it stops: "
              f"{first_non_tempo['k']} ({first_non_tempo['cheapest_contract']})")
    else:
        print(f"Tempo remains the cheapest contract up to k = {be_max}.")

    if capacities:
        print(f"\nRecommended battery capacity (marginal saving >= "
              f"{threshold * 100:.0f} cEUR/kWh-year, reference "
              f"{'cheapest contract' if ref_contract == 'best' else ref}):")
        print(f"{'k':>4} {'cap kWh':>8} {'final marg':>10} "
              f"{'saving EUR':>10} {'gridIn red':>10}")
        for k in scale_factors:
            r = recommendations[k]
            print(f"{k:>4.1f} {r['capacity_kwh']:>8.1f} "
                  f"{r['marginal_saving_eur_per_kwh']:>10.4f} "
                  f"{r['saving_eur']:>10.2f} {r['grid_in_reduction_kwh']:>10.2f}"
                  + (f"   {r['note']}" if r["note"] else ""))

    # ---- write outputs ------------------------------------------------------
    result = {
        "window": {"start": start, "end": end, "hours": n_hours},
        "source": args.input,
        "baseline": {
            **{k1: round(v, 4) if isinstance(v, float) else v
               for k1, v in base.items() if k1 != "tempo_kwh"},
            "tempo_kwh": base["tempo_kwh"],
        },
        "contracts": {n: {"cost": baseline_priced[n]["cost"]}
                      for n in baseline_priced},
        "cheapest_contract": baseline_cheapest,
        "solar_scenarios": scaling_out,
        "break_even": {
            "k_min": be_min, "k_max": be_max, "k_step": be_step,
            "scan": be_factors,
            "tempo_stops_optimal_at_k": (first_non_tempo["k"]
                                         if first_non_tempo else None),
        },
        "battery_config": {
            "capacities_kwh": [round(c, 2) for c in capacities],
            "round_trip_efficiency": efficiency,
            "max_charge_power_kw": max_charge,
            "max_discharge_power_kw": max_discharge,
            "max_daily_cycles": max_daily_cycles,
        },
        "night_grid_charge": {
            "enabled": night_enabled,
            "capacities_kwh": [round(c, 2) for c in night_capacities],
        },
        "marginal_saving_threshold_eur_per_kwh": threshold,
        "marginal_reference_contract": ref_contract,
        "recommendations": {str(k): recommendations[k] for k in scale_factors},
    }
    vmlib.write_json(args.output, result)

    with open(args.sizing_output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["k", "capacity_kwh", "grid_in_reduction_kwh",
                    "discharged_kwh", "cost_eur_reference", "saving_eur",
                    "marginal_eur_per_kwh"])
        for row in sizing_rows:
            w.writerow([f"{row['k']:.2f}", f"{row['capacity_kwh']:.2f}",
                        row["grid_in_reduction_kwh"],
                        row["discharged_kwh"], row["cost_eur_reference"],
                        row["saving_eur"], row["marginal_eur_per_kwh"]])

    print(f"\nWritten: {args.output}")
    print(f"Written: {args.sizing_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())