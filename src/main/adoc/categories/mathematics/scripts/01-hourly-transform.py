#!/usr/bin/env python3
"""
Hourly transform - the "as if" filters of the hourly record.

Reads the canonical hourly record (`hourly-energy.csv`, built by
`01-hourly-export.py`) offline and rebuilds it under two consumption
filters, so the house can be modelled *as if* one of its high consumers had
behaved differently. Both filters act on the hourly increments only, and
each one keeps the record self-consistent: what leaves an hour is either
re-created in another hour or disappears from the house, and Grid In and
the six Tempo registers always move together, so the Grid In ~ Sum Tempo
identity survives the transform.

Filters (each behind its own parameter, both enabled by default):

  * `--filter-car` / `--no-filter-car`
    Review the hourly increments of the car counter (`electric_car_kwh`) and
    remove them from Grid In and from the Tempo registers of the same hour,
    *as if the car had never been charged* and therefore had no impact on
    anything else. The energy leaves the house: the yearly Grid In and Sum
    Tempo both drop by exactly the removed kWh, no hour is re-dated, and
    the solar / export / auto-consumed columns are untouched (the car is
    never modelled as solar-borne here - it is billed on the house meter).

  * `--filter-water` / `--no-filter-water`
    Review the hourly increments of the water heater counter
    (`water_heater_kwh`). The energy burnt outside the solar day light
    (default 19h00-23h59 and 00h00-05h59 UTC) is moved *earlier*, into the
    day window of the same UTC day (default 11h00-18h59), as if the same
    hot water had been produced earlier. The move is propagated to Grid In
    and to the Tempo registers of the destination hours, so night
    consumption drops, day consumption grows and the daily total stays
    identical: only the register mix (High/Low cost x Tempo color) of each
    hour changes. The moved kWh is *not* modelled as solar-borne - the
    point of the filter is the HP/HC shift, not a solar offset (Phase 7
    covers the solar displacement).

Method:
  * the hourly increments are read as-is (the record already holds the
    hourly deltas of the six Tempo registers, Grid In / Out and the solar
    total); the three per-color Tempo sums are re-computed from the six
    transformed registers;
  * a filter can only move kWh an hour can prove: the removal of an hour is
    capped by the hour's Grid In and by the Tempo energy that hour actually
    registered, and a moved kWh is only re-created in a day hour that has
    both Grid In and Tempo data. Unmovable kWh stays where it is and is
    reported as such (nothing is interpolated, nothing is invented);
  * the share of an hour is split between the six registers proportionally
    to the registers that accrued that hour, so a removal or an addition
    keeps the hour's own Blue/White/Red x HP/HC mix;
  * the water move distributes the day's night energy over the day window
    proportionally to the day's Grid In there (a water heater runs on top of
    an existing draw), with an equal split as fallback for a day with no
    daytime draw at all;
  * the two filters compose in a fixed order: the car first (as if it had
    never existed), then the water move (on the car-free house).

Outputs (the JSON and the HTML share the basename of `--output`):
  * `hourly-transform.csv`  the transformed record (8 760 rows): solar, grid
    in/out, the six Tempo registers plus their three per-color sums, the
    consumer columns, the filter columns (`car_removed_kwh`,
    `water_moved_from_night_kwh`, `water_moved_to_day_kwh`) and the
    derived `auto_consumed_kwh` / `home_consumption_kwh`. The column names
    of `hourly-energy.csv` are kept, so phases 3..7 can read it as well; the
    cumulative `{metric}_counter_kwh` columns are not republished, since no
    physical counter is modified by a filter;
  * `hourly-transform.json`  the structured result: totals measured vs
    transformed, monthly and daily aggregates, per-filter accounting and
    the contract costs of both;
  * `hourly-transform.html`  the report: the new expected totals (Grid In,
    Tempo), the monthly Grid In / Tempo bars, the monthly and daily
    solar / grid / export / auto-consumed timelines and the per-day detail.

Usage:
    01-hourly-transform.py [--config scripts/energy-config.yaml]
                           [--input scripts/output/hourly-energy.csv]
                           [--output scripts/output/hourly-transform.csv]
                           [--no-filter-car] [--no-filter-water]
"""

import argparse
import csv
import datetime
import html
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmlib

COLORS = ["blue", "white", "red"]
TEMPO_KEYS = list(vmlib.TEMPO_METRIC_KEYS)          # the six registers
COLOR_SUM_COLUMNS = [f"tempo_{c}_hp_hc" for c in COLORS]

# Windows of the water filter, in UTC hours: the night hours are the ones
# outside the solar day light, the day hours are where that energy is moved
# to (always earlier in absolute time: 00h-05h -> 11h-18h and 19h-23h ->
# 11h-18h of the same UTC day).
DEFAULT_NIGHT_HOURS = [[19, 24], [0, 6]]
DEFAULT_DAY_HOURS = [[11, 19]]

REQUIRED_COLUMNS = (["utc_hour", "solar_total_kwh", "grid_in_kwh",
                     "grid_out_kwh"] + TEMPO_KEYS
                    + ["electric_car_kwh", "water_heater_kwh"])

CSV_COLUMNS = (["utc_hour", "solar_total_kwh", "grid_in_kwh", "grid_out_kwh"]
               + [f"tempo_{c}_{p}" for c in COLORS for p in ("hp", "hc")]
               + COLOR_SUM_COLUMNS
               + ["electric_car_kwh", "water_heater_kwh", "heaters_kwh",
                  "car_removed_kwh", "water_moved_from_night_kwh",
                  "water_moved_to_day_kwh", "auto_consumed_kwh",
                  "home_consumption_kwh"])

METRIC_LABELS = [
    ("grid_in_kwh", "Grid In"),
    ("tempo_total_kwh", "Sum Tempo"),
    ("tempo_hp_kwh", "Tempo HP"),
    ("tempo_hc_kwh", "Tempo HC"),
    ("tempo_blue_kwh", "Tempo Blue"),
    ("tempo_white_kwh", "Tempo White"),
    ("tempo_red_kwh", "Tempo Red"),
    ("solar_total_kwh", "Solar AC"),
    ("grid_out_kwh", "Grid Out (export)"),
    ("auto_consumed_kwh", "Auto-consumed"),
    ("home_consumption_kwh", "Home consumption"),
    ("electric_car_kwh", "Car"),
    ("water_heater_kwh", "Water heater"),
    ("heaters_kwh", "Heaters"),
]


