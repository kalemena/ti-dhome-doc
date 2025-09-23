#!/usr/bin/env python3
"""
Phase 4 - contract comparison: "Is Tempo the good contract?".

Reads the six HP/HC Tempo registers of the Phase 2 canonical record
(`hourly-energy.csv`, columns tempo_{blue,white,red}_{hp,hc}) and prices the
yearly consumption under every contract of energy-config.yaml with the exact
readme.adoc formulas:

  * base          flat rate x total energy                      (kWhBase)
  * blue          hc/hp split across all colors                 (kWhHC/HP)
  * tempo         per color x period rates                      (kWhTBHC...)
  * zenfix        flat rate x total energy                      (kWhZenFix)
  * zenfix-tempo  hc/hp split across all colors                 (kWhZenFixHC/HP)

The HP/HC split is the physical one carried by the six teleinfo registers
(summed yearly over their available hours), so no hour-window reconstruction
is needed and the result matches the meter exactly.

Costs use vmlib.Tariff (rates live in energy-config.yaml, not in this script).
Each register's yearly increase is the sum over its present hours; missing
hours are reported, never interpolated.

Output: scripts/output/contracts.json + compact table (contract, cost,
delta vs Tempo). `delta_vs_tempo` = contract cost - Tempo cost, and
`tempo_vs_base` = absolute and relative reduction of Tempo vs Base.

Usage:
    03-contract-costs.py [--config energy-config.yaml]
                         [--input scripts/output/hourly-energy.csv]
                         [--output scripts/output/contracts.json]
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmlib


def yearly_tempo(record: dict[str, list]) -> tuple[dict[str, float], dict[str, int]]:
    """Yearly kWh of the six teleinfo registers (sum over present hours)."""
    kwh: dict[str, float] = {}
    missing: dict[str, int] = {}
    for metric in vmlib.TEMPO_METRIC_KEYS:
        col = record[metric]
        present = [v for v in col if not math.isnan(v)]
        kwh[metric] = sum(present)
        missing[metric] = len(col) - len(present)
    return kwh, missing


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Yearly cost under each contract / readme formulas")
    ap.add_argument("--config", default="scripts/energy-config.yaml")
    ap.add_argument("--input", default="scripts/output/hourly-energy.csv",
                    help="Phase 2 canonical record (hourly-energy.csv)")
    ap.add_argument("--output", default="scripts/output/contracts.json")
    args = ap.parse_args()

    cfg = vmlib.load_config(args.config)

    if not os.path.exists(args.input):
        print(f"error: {args.input} not found - run `make export` first")
        return 1

    record = vmlib.load_hourly_energy(args.input)
    tempo_cols = list(vmlib.TEMPO_METRIC_KEYS)
    missing_cols = [c for c in tempo_cols if c not in record]
    if missing_cols:
        print(f"error: {args.input} lacks the six HP/HC tempo registers "
              f"{missing_cols} - re-run `make export` (Phase 2 emits them)")
        return 1

    tempo_kwh, missing_hours = yearly_tempo(record)
    tempo_total = sum(tempo_kwh.values())
    tariffs = vmlib.load_tariffs(cfg)

    costs = {name: t.cost(tempo_kwh) for name, t in tariffs.items()}
    tempo_cost = costs.get("tempo")
    base_cost = costs.get("base")

    contracts: dict[str, dict] = {}
    for name, t in tariffs.items():
        total_kwh = t.total_kwh(tempo_kwh)
        contracts[name] = {
            "description": t.description,
            "kind": t.kind,
            "cost": round(costs[name], 2),
            "total_kwh": round(total_kwh, 2),
            "price_per_kwh": round(costs[name] / total_kwh, 4)
            if total_kwh else None,
            "delta_vs_tempo_eur": (round(costs[name] - tempo_cost, 2)
                                   if tempo_cost is not None else None),
            "delta_vs_tempo_pct": (round((costs[name] - tempo_cost)
                                         / tempo_cost * 100, 2)
                                   if tempo_cost else None),
        }

    ranking = sorted(costs, key=lambda n: costs[n])

    tempo_vs_base = None
    if base_cost is not None and tempo_cost is not None:
        tempo_vs_base = {
            "absolute_reduction_eur": round(base_cost - tempo_cost, 2),
            "percent_reduction": round((base_cost - tempo_cost)
                                       / base_cost * 100, 2),
        }

    n_hours = len(record["utc_hour"])
    result = {
        "window": {
            "start": record["utc_hour"][0] if n_hours else None,
            "end": record["utc_hour"][-1] if n_hours else None,
        },
        "source": args.input,
        "tempo_kwh": {m: round(v, 4) for m, v in tempo_kwh.items()},
        "tempo_total_kwh": round(tempo_total, 4),
        "tempo_missing_hours": missing_hours,
        "contracts": contracts,
        "ranking": ranking,
        "tempo_vs_base": tempo_vs_base,
    }

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    vmlib.write_json(args.output, result)

    print(f"Source: {args.input} (offline, no DB query)")
    print(f"Tempo registers (yearly kWh): "
          f"{ {m: round(v, 2) for m, v in tempo_kwh.items()} }")
    print(f"Total: {tempo_total:.2f} kWh (missing hours: "
          f"{ {m: h for m, h in missing_hours.items()} })\n")

    print(f"{'contract':14} {'cost EUR':>10} {'delta vs Tempo':>16}")
    for name in ranking:
        c = contracts[name]
        delta = c["delta_vs_tempo_eur"]
        delta_s = "  -  " if delta is None else f"{delta:10.2f} EUR"
        print(f"{name:14} {c['cost']:10.2f} {delta_s:>16}")
    if tempo_vs_base:
        print(f"\nTempo vs Base: -{tempo_vs_base['absolute_reduction_eur']:.2f} EUR "
              f"(-{tempo_vs_base['percent_reduction']:.2f} %)")
    print(f"\nCheapest -> most expensive: {' < '.join(ranking)}")

    print(f"\nWritten: {args.output}")

    # Surface a ranking that contradicts the readme expectation loudly.
    expected = ["tempo", "zenfix", "zenfix-tempo", "blue", "base"]
    if ranking != expected:
        print(f"note: ranking {ranking} differs from the readme expectation "
              f"{expected} - review the rates/window before trusting it.")

    return 0


if __name__ == "__main__":
    sys.exit(main())