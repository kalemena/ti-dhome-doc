# Solar × Battery Scenarios — Summary & ROI

Source: `scripts/06-solar-scenarios.py` over the window 2025-09-01 → 2026-09-01
(hourly record `scripts/output/hourly-energy.csv`). Scaled solar `k` assumes
new panels with the same diurnal shape. Savings are re-priced under the 5
contracts; **Tempo is the cheapest contract in every combination** below.

## Assumptions

- Battery priced linearly: **250 €/kWh** (4 kWh = 1 000 €, 8 kWh = 2 000 €,
  16 kWh = 4 000 €).
- Savings shown = incremental vs *no battery at the same k*, under Tempo.
- Grid-draw displacement model anchored on the measured meter; at `k=1` the
  model reproduces the baseline exactly (grid in 3 344.7 kWh, Tempo 557.58 €).
- Battery charges only from would-be exported solar (no grid-charging
  arbitrage), 90 % round-trip efficiency, 3.5 kW charge/discharge cap.

## Summary (Tempo cost + grid-in reduction)

| k | Solar | Battery | Auto-consumed | Grid In − | Tempo cost | Saving vs no-battery |
|---|-------|---------|---------------|-----------|-----------|------|
| 1 | 1013 kWh | 0 | 77.0 % | 0 | 557.58 € | — |
| | | 4 | 97.7 % | 209.7 kWh | 520.74 € | 36.84 € |
| | | 8 | 98.1 % | 213.6 kWh | 520.17 € | 37.41 € |
| | | 16 | 98.1 % | 213.6 kWh | 520.17 € | 37.41 € |
| 2 | 2027 kWh | 0 | 60.0 % | 436.5 kWh | 482.60 € | — |
| | | 4 | 89.8 % | 602.3 kWh | 384.46 € | 98.14 € |
| | | 8 | 92.7 % | 662.2 kWh | 374.58 € | 108.02 € |
| | | 16 | 93.9 % | 686.8 kWh | 371.42 € | 111.18 € |
| 3 | 3041 kWh | 0 | 46.0 % | 618.5 kWh | 450.84 € | — |
| | | 4 | 73.2 % | 827.2 kWh | 317.35 € | 133.49 € |
| | | 8 | 78.9 % | 1 001.5 kWh | 290.59 € | 160.25 € |
| | | 16 | 83.0 % | 1 125.0 kWh | 273.43 € | 177.41 € |

## ROI (simple payback and yearly return)

| k | 4 kWh (1 000 €) | 8 kWh (2 000 €) | 16 kWh (4 000 €) |
|---|-----------------|-----------------|------------------|
| 1 | 27.1 yr · 3.7 % | 53.5 yr · 1.9 % | 106.9 yr · 0.9 % |
| 2 | 10.2 yr · 9.8 % | 18.5 yr · 5.4 % | 36.0 yr · 2.8 % |
| 3 | **7.5 yr · 13.3 %** | 12.5 yr · 8.0 % | 22.5 yr · 4.4 % |

## ROI comment

- **Today (`k=1`), the battery never pays back.** It captures a hard cap of
  ~237 kWh/yr of export, so a 4 kWh unit already saturates (~37 €/yr) and
  16 kWh earns nothing extra. 27–107 yr payback is far beyond any battery
  lifetime.
- **Even at `k=3`, only the smallest battery is borderline.** 4 kWh at triple
  solar gives 13.3 %/yr (≈7.5 yr payback) — the only combo inside a typical
  10–15 yr battery life. Every step up wastes money: 8 kWh returns 8 %/yr,
  16 kWh only 4.4 %/yr, because each added kWh both costs the same 250 € and
  earns *less* per year.
- **Sizing rule from the numbers:** if you buy a battery, buy the *minimum*
  size that captures the daily surplus (4 kWh at this site), never the big
  one. Diminishing returns dominate.