# --------------------------------------------------------------------------
# NaN-safe helpers and hour windows
# --------------------------------------------------------------------------

def _present(value) -> bool:
    return value is not None and not math.isnan(value)


def _value(value, default: float = 0.0) -> float:
    return value if _present(value) else default


def _sum(values) -> float:
    return sum(v for v in values if _present(v))


def _fmt(value, digits: int = 2) -> str:
    return "nan" if not _present(value) else f"{value:.{digits}f}"


def hour_of(label: str) -> int:
    return int(label[11:13])


def norm_window(window, default) -> list[list[int]]:
    """Normalize a config window to a list of [start, end) UTC hour ranges."""
    if not window:
        return [list(w) for w in default]
    if isinstance(window[0], (int, float)):
        return [[int(window[0]), int(window[1])]]
    return [[int(w[0]), int(w[1])] for w in window]


def in_window(hour: int, windows: list[list[int]]) -> bool:
    return any(s <= hour < e for s, e in windows)


def window_text(windows: list[list[int]]) -> str:
    return " + ".join(f"{s:02d}h-{e - 1:02d}h" if e <= 24 else f"{s:02d}h-24h"
                      for s, e in windows)


def day_rows(record: dict[str, list]) -> dict[str, list[int]]:
    """Row indexes grouped by UTC day (the record is hour ordered)."""
    days: dict[str, list[int]] = {}
    for i, label in enumerate(record["utc_hour"]):
        days.setdefault(label[:10], []).append(i)
    return days


# --------------------------------------------------------------------------
# Tempo register bookkeeping
# --------------------------------------------------------------------------

def register_split(tempo: dict[str, list], i: int
                   ) -> tuple[float, list[tuple[str, float]]]:
    """(energy registered at hour i, share of each register at that hour).

    The share is proportional to the registers that actually accrued the
    hour, i.e. the hour's own Blue/White/Red x HP/HC mix. The split is empty
    when the hour registered nothing (silent teleinfo stream).
    """
    values = [(k, tempo[k][i]) for k in TEMPO_KEYS
              if _present(tempo[k][i]) and tempo[k][i] > 0]
    total = sum(v for _, v in values)
    if total <= 0:
        return 0.0, []
    return total, [(k, v / total) for k, v in values]


# --------------------------------------------------------------------------
# Filter 1 - the car removed from the house
# --------------------------------------------------------------------------

def filter_car(record: dict[str, list], grid: list[float],
               tempo: dict[str, list]) -> tuple[list[float], list[float], dict]:
    """Deduct the car energy from Grid In and from the Tempo registers.

    `grid` and `tempo` are updated in place. The hourly removal is capped by
    the hour's Grid In and by the Tempo energy that hour registered (a
    counter that did not report cannot have its energy moved), and the
    removed kWh is split between the registers of that hour, so Grid In and
    Tempo keep the same relative order.

    Returns (remaining car column, removed kWh per hour, accounting).
    """
    n = len(record["utc_hour"])
    car = record["electric_car_kwh"]
    kept = list(car)
    removed = [0.0] * n
    by_register = {k: 0.0 for k in TEMPO_KEYS}
    hours_with_car = 0
    not_removed = 0.0

    for i in range(n):
        c = car[i]
        if not _present(c) or c <= 0:
            continue
        hours_with_car += 1
        registered, split = register_split(tempo, i)
        d = min(c, _value(grid[i]), registered)
        if d <= 0:
            not_removed += c
            continue
        grid[i] = grid[i] - d
        for k, share in split:
            tempo[k][i] -= d * share
            by_register[k] += d * share
        removed[i] = d
        kept[i] = c - d

    return kept, removed, {
        "car_total_kwh": round(_sum(car), 2),
        "removed_kwh": round(sum(removed), 2),
        "left_in_place_kwh": round(not_removed, 2),
        "hours_with_car": hours_with_car,
        "hours_removed": sum(1 for v in removed if v > 0),
        "removed_by_tempo_register_kwh":
            {k: round(v, 2) for k, v in by_register.items()},
    }


def car_report_off(record: dict[str, list]) -> dict:
    """The same accounting shape, for `--no-filter-car`."""
    return {
        "car_total_kwh": round(_sum(record["electric_car_kwh"]), 2),
        "removed_kwh": 0.0,
        "left_in_place_kwh": 0.0,
        "hours_with_car": sum(1 for v in record["electric_car_kwh"]
                              if _present(v) and v > 0),
        "hours_removed": 0,
        "removed_by_tempo_register_kwh": {k: 0.0 for k in TEMPO_KEYS},
    }


# --------------------------------------------------------------------------
# Filter 2 - the water heater night energy moved to the day window
# --------------------------------------------------------------------------

