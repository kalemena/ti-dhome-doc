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