- **Caveats:** this is an upper bound — no degradation, no grid-charging
  arbitrage, and the savings ride on top of the (battery-free) solar-scaling
  gain. The battery materially helps self-consumption, but as an *investment*
  it only approaches reasonableness at `k=3` with a 4 kWh unit.

## Night grid-charging scenario (Tempo HC arbitrage)

A second model lets the battery also buy from the grid **at night**, at the
Tempo **Low-cost (HC)** rate of the night's color, to cover the next day's
**High-cost (HP)** draw. The rule keeps paid charging subordinate to free sun
(`night_grid_charge` in `energy-config.yaml`; implementation
`simulate_battery_night` in `06-solar-scenarios.py`):

- Per UTC day, solar surplus is the *first* fill: the battery charges from
  the would-be export pool exactly as before.
- Grid charging only runs in the pre-dawn HC window (hours 0–5 UTC) and is
  capped per day at `capacity − day_surplus`: it **buys only the capacity the
  sun won't fill for free**, never charge-for-then-export kWh.
- Charging is billed at the night's Tempo color × HC (~0.13–0.15 €/kWh);
  **discharge serves HP hours only**, so stored energy displaces the costliest
  power (HPs are ~58 % of the Tempo-metered draw).

### Tempo cost vs solar-only battery, same k

| k | Battery | Night cost | Δ vs solar-only | Grid bought | Grid In − |
|---|---------|-----------|-----------------|-------------|-----------|
| 1 | 4 | **489.89 €** | −30.9 € | 1 123.8 kWh | 1 145.2 kWh |
| | 8 | **478.46 €** | −41.7 € | 1 584.4 kWh | 1 533.4 kWh |
| | 16 | **479.25 €** | −40.9 € | 1 659.8 kWh | 1 590.4 kWh |
| 2 | 4 | **384.30 €** | −0.2 € | 748.4 kWh | 1 041.0 kWh |
| | 8 | 400.71 € | **+26.1 €** | 1 302.0 kWh | 1 341.6 kWh |
| | 16 | 408.11 € | **+36.7 €** | 1 399.3 kWh | 1 378.2 kWh |
| 3 | 4 | 334.60 € | **+17.3 €** | 514.3 kWh | 962.7 kWh |
| | 8 | 345.24 € | **+54.7 €** | 956.5 kWh | 1 220.9 kWh |
| | 16 | 377.47 € | **+104.0 €** | 1 211.5 kWh | 1 233.2 kWh |

Columns: Tempo cost with night charging; Δ vs the solar-only battery at the
same k (`+` = night charging costs *more*); grid energy actually bought (paid
as drawn, 90 % stored); grid-in reduction vs the k-level no-battery case.

### Reading

- **Night charging shines only when solar is scarce.** At `k=1` it targets the
  site's real weakness — nearly 2 000 kWh/yr of HP imports at blue/white/red
  HP prices — and doubles the battery's value (4 kWh: 36.84 → 67.69 €/yr).
  The 8 kWh points get ~parity (79.12 € @ 8 vs 78.33 € @ 16): saturation again.
- **With enough solar it backfires.** At `k=2` and `k=3` the two sources
  compete for the same tank: a battery already filled at night wastes the
  day's free sunlight (exported at ~0 €/kWh feed-in) while the HC bill piles
  up. 16 kWh at `k=3` is ~104 €/yr *worse* than the same battery solar-only.
- **Verdict:** the clever win is *small-solar + small battery, night
  arbitrage*. It never resurrects the ROI story from the table above — a 4 kWh
  unit at `k=1` rises from 27.1 to 14.8 yr payback (1 000 € / 67.69 €), still
  past battery lifetime, and any bigger battery only erodes that.
- **Model caveats:** discharge remains HP-priority (a design choice, not an
  optimum — the same model could equally trade HC-in for HC-out on blue days,
  where the margin is ~1 c€/kWh and barely worth a battery cycle). The
  `auto-consumed %` figure is not reported for this variant because grid-stored
  kWh count as on-site consumption and push it past 100 %.