def filter_water(record: dict[str, list], grid: list[float],
                 tempo: dict[str, list], night_hours: list[list[int]],
                 day_hours: list[list[int]], distribution: str
                 ) -> tuple[list[float], list[float], list[float], dict]:
    """Move the water energy burnt at night into the day's day window.

    `grid` and `tempo` are updated in place. Per day, the night energy is
    removed from Grid In and from the Tempo registers of the hours that
    registered it, then re-created over the day window proportionally to
    the day's Grid In there (equal split when the day has no daytime draw).
    The daily total is therefore unchanged: only the hour of consumption and
    the HP/HC mix move.

    Returns (water column after the move, kWh removed from night, kWh added
    in the day window, accounting).
    """
    n = len(record["utc_hour"])
    water = record["water_heater_kwh"]
    kept = list(water)
    from_night = [0.0] * n
    to_day = [0.0] * n
    by_register = {k: 0.0 for k in TEMPO_KEYS}
    night_total = 0.0
    days_moved = 0
    days_with_night = 0
    days_equal_split = 0
    days_without_destination: list[str] = []

    for day, rows in day_rows(record).items():
        night_rows = [i for i in rows
                      if in_window(hour_of(record["utc_hour"][i]), night_hours)
                      and _present(water[i]) and water[i] > 0]
        if not night_rows:
            continue
        days_with_night += 1
        night_total += _sum(water[i] for i in night_rows)

        # Destination hours of the same day: only hours that recorded both
        # Grid In and Tempo energy can re-create the moved kWh.
        dest = [i for i in rows
                if in_window(hour_of(record["utc_hour"][i]), day_hours)
                and _present(grid[i]) and register_split(tempo, i)[0] > 0]
        if not dest:
            days_without_destination.append(day)
            continue

        pending = 0.0
        for i in night_rows:
            _, split = register_split(tempo, i)
            d = min(water[i], _value(grid[i]),
                    sum(tempo[k][i] for k, _ in split))
            if d <= 0:
                continue
            grid[i] = grid[i] - d
            for k, share in split:
                tempo[k][i] -= d * share
                by_register[k] -= d * share
            from_night[i] = d
            kept[i] -= d
            pending += d
        if pending <= 0:
            continue
        days_moved += 1

        if distribution == "equal":
            weights = [1.0 / len(dest)] * len(dest)
        else:
            base = [grid[i] for i in dest]
            total_base = sum(base)
            if total_base > 0:
                weights = [v / total_base for v in base]
            else:
                weights = [1.0 / len(dest)] * len(dest)
                days_equal_split += 1

        for i, w in zip(dest, weights):
            share = pending * w
            grid[i] = grid[i] + share
            _, split = register_split(tempo, i)
            for k, reg_share in split:
                tempo[k][i] += share * reg_share
                by_register[k] += share * reg_share
            to_day[i] += share
            kept[i] += share

    moved = sum(from_night)
    return kept, from_night, to_day, {
        "night_hours_utc": night_hours,
        "day_hours_utc": day_hours,
        "distribution": distribution,
        "water_total_kwh": round(_sum(water), 2),
        "water_night_kwh": round(night_total, 2),
        "moved_kwh": round(moved, 2),
        "left_at_night_kwh": round(night_total - moved, 2),
        "grid_in_night_kwh": round(-moved, 2),
        "grid_in_day_kwh": round(sum(to_day), 2),
        "days_with_night_water": days_with_night,
        "days_moved": days_moved,
        "days_equal_split": days_equal_split,
        "days_without_destination": days_without_destination,
        "moved_by_tempo_register_kwh":
            {k: round(v, 2) for k, v in by_register.items()},
    }


def water_report_off(record: dict[str, list], night_hours: list[list[int]],
                     day_hours: list[list[int]], distribution: str) -> dict:
    """The same accounting shape, for `--no-filter-water`."""
    return {
        "night_hours_utc": night_hours,
        "day_hours_utc": day_hours,
        "distribution": distribution,
        "water_total_kwh": round(_sum(record["water_heater_kwh"]), 2),
        "water_night_kwh": 0.0,
        "moved_kwh": 0.0,
        "left_at_night_kwh": 0.0,
        "grid_in_night_kwh": 0.0,
        "grid_in_day_kwh": 0.0,
        "days_with_night_water": 0,
        "days_moved": 0,
        "days_equal_split": 0,
        "days_without_destination": [],
        "moved_by_tempo_register_kwh": {k: 0.0 for k in TEMPO_KEYS},
    }


# --------------------------------------------------------------------------
# Derived columns, totals and aggregates
# --------------------------------------------------------------------------

def derive(grid: list[float], record: dict[str, list],
           tempo: dict[str, list]) -> dict[str, list[float]]:
    """Per-color Tempo sums, auto-consumed solar and home consumption."""
    n = len(grid)
    auto = [None if not _present(record["solar_total_kwh"][i])
            or not _present(record["grid_out_kwh"][i])
            else record["solar_total_kwh"][i] - record["grid_out_kwh"][i]
            for i in range(n)]
    return {
        **{col: [tempo[f"tempo_{c}_hp"][i] + tempo[f"tempo_{c}_hc"][i]
                 for i in range(n)]
           for c, col in zip(COLORS, COLOR_SUM_COLUMNS)},
        "auto_consumed_kwh": auto,
        "home_consumption_kwh": [None if not _present(grid[i])
                                 or not _present(auto[i])
                                 else grid[i] + auto[i] for i in range(n)],
    }


def totals(columns: dict[str, list]) -> dict:
    """Yearly kWh of every reported metric, NaN-safe."""
    out = {"grid_in_kwh": _sum(columns["grid_in_kwh"])}
    for c in COLORS:
        out[f"tempo_{c}_kwh"] = _sum(columns[f"tempo_{c}_hp_hc"])
    out["tempo_hp_kwh"] = sum(_sum(columns[f"tempo_{c}_hp"]) for c in COLORS)
    out["tempo_hc_kwh"] = sum(_sum(columns[f"tempo_{c}_hc"]) for c in COLORS)
    out["tempo_total_kwh"] = out["tempo_hp_kwh"] + out["tempo_hc_kwh"]
    for key in ("solar_total_kwh", "grid_out_kwh", "electric_car_kwh",
                "water_heater_kwh", "heaters_kwh", "auto_consumed_kwh",
                "home_consumption_kwh"):
        if key in columns:
            out[key] = _sum(columns[key])
    return {k: round(v, 2) for k, v in out.items()}


def aggregate(base: dict[str, list], new: dict[str, list]) -> list[dict]:
    """Per month and per day kWh buckets, chronologically.

    Each series is summed over its own present hours (a NaN hour is a gap,
    not a zero), so the monthly values re-sum to the yearly totals. `base`
    is the measured record, `new` the transformed columns.
    """
    series = ("solar_kwh", "grid_in_kwh", "grid_out_kwh", "auto_consumed_kwh",
              "tempo_total_kwh", "car_removed_kwh", "water_from_night_kwh",
              "water_to_day_kwh", "grid_in_base_kwh", "tempo_base_kwh")
    out: dict[str, dict] = {}
    for i, label in enumerate(base["utc_hour"]):
        for period in (label[:7], label[:10]):
            b = out.setdefault(period, {"period": period, "slots": 0,
                                        **{k: 0.0 for k in series}})
            b["slots"] += 1
            b["solar_kwh"] += _value(new["solar_total_kwh"][i])
            b["grid_in_kwh"] += _value(new["grid_in_kwh"][i])
            b["grid_out_kwh"] += _value(new["grid_out_kwh"][i])
            b["auto_consumed_kwh"] += _value(new["auto_consumed_kwh"][i])
            b["tempo_total_kwh"] += sum(_value(new[f"tempo_{c}_hp_hc"][i])
                                        for c in COLORS)
            b["car_removed_kwh"] += _value(new["car_removed_kwh"][i])
            b["water_from_night_kwh"] += _value(
                new["water_moved_from_night_kwh"][i])
            b["water_to_day_kwh"] += _value(new["water_moved_to_day_kwh"][i])
            b["grid_in_base_kwh"] += _value(base["grid_in_kwh"][i])
            b["tempo_base_kwh"] += sum(_value(base[f"tempo_{c}_hp_hc"][i])
                                       for c in COLORS)
    return [{k: (round(v, 2) if isinstance(v, float) else v)
             for k, v in out[period].items()} for period in sorted(out)]


def split_periods(periods: list[dict]) -> tuple[list[dict], list[dict]]:
    monthly = [p for p in periods if len(p["period"]) == 7]
    daily = [p for p in periods if len(p["period"]) == 10]
    return monthly, daily


def contract_costs(tariffs: dict[str, vmlib.Tariff],
                   columns: dict[str, list]) -> dict[str, float]:
    """Yearly cost per contract for a six-register split."""
    kwh = {k: _sum(columns[k]) for k in TEMPO_KEYS}
    return {name: round(t.cost(kwh), 2) for name, t in tariffs.items()}


def write_csv(path: str, columns: dict[str, list]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for i in range(len(columns["utc_hour"])):
            writer.writerow([columns[col][i] if col == "utc_hour"
                             else _fmt(columns[col][i], 4)
                             for col in CSV_COLUMNS])


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------

def render_html(result: dict, path: str) -> None:
    e = html.escape
    rows = []
    title = "Hourly transform - filtered hourly record"
    measured = result["totals"]["measured"]
    transformed = result["totals"]["transformed"]
    deltas = result["deltas"]
    car = result["car"]
    water = result["water"]
    monthly, daily = result["monthly"], result["daily"]
    has_car = result["filters"]["car"]
    has_water = result["filters"]["water"]

    def table(headers, data):
        h = "".join(f"<th>{e(str(x))}</th>" for x in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>"
            for r in data)
        return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"

    def collapsed(summary, content):
        return (f"<details><summary>{e(summary)}</summary>"
                f"{content}</details>")

    def kpi(label, value, sub, cls="card"):
        return (f"<div class='{cls}'><div class='kpi-label'>{e(label)}</div>"
                f"<div class='kpi-value'>{e(value)}</div>"
                f"<div class='kpi-sub'>{e(sub)}</div></div>")

    def num(value, digits=2):
        return "-" if not _present(value) else f"{value:,.{digits}f}"

    def pct(value):
        return "-" if not _present(value) else f"{value:+.2f} %"

    def grouped_bars(groups, series_names, palette):
        """Vertical grouped bar chart.

        groups = [(x_label, [value per series]), ...] aligned to
        `series_names`, colored in palette order.
        """
        maxv = max((abs(v) for _, vs in groups for v in vs), default=0.0) or 1.0
        out = ['<div class="chart">', '<div class="chart-groups">']
        for x, vs in groups:
            out.append('<div class="chart-group"><div class="chart-bars">')
            for k, v in enumerate(vs):
                h = abs(v) / maxv * 100
                out.append(
                    f'<div class="chart-bar" style="height:{h:.1f}%;'
                    f'background:{palette[k % len(palette)]}" '
                    f'title="{e(series_names[k])}: {v:.2f} kWh">'
                    f'<span>{v:.0f}</span></div>')
            out.append('</div>')
            out.append(f'<div class="chart-month">{e(x)}</div></div>')
        out.append('</div>')
        legend = "".join(
            f'<span><i class="dot" style="background:'
            f'{palette[k % len(palette)]}"></i>{e(name)}</span>'
            for k, name in enumerate(series_names))
        out.append(f'<div class="chart-legend">{legend}</div></div>')
        return "".join(out)

    def line_chart(x_labels, series, tooltip_series=None, width=620,
                   height=220):
        """SVG line chart; series = [(name, color, [values]), ...] aligned to
        x_labels. Negative values (export, removed energy) plot below the
        zero baseline.

        tooltip_series = [(name, color, [values]), ...] supplies extra series
        that are NOT drawn but shown in the per-point hover tooltip.
        """
        drawn = list(series)
        tips = drawn + [(n, c, vs) for n, c, vs in (tooltip_series or [])]
        vals = [v for _, _, vs in drawn for v in vs]
        ymin = min([0.0] + vals)
        ymax = max([0.0] + vals)
        if ymax - ymin < 1e-9:
            ymax = ymin + 1.0
        pl, pr, pt, pb = 34, 10, 10, 24
        pw = width - pl - pr
        ph = height - pt - pb
        n = len(x_labels)
        # One x label per point is unreadable on a daily series (365 points),
        # so the tick labels are thinned out; the last one is always kept.
        label_step = 1 if n <= 24 else n // 14

        def X(i):
            return pl + (pw * i / (n - 1) if n > 1 else pw / 2)

        def Y(v):
            return pt + (ymax - v) / (ymax - ymin) * ph

        svg = [f'<svg class="chart-svg" viewBox="0 0 {width} {height}">']
        for k in range(5):
            v = ymin + (ymax - ymin) * k / 4
            yy = Y(v)
            svg.append(f'<line class="grid" x1="{pl}" y1="{yy:.1f}" '
                       f'x2="{width - pr}" y2="{yy:.1f}"/>')
            svg.append(f'<text class="axis" x="{pl - 5}" y="{yy + 3:.1f}" '
                       f'text-anchor="end">{v:,.1f}</text>')
        svg.append(f'<line class="zero" x1="{pl}" y1="{Y(0):.1f}" '
                   f'x2="{width - pr}" y2="{Y(0):.1f}"/>')
        for name, color, vs in drawn:
            pts = " ".join(f"{X(i):.1f},{Y(v):.1f}"
                           for i, v in enumerate(vs))
            svg.append(f'<polyline fill="none" stroke="{color}" '
                       f'stroke-width="2" stroke-linejoin="round" '
                       f'points="{pts}"/>')
        for i, lab in enumerate(x_labels):
            if i % label_step and i != n - 1:
                continue
            svg.append(f'<text class="axis" x="{X(i):.1f}" '
                       f'y="{height - 8}" text-anchor="middle">'
                       f'{e(lab)}</text>')
        for i in range(n):
            for name, color, vs in drawn:
                svg.append(
                    f'<circle cx="{X(i):.1f}" cy="{Y(vs[i]):.1f}" r="3" '
                    f'fill="{color}" data-x="{i}"/>')
        svg.append('</svg>')
        legend = "".join(
            f'<span><i class="dot" style="background:{e(color)}"></i>'
            f'{e(name)}</span>'
            for name, color, _ in drawn)
        data = json.dumps({
            "labels": x_labels,
            "series": [{"n": n, "c": c, "v": vs} for n, c, vs in tips],
        })
        tip = ('<div class="chart-tip" hidden></div>'
               '<script>(function(chart){var tip=chart.querySelector("'
               '.chart-tip");if(!tip)return;var D=__DATA__;var cs=chart.'
               'querySelectorAll("svg circle");function rows(x){var s=D.'
               'series,o=[];for(var k=0;k<s.length;k++){o.push('
               '"<span class=\'tip-d\' style=\'background:"+s[k].c+"\'></span>"'
               '+s[k].n+"&nbsp;<b>"+s[k].v[x].toFixed(2)+" kWh</b>");}return o;}'
               'function show(el){var x=+el.getAttribute("data-x");tip.innerHTML='
               '"<b>"+D.labels[x]+"</b><br>"+rows(x).join("<br>");tip.hidden=false;}'
               'function place(el,ev){var r=chart.getBoundingClientRect();'
               'tip.style.left=(ev.clientX-r.left+14)+"px";tip.style.top='
               '(ev.clientY-r.top-8)+"px";}for(var j=0;j<cs.length;j++){'
               '(function(c){c.addEventListener("mouseenter",function(e){'
               'show(c);place(c,e);});c.addEventListener("mousemove",'
               'function(e){place(c,e);});c.addEventListener("mouseleave",'
               'function(){tip.hidden=true;});})(cs[j]);}})(document'
               '.currentScript.parentElement);</script>').replace(
            "__DATA__", data)
        return (f'<div class="chart">{"".join(svg)}'
                f'<div class="chart-legend">{legend}</div>{tip}</div>')

    def short_month(period):
        return datetime.datetime.strptime(period, "%Y-%m").strftime("%b %y")

    # --- new expected totals -------------------------------------------------
    identity_measured = measured["tempo_total_kwh"] - measured["grid_in_kwh"]
    identity_new = transformed["tempo_total_kwh"] - transformed["grid_in_kwh"]
    id_pct_measured = (identity_measured / measured["grid_in_kwh"] * 100
                       if measured["grid_in_kwh"] else None)
    id_pct_new = (identity_new / transformed["grid_in_kwh"] * 100
                  if transformed["grid_in_kwh"] else None)
    rows.append('<div class="cards">' + "".join([
        kpi("Grid In (measured)", num(measured["grid_in_kwh"]) + " kWh",
            "as metered"),
        kpi("Grid In (transformed)", num(transformed["grid_in_kwh"]) + " kWh",
            f"{pct(deltas['grid_in_kwh']['pct'])} of measured",
            "card ok" if deltas["grid_in_kwh"]["kwh"] <= 0 else "card bad"),
        kpi("Sum Tempo (measured)", num(measured["tempo_total_kwh"]) + " kWh",
            "six registers"),
        kpi("Sum Tempo (transformed)",
            num(transformed["tempo_total_kwh"]) + " kWh",
            f"{pct(deltas['tempo_total_kwh']['pct'])} of measured",
            "card ok" if deltas["tempo_total_kwh"]["kwh"] <= 0 else "card bad"),
        kpi("Tempo - Grid In", f"{identity_new:+,.2f} kWh",
            f"{pct(id_pct_new)} (measured {identity_measured:+,.2f} kWh, "
            f"{pct(id_pct_measured)})", "card ok"),
    ]) + '</div>')

    rows.append("<h2>New expected totals</h2>")
    rows.append(table(
        ["Metric", "Measured kWh", "Transformed kWh", "Delta kWh", "Delta %"],
        [[label, num(measured.get(key)), num(transformed.get(key)),
          "-" if key not in deltas else f"{deltas[key]['kwh']:+,.2f}",
          pct(deltas.get(key, {}).get("pct"))]
         for key, label in METRIC_LABELS
         if key in measured and key in transformed]))
    rows.append("<p>The water filter only redistributes the day, so its "
                "yearly totals stay flat: what changes is the hour of "
                "consumption and the High/Low cost split. The car filter "
                "removes energy from the house, hence the drop of Grid In "
                "and Sum Tempo - both filters move Grid In and Tempo "
                "together, so the identity below is preserved.</p>")

    rows.append("<h2>Filters applied</h2>")
    rows.append('<div class="cards">' + "".join([
        kpi("Car filter", "ON" if has_car else "off",
            f"{num(car['removed_kwh'])} kWh removed" if has_car
            else "no effect",
            "card ok" if has_car else "card"),
        kpi("Water filter", "ON" if has_water else "off",
            f"{num(water['moved_kwh'])} kWh night -> day" if has_water
            else "no effect",
            "card ok" if has_water else "card"),
        kpi("Night water", num(water["water_night_kwh"]) + " kWh",
            f"{water['days_with_night_water']} night days"),
        kpi("Daily total", "unchanged" if has_water else "-",
            f"night {num(water['grid_in_night_kwh'])} / "
            f"day {num(water['grid_in_day_kwh'])} kWh" if has_water else "-"),
    ]) + '</div>')

    if has_car:
        rows.append("<h3>Car filter: the house as if the car had never been "
                    "charged</h3>")
        rows.append(table(
            ["kWh", "value"],
            [["Car energy metered", num(car["car_total_kwh"])],
             ["Removed from Grid In and Tempo", num(car["removed_kwh"])],
             ["Left in place (hourly cap)", num(car["left_in_place_kwh"])],
             ["Hours with car energy", car["hours_with_car"]],
             ["Hours actually filtered", car["hours_removed"]]]))
        rows.append("<h4>Tempo registers the removal came from</h4>")
        rows.append(table(
            ["Tempo register", "kWh removed"],
            [[k, num(v)] for k, v in
             car["removed_by_tempo_register_kwh"].items() if v]))

    if has_water:
        night = window_text(water["night_hours_utc"])
        day = window_text(water["day_hours_utc"])
        rows.append(f"<h3>Water filter: night energy moved into {e(day)} "
                    f"UTC</h3>")
        rows.append(table(
            ["kWh", "value"],
            [["Water energy metered", num(water["water_total_kwh"])],
             [f"Water burnt at night ({e(night)} UTC)",
              num(water["water_night_kwh"])],
             [f"Moved to the day window ({e(day)} UTC)",
              num(water["moved_kwh"])],
             ["Left at night (hourly cap)", num(water["left_at_night_kwh"])],
             ["Grid In, night hours", num(water["grid_in_night_kwh"])],
             ["Grid In, day window", num(water["grid_in_day_kwh"])],
             ["Days with night water", water["days_with_night_water"]],
             ["Days moved", water["days_moved"]],
             ["Days split equally (no daytime draw)",
              water["days_equal_split"]],
             ["Days without a destination hour",
              len(water["days_without_destination"])]]))
        rows.append("<h4>Tempo registers the move came from / went to</h4>")
        rows.append(table(
            ["Tempo register", "kWh removed at night", "kWh added in the day"],
            [[k, num(-v), num(v)] for k, v in
             water["moved_by_tempo_register_kwh"].items()]))

    # --- monthly grid / tempo ----------------------------------------------
    if monthly:
        names = ["Grid In measured", "Grid In transformed",
                 "Sum Tempo measured", "Sum Tempo transformed"]
        groups = [(short_month(m["period"]),
                   [m["grid_in_base_kwh"], m["grid_in_kwh"],
                    m["tempo_base_kwh"], m["tempo_total_kwh"]])
                  for m in monthly]
        rows.append("<h2>Monthly Grid In / Sum Tempo (kWh)</h2>")
        rows.append(grouped_bars(groups, names,
                                 ["#4a90d9", "#7fb2e0", "#e0a04e", "#f0c68a"]))

    # --- monthly and daily solar / grid / export / auto-consumed ------------
    if monthly:
        labels = [short_month(m["period"]) for m in monthly]
        series = [
            ("Solar", "#e6b800", [m["solar_kwh"] for m in monthly]),
            ("Auto-consumption", "#2e9e5b",
             [m["auto_consumed_kwh"] for m in monthly]),
            ("Export", "#4a90d9", [-m["grid_out_kwh"] for m in monthly]),
            ("Grid In (transformed)", "#8e44ad",
             [m["grid_in_kwh"] for m in monthly]),
        ]
        tips = [
            ("Grid In (measured)", "#4a90d9",
             [m["grid_in_base_kwh"] for m in monthly]),
            ("Sum Tempo (transformed)", "#e0a04e",
             [m["tempo_total_kwh"] for m in monthly]),
            ("Car removed", "#d64545", [m["car_removed_kwh"] for m in monthly]),
        ]
        rows.append("<h2>Monthly solar / grid / export / auto-consumed "
                    "(kWh)</h2>")
        rows.append("<p>Export is plotted negative, below the baseline. The "
                    "hover tooltip adds the measured Grid In, the Tempo "
                    "total and the car energy removed.</p>")
        rows.append(line_chart(labels, series, tooltip_series=tips))

    if daily:
        labels = [d["period"][5:] for d in daily]
        series = [
            ("Solar", "#e6b800", [d["solar_kwh"] for d in daily]),
            ("Auto-consumption", "#2e9e5b",
             [d["auto_consumed_kwh"] for d in daily]),
            ("Export", "#4a90d9", [-d["grid_out_kwh"] for d in daily]),
            ("Grid In (transformed)", "#8e44ad",
             [d["grid_in_kwh"] for d in daily]),
        ]
        tips = [
            ("Grid In (measured)", "#4a90d9",
             [d["grid_in_base_kwh"] for d in daily]),
            ("Sum Tempo (transformed)", "#e0a04e",
             [d["tempo_total_kwh"] for d in daily]),
            ("Car removed", "#d64545", [d["car_removed_kwh"] for d in daily]),
        ]
        rows.append("<h2>Daily solar / grid / export / auto-consumed "
                    "(kWh)</h2>")
        rows.append(line_chart(labels, series, tooltip_series=tips))

    if daily:
        series = [
            ("Car removed", "#d64545",
             [-d["car_removed_kwh"] for d in daily]),
            ("Water moved from night", "#4a90d9",
             [-d["water_from_night_kwh"] for d in daily]),
            ("Water moved to day", "#2e9e5b",
             [d["water_to_day_kwh"] for d in daily]),
        ]
        rows.append("<h2>Daily effect of the filters on Grid In (kWh)</h2>")
        rows.append("<p>Negative = less Grid In that day, positive = more. "
                    "The water move nets to zero over a day, the car "
                    "removal does not.</p>")
        rows.append(line_chart(labels, series))

    # --- contract costs -----------------------------------------------------
    costs = result.get("contracts")
    if costs:
        rows.append("<h2>Yearly cost of the transformed split</h2>")
        rows.append(table(
            ["Contract", "Measured EUR", "Transformed EUR", "Delta EUR"],
            [[name, num(costs["measured"][name]),
              num(costs["transformed"][name]),
              f"{costs['transformed'][name] - costs['measured'][name]:+,.2f}"]
             for name in sorted(costs["measured"])]))

    # --- detail tables ------------------------------------------------------
    if monthly:
        rows.append(collapsed(
            f"Monthly detail ({len(monthly)} months)",
            table(["Month", "Solar", "Grid In measured", "Grid In after",
                   "Sum Tempo measured", "Sum Tempo after", "Export",
                   "Auto-consumed", "Car removed", "Water night -> day"],
                  [[m["period"], num(m["solar_kwh"]),
                    num(m["grid_in_base_kwh"]), num(m["grid_in_kwh"]),
                    num(m["tempo_base_kwh"]), num(m["tempo_total_kwh"]),
                    num(m["grid_out_kwh"]), num(m["auto_consumed_kwh"]),
                    num(m["car_removed_kwh"]),
                    f"{m['water_from_night_kwh']:.2f} -> "
                    f"{m['water_to_day_kwh']:.2f}"] for m in monthly])))
    if daily:
        rows.append(collapsed(
            f"Daily detail ({len(daily)} days)",
            table(["Date", "Solar", "Grid In measured", "Grid In after",
                   "Sum Tempo after", "Export", "Auto-consumed",
                   "Car removed", "Water night -> day"],
                  [[d["period"], num(d["solar_kwh"]),
                    num(d["grid_in_base_kwh"]), num(d["grid_in_kwh"]),
                    num(d["tempo_total_kwh"]), num(d["grid_out_kwh"]),
                    num(d["auto_consumed_kwh"]), num(d["car_removed_kwh"]),
                    f"{d['water_from_night_kwh']:.2f} -> "
                    f"{d['water_to_day_kwh']:.2f}"] for d in daily])))

    w = result["window"]
    css = """
 body { font-family: -apple-system, 'Segoe UI', Roboto, Arial, sans-serif;
        margin: 2rem; color: #222; background: #fafafa; }
 h1 { border-bottom: 2px solid #333; padding-bottom: .3rem; }
 h2 { margin-top: 2rem; border-bottom: 1px solid #ccc; padding-bottom: .2rem; }
 h3 { margin-top: 1.2rem; }
 h4 { margin-top: .9rem; font-size: .95rem; color: #444; }
 .cards { display: flex; flex-wrap: wrap; gap: .8rem; margin: 1rem 0; }
 .card { flex: 1 1 160px; background: #fff; border: 1px solid #ddd;
         border-left: 5px solid #4a90d9; border-radius: 6px; padding: .6rem .9rem;
         box-shadow: 0 1px 2px rgba(0,0,0,.05); }
 .card.ok { border-left-color: #2e9e5b; }
 .card.bad { border-left-color: #d64545; }
 .kpi-label { font-size: .7rem; text-transform: uppercase; letter-spacing: .04em;
              color: #666; }
 .kpi-value { font-size: 1.3rem; font-weight: 600; margin: .15rem 0; }
 .kpi-sub { font-size: .8rem; color: #888; }
 .chart { margin: .6rem 0 1rem; position: relative; }
 .chart-tip { position: absolute; z-index: 10; pointer-events: none;
              background: rgba(20,20,20,.92); color: #fff; border-radius: 5px;
              padding: .4rem .55rem; font-size: .72rem; line-height: 1.4;
              white-space: nowrap; box-shadow: 0 1px 4px rgba(0,0,0,.3); }
 .chart-tip .tip-d { display: inline-block; width: 8px; height: 8px;
                     border-radius: 2px; margin-right: .3rem;
                     vertical-align: baseline; }
 .chart-tip b { color: #fff; }
 .chart-groups { display: flex; align-items: stretch; gap: .25rem; height: 210px; }
 .chart-group { flex: 1 1 0; display: flex; flex-direction: column; min-width: 0; }
 .chart-bars { flex: 1; display: flex; align-items: flex-end;
               justify-content: center; gap: 3px; }
 .chart-bar { position: relative; width: min(16px, 42%); display: flex;
              align-items: flex-start; justify-content: center;
              border-radius: 3px 3px 0 0; }
 .chart-bar span { font-size: .6rem; font-weight: 600; color: #fff;
                   padding-top: 2px; text-shadow: 0 0 2px rgba(0,0,0,.5); }
 .chart-month { text-align: center; font-size: .68rem; color: #555;
                margin-top: .25rem; white-space: nowrap; }
 .chart-legend { display: flex; flex-wrap: wrap; gap: 1.2rem; margin-top: .4rem;
                 font-size: .8rem; color: #333; }
 .chart-legend .dot { display: inline-block; width: 12px; height: 12px;
                      border-radius: 3px; margin-right: .35rem;
                      vertical-align: -1px; }
 .chart-svg { width: 100%; height: auto; max-width: 680px; }
 .chart-svg .grid { stroke: #e4e4e4; stroke-width: 1; }
 .chart-svg .zero { stroke: #999; stroke-width: 1; stroke-dasharray: 3 3; }
 .chart-svg .axis { font-family: Arial, sans-serif; font-size: 9px; fill: #666; }
 details { margin: .6rem 0; background: #fff; border: 1px solid #ddd;
           border-radius: 6px; }
 details summary { cursor: pointer; padding: .5rem .8rem; font-weight: 600;
                   color: #333; }
 details table { margin: .2rem 1rem 1rem; }
 p { color: #444; }
 table { border-collapse: collapse; margin: 1rem 0; width: 100%; }
 th, td { border: 1px solid #aaa; padding: .3rem .7rem; text-align: left; }
 th { background: #eee; }
"""
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{e(title)}</title>
<style>{css}</style></head><body>
<h1>{e(title)}</h1>
<p>Source: {e(result['source'])}<br>
Window: {e(w['start'])} -> {e(w['end'])} ({w['hours']} hours, {w['days']} days)<br>
Filters: car {'ON' if has_car else 'off'}, water {'ON' if has_water else 'off'}<br>
Generated: {e(result['generated'])}</p>
{''.join(rows)}
</body></html>
"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(page)
    print(f"HTML report saved: {path}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Transform the hourly record with the car / water filters")
    ap.add_argument("--config", default="scripts/energy-config.yaml")
    ap.add_argument("--input", default="scripts/output/hourly-energy.csv",
                    help="canonical record (01-hourly-export.py)")
    ap.add_argument("--output", default="scripts/output/hourly-transform.csv",
                    help="transformed record (JSON + HTML share the basename)")
    ap.add_argument("--filter-car", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="deduct the car energy from Grid In and Tempo, as "
                         "if it had never been charged (default: on)")
    ap.add_argument("--filter-water", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="move the water heater night energy into the day "
                         "window of the same day (default: on)")
    args = ap.parse_args()

    cfg = vmlib.load_config(args.config)

    if not os.path.exists(args.input):
        print(f"error: {args.input} not found - run `make export` first")
        return 1

    record = vmlib.load_hourly_energy(args.input)
    missing = [c for c in REQUIRED_COLUMNS if c not in record]
    if missing:
        print(f"error: input {args.input} lacks columns {missing}")
        return 1

    n_hours = len(record["utc_hour"])
    start = record["utc_hour"][0] if n_hours else "?"
    end = record["utc_hour"][-1] if n_hours else "?"
    print(f"Source: {args.input} (offline, no DB query)")
    print(f"Window: {start} -> {end} ({n_hours} hours)\n")

    wcfg = cfg.get("hourly_transform", {}).get("water", {})
    night_hours = norm_window(wcfg.get("night_hours"), DEFAULT_NIGHT_HOURS)
    day_hours = norm_window(wcfg.get("day_hours"), DEFAULT_DAY_HOURS)
    distribution = str(wcfg.get("distribution", "grid_in"))
    print(f"Car filter:   {'ON' if args.filter_car else 'off'}"
          f"  (--{'no-' if args.filter_car else ''}filter-car)")
    print(f"Water filter: {'ON' if args.filter_water else 'off'}"
          f"  (--{'no-' if args.filter_water else ''}filter-water),"
          f" night {window_text(night_hours)} -> day"
          f" {window_text(day_hours)} UTC, split on {distribution}\n")

    # The measured columns of the record are never touched: the filters work
    # on copies of Grid In and of the six Tempo registers, and the transform
    # is then published as its own column set.
    grid = list(record["grid_in_kwh"])
    tempo = {k: list(record[k]) for k in TEMPO_KEYS}

    if args.filter_car:
        car_column, car_removed, car_report = filter_car(record, grid, tempo)
    else:
        car_column, car_removed = list(record["electric_car_kwh"]), \
            [0.0] * n_hours
        car_report = car_report_off(record)

    if args.filter_water:
        water_column, water_from_night, water_to_day, water_report = \
            filter_water(record, grid, tempo, night_hours, day_hours,
                         distribution)
    else:
        water_column = list(record["water_heater_kwh"])
        water_from_night, water_to_day = [0.0] * n_hours, [0.0] * n_hours
        water_report = water_report_off(record, night_hours, day_hours,
                                        distribution)

    columns = {
        **record,
        "grid_in_kwh": grid,
        **tempo,
        **derive(grid, record, tempo),
        "electric_car_kwh": car_column,
        "water_heater_kwh": water_column,
        "car_removed_kwh": car_removed,
        "water_moved_from_night_kwh": water_from_night,
        "water_moved_to_day_kwh": water_to_day,
    }

    measured = totals(record)
    transformed = totals(columns)
    deltas = {
        key: {"kwh": round(transformed[key] - measured[key], 2),
              "pct": (round((transformed[key] - measured[key])
                            / measured[key] * 100, 2)
                      if measured.get(key) else None)}
        for key in transformed if key in measured
    }

    monthly, daily = split_periods(aggregate(record, columns))
    tariffs = vmlib.load_tariffs(cfg)
    contracts = {"measured": contract_costs(tariffs, record),
                 "transformed": contract_costs(tariffs, columns)}

    result = {
        "generated": datetime.datetime.now(datetime.timezone.utc)
                              .strftime("%Y-%m-%d %H:%M UTC"),
        "window": {"start": start, "end": end, "hours": n_hours,
                   "days": n_hours // 24},
        "source": args.input,
        "output": args.output,
        "filters": {"car": args.filter_car, "water": args.filter_water},
        "totals": {"measured": measured, "transformed": transformed},
        "deltas": deltas,
        "car": car_report,
        "water": water_report,
        "contracts": contracts,
        "monthly": monthly,
        "daily": daily,
    }

    write_csv(args.output, columns)

    # --- console report ------------------------------------------------------
    print("Filters")
    if args.filter_car:
        print(f"  car: {car_report['car_total_kwh']:.2f} kWh metered, "
              f"{car_report['removed_kwh']:.2f} kWh deducted from Grid In "
              f"and Tempo over {car_report['hours_removed']} hours, "
              f"{car_report['left_in_place_kwh']:.2f} kWh left in place "
              f"(hourly Grid In / Tempo cap)")
    if args.filter_water:
        print(f"  water: {water_report['water_night_kwh']:.2f} kWh burnt at "
              f"night, {water_report['moved_kwh']:.2f} kWh moved to "
              f"{window_text(day_hours)} UTC over "
              f"{water_report['days_moved']} days, "
              f"{water_report['left_at_night_kwh']:.2f} kWh left at night")
    if not args.filter_car and not args.filter_water:
        print("  no filter enabled: the record is copied unchanged")

    print(f"\n{'metric':22} {'measured':>11} {'transformed':>12} "
          f"{'delta':>10} {'delta %':>9}")
    for key, label in METRIC_LABELS:
        if key not in measured or key not in transformed:
            continue
        delta = deltas.get(key, {"kwh": 0.0, "pct": None})
        delta_pct = "-" if delta["pct"] is None else f"{delta['pct']:+.2f}"
        print(f"{label:22} {measured[key]:11.2f} {transformed[key]:12.2f} "
              f"{delta['kwh']:+10.2f} {delta_pct:>9}")

    identity_measured = measured["tempo_total_kwh"] - measured["grid_in_kwh"]
    identity_new = transformed["tempo_total_kwh"] - transformed["grid_in_kwh"]
    print(f"\nIdentity Tempo - Grid In: {identity_measured:+.2f} kWh "
          f"({identity_measured / measured['grid_in_kwh'] * 100:+.2f} %) "
          f"measured -> {identity_new:+.2f} kWh "
          f"({identity_new / transformed['grid_in_kwh'] * 100:+.2f} %) "
          f"transformed")

    print(f"\n{'contract':14} {'measured EUR':>14} {'transformed EUR':>17} "
          f"{'delta EUR':>11}")
    for name in sorted(contracts["measured"]):
        before = contracts["measured"][name]
        after = contracts["transformed"][name]
        print(f"{name:14} {before:14.2f} {after:17.2f} {after - before:+11.2f}")

    json_path = os.path.splitext(args.output)[0] + ".json"
    vmlib.write_json(json_path, result)
    html_path = os.path.splitext(args.output)[0] + ".html"
    print(f"\nWritten: {args.output} ({n_hours} rows)")
    print(f"Written: {json_path}")
    render_html(result, html_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
