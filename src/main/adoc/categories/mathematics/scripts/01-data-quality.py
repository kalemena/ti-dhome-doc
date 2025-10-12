#!/usr/bin/env python3
"""
VictoriaMetrics data-quality report for the mathematics/energy analysis.

Reproduces, as reusable functions, the probes executed against the live VM:
  1. per-day max_over_time sampling of cumulative counters over a 1-year window,
  2. daily deltas (increments),
  3. Tempo identity check: Grid In vs sum of *available* Tempo counters per day
     (inactive White/Red days are expected, not faults),
  4. missing-day classification per counter + deficit by missing combination,
  5. solar consistency check: AC vs sum(DC channels), counter resets, flat
     days, and "exported more than produced" anomalies; the JSON keeps the
     monthly solar / export / auto-consumed aggregates plus the same
     per-day aggregates (`solar.daily`), and `identity.daily` holds the
     per-day grid / tempo pair, so the HTML report can render both the
     monthly and the daily "solar / grid / export / auto-consumed" views,
  6. hourly data-quality check (per-hour slots): coverage per metric, grid
     in/out gap windows, solar daylight gaps vs expected night silence, solar
     hourly increments beyond the physical panel cap (1 kW panels ->
     at most ~1 kWh/h; a larger delta means a counter jump / corruption), and
     the car / water heater / heaters counters (coverage, gap windows,
     counter resets),
  7. optional monthly coverage probe per metric,
  8. optional HTML report rendering.

Usage:
    01-data-quality.py [-u VM_URL] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                       [--config energy-config.yaml] [--output data-quality.json]
                       [--checks all|identity|solar|hourly]
                       [--threshold 0.05] [--workers 16] [--html report.html]
                       [--no-coverage]
"""

import argparse
import datetime
import html
import json
import os
import sys
import urllib.request
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

TEMPO_KEYS = [
    "tempo_blue_hc", "tempo_blue_hp",
    "tempo_white_hc", "tempo_white_hp",
    "tempo_red_hc", "tempo_red_hp",
]

# Car / water heater / heaters: high consumers sitting behind the same garage
# power meter as grid in/out, as cumulative energy counters (kWh). They get the
# same hourly treatment as grid_in/grid_out: coverage, gap windows, and counter
# resets. A flat hour is normal (nothing drawing), a fully flat window is not.
CONSUMER_KEYS = ["electric_car", "water_heater", "heaters"]


def load_config(path: str | None) -> dict:
    if not path:
        raise SystemExit("ERROR: a config file is required "
                         "(use --config path/to/energy-config.yaml)")
    if not HAVE_YAML:
        raise SystemExit("ERROR: PyYAML is required to read the config file. "
                         "Run `make setup` to install deps.")
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: config file not found: {path}")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    print(f"Loaded config: {path}")
    return cfg


def parse_ts(value) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(
            tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def window_days(cfg: dict) -> tuple[datetime.datetime, datetime.datetime, int]:
    start = parse_ts(cfg["window"]["start"])
    end = parse_ts(cfg["window"]["end"])
    return start, end, (end - start).days


def boundary_times(start: datetime.datetime, n_days: int, offset_s: int = 1) -> list[int]:
    return [
        int((start + datetime.timedelta(days=i)).timestamp()) + offset_s
        for i in range(n_days + 1)
    ]


def query_max_over_time(url: str, selector: str, time_ts: int,
                        lookback: str = "2d") -> float | None:
    query = urllib.parse.urlencode({
        "query": f"max_over_time({selector}[{lookback}])",
        "time": time_ts,
    })
    with urllib.request.urlopen(f"{url}/api/v1/query?{query}", timeout=60) as r:
        result = json.load(r)["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def sample_daily_series(url: str, selector: str, times: list[int],
                        workers: int = 16) -> list[float | None]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda t: query_max_over_time(url, selector, t), times))


def sample_deltas(url: str, selector: str, times: list[int],
                  workers: int = 16) -> list[float | None]:
    return daily_deltas(sample_daily_series(url, selector, times, workers))


def daily_deltas(values: list[float | None]) -> list[float | None]:
    return [
        None if values[i - 1] is None or values[i] is None
        else values[i] - values[i - 1]
        for i in range(1, len(values))
    ]


def identity_check(url: str, cfg: dict, start: datetime.datetime,
                   n_days: int, workers: int = 16,
                   grid_delta: list[float | None] | None = None,
                   tempo_delta: dict[str, list[float | None]] | None = None
                   ) -> dict:
    metrics = cfg["metrics"]
    grid = metrics["grid_in"]
    if grid_delta is None:
        grid_delta = sample_deltas(url, grid["selector"],
                                   boundary_times(start, n_days), workers)

    tempo = {k: metrics[k] for k in TEMPO_KEYS}
    if tempo_delta is None:
        times = boundary_times(start, n_days)
        tempo_delta = {key: sample_deltas(url, m["selector"], times, workers)
                       for key, m in tempo.items()}

    tot_grid = 0.0
    tot_tempo = 0.0
    incomplete_days = []
    diverging_days = []
    monthly: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    daily: list[dict] = []

    for i in range(n_days):
        g = grid_delta[i]
        if g is None:
            continue
        s = 0.0
        missing = []
        for key, dv in tempo_delta.items():
            if dv[i] is None:
                missing.append(key)
                continue
            s += dv[i] * tempo[key]["scale_to_kwh"]
        day = (start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        tot_grid += g
        tot_tempo += s
        if len(missing) == len(tempo_delta):
            incomplete_days.append({"date": day, "grid_kwh": round(g, 2),
                                    "missing": missing})
        elif abs(g - s) > 0.05 * max(g, 0.001):
            diverging_days.append({"date": day, "grid_kwh": round(g, 2),
                                   "tempo_kwh": round(s, 2)})
        mo = day[:7]
        monthly[mo][0] += g
        monthly[mo][1] += s
        daily.append({"date": day, "grid_kwh": round(g, 2),
                      "tempo_kwh": round(s, 2)})

    diff = tot_grid - tot_tempo
    return {
        "grid_in_total_kwh": round(tot_grid, 2),
        "tempo_sum_total_kwh": round(tot_tempo, 2),
        "diff_kwh": round(diff, 2),
        "diff_pct": round(diff / tot_grid * 100, 2) if tot_grid else None,
        "incomplete_days": incomplete_days,
        "diverging_days": diverging_days,
        "daily": daily,
        "monthly": {k: [round(v[0], 2), round(v[1], 2)] for k, v in
                    sorted(monthly.items())},
    }


def missing_classification(grid_delta: list[float | None],
                           tempo_scale: dict[str, float],
                           tempo_delta: dict[str, list[float | None]],
                           start: datetime.datetime) -> dict:
    per_counter = Counter()
    deficit = defaultdict(float)
    for i in range(len(grid_delta)):
        g = grid_delta[i]
        if g is None:
            continue
        missing = [k for k in tempo_delta if tempo_delta[k][i] is None]
        present = sum(tempo_delta[k][i] * tempo_scale[k] for k in tempo_delta
                      if tempo_delta[k][i] is not None)
        if missing:
            for k in missing:
                per_counter[k] += 1
            combo = "_".join(sorted(missing))
            deficit[combo] += g - present
    return {
        "silent_days_per_counter": dict(per_counter),
        "deficit_by_missing": {k: round(v, 2) for k, v in
                               sorted(deficit.items(), key=lambda x: -x[1])},
    }


def solar_check(url: str, cfg: dict, start: datetime.datetime, n_days: int,
                workers: int = 16, margin_kwh: float = 0.05) -> dict:
    metrics = cfg["metrics"]
    times = boundary_times(start, n_days)
    ac = sample_deltas(url, metrics["solar_total"]["selector"], times, workers)
    dc0 = sample_deltas(url, metrics["solar_dc0"]["selector"], times, workers)
    dc1 = sample_deltas(url, metrics["solar_dc1"]["selector"], times, workers)
    grid_out = sample_deltas(url, metrics["grid_out"]["selector"], times,
                             workers)

    known_days = set(cfg.get("known_anomalies", {}).get("solar_flat_day", []))

    missing = []
    negative = []
    flat = []
    imports = []
    unknown_imports = []
    dla = []
    monthly: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    daily: list[dict] = []
    tot_ac = tot_dc0 = tot_dc1 = tot_out = 0.0

    for i in range(n_days):
        day = (start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        a = ac[i]
        o = grid_out[i]
        if a is None:
            missing.append(day)
        else:
            if a < 0:
                negative.append({"date": day, "delta_kwh": round(a, 3)})
            elif a == 0:
                flat.append(day)
            tot_ac += a
            if o is None:
                dla.append(day)
            elif o > 0 and a < o - margin_kwh:
                imports.append({"date": day, "solar_kwh": round(a, 2),
                                "export_kwh": round(o, 2)})
        if o is not None:
            tot_out += o
        mo = day[:7]
        monthly[mo][0] += a if a is not None else 0.0
        monthly[mo][1] += o if o is not None else 0.0
        daily.append({
            "date": day,
            "solar_kwh": round(a, 2) if a is not None else None,
            "export_kwh": round(o, 2) if o is not None else None,
            "auto_consumed_kwh": (round(a - o, 2)
                                  if a is not None and o is not None else None),
        })
        if dc0[i] is not None:
            tot_dc0 += dc0[i]
        if dc1[i] is not None:
            tot_dc1 += dc1[i]

    sign = lambda x: (x >= margin_kwh or x <= -margin_kwh)
    ac_dc_diff = tot_ac - (tot_dc0 + tot_dc1)
    unknown_imports = [d for d in imports if d["date"] not in known_days]
    unknown_flat = [d for d in flat if d not in known_days]
    ok = (not missing and not negative and not unknown_imports
          and not unknown_flat and not sign(ac_dc_diff))

    return {
        "ok": ok,
        "solar_total_kwh": round(tot_ac, 2),
        "solar_dc0_kwh": round(tot_dc0, 2),
        "solar_dc1_kwh": round(tot_dc1, 2),
        "ac_vs_dc_sum_diff_kwh": round(ac_dc_diff, 2),
        "grid_out_total_kwh": round(tot_out, 2),
        "auto_consumed_total_kwh": round(tot_ac - tot_out, 2),
        "missing_days": missing,
        "negative_days": negative,
        "flat_days": flat,
        "export_exceeds_production_days": imports,
        "unknown_anomaly_days": unknown_imports + unknown_flat,
        "daily": daily,
        "monthly": {k: [round(v[0], 2), round(v[1], 2)] for k, v in
                    sorted(monthly.items())},
    }


def coverage_probe(url: str, cfg: dict, start: datetime.datetime,
                   n_days: int, samples: int = 13) -> dict:
    metrics = cfg["metrics"]
    step = n_days // samples
    sample_ts = []
    for i in range(samples):
        d = start + datetime.timedelta(days=i * step + step // 2)
        sample_ts.append(int(d.timestamp()))
    out = {}
    for key, m in metrics.items():
        if key in TEMPO_KEYS:
            continue
        vals = [query_max_over_time(url, m["selector"], ts)
                for ts in sample_ts]
        have = [v for v in vals if v is not None]
        out[key] = {
            "covered_samples": len(have),
            "total_samples": samples,
            "coverage_pct": round(len(have) / samples * 100, 1),
            "first": have[0] if have else None,
            "last": have[-1] if have else None,
        }
    # The six Tempo registers are redundant views of the same draw, so a
    # per-register coverage is misleading (registers are silent while their
    # color is inactive). The single signal that matters is whether at least
    # one register reports at each sample.
    have = []
    for ts in sample_ts:
        vals = [query_max_over_time(url, metrics[k]["selector"], ts)
                for k in TEMPO_KEYS]
        present = [v for v in vals if v is not None]
        if present:
            have.append(max(present))
    out["tempo"] = {
        "covered_samples": len(have),
        "total_samples": samples,
        "coverage_pct": round(len(have) / samples * 100, 1),
        "first": have[0] if have else None,
        "last": have[-1] if have else None,
    }
    return out


def fetch_hourly_series(url: str, selector: str, start: datetime.datetime,
                        end: datetime.datetime) -> dict[int, float]:
    """Return {unix_ts: counter value} at hourly boundaries for a counter.

    Fetched with query_range(step=1h) over monthly chunks. A full-year
    query_range exceeds VictoriaMetrics' per-series sample cap for the dense
    zigbee series (~30k samples/day), so the year is split per calendar month.
    """
    vals: dict[int, float] = {}
    month = start
    while month < end:
        nxt = (month.replace(day=28) + datetime.timedelta(days=7)
               ).replace(day=1)
        nxt = min(nxt, end)
        params = urllib.parse.urlencode({
            "query": selector,
            "start": month.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": nxt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "step": "3600s",
        })
        with urllib.request.urlopen(
                f"{url}/api/v1/query_range?{params}", timeout=120) as r:
            result = json.load(r)["data"]["result"]
        if result:
            for ts, value in result[0]["values"]:
                vals[int(ts)] = float(value)
        month = nxt
    return vals


def hourly_check(url: str, cfg: dict, start: datetime.datetime,
                 n_days: int) -> dict:
    """Hourly data-quality assertion over the 1-year window.

    For every hourly slot of the window each cumulative counter is expected to
    yield a value at its start and end boundaries so the hourly increment can
    be computed. Reports:

    * grid_in / grid_out: missing hourly slots (real measurement gaps), as
      windows;
    * solar_*: missing slots split between expected night silence and daylight
      gaps, plus hourly increments exceeding the physical panel cap (1 kW ->
      at most ~1 kWh/h); such jumps are flagged as counter corruption;
    * tempo_*: the six registers are redundant views of the same grid draw,
      so a missing counter (whole inactive-color days, single-register holes)
      is expected and NOT a gap as long as at least one of the six keeps
      reporting; only boundary hours where none of the six is reported ("stream
      down") are failures;
    * electric_car / water_heater / heaters: cumulative counters behind the
      same garage power meter as grid in/out, so the signals are the hourly
      missing slots (grouped in gap windows), the counter resets (negative
      hourly increments) and a window entirely flat (frozen counter). Flat
      *hours* are expected - a car only draws while charging.

    Gaps recorded in `known_anomalies` (grid_missing_hour, solar_missing_hour,
    solar_over_max_hour) are acknowledged and do not fail the check; the
    report still lists them. The three consumer counters sit behind the same
    garage power meter as grid in/out, so `grid_missing_hour` acknowledges
    their outages too; `consumer_missing_hour` (optional, keyed per counter)
    adds counter-specific ones.
    """
    metrics = cfg["metrics"]
    hq = cfg["hourly_quality"]
    solar_max = hq["solar_max_kwh_per_hour"]
    solar_tol = hq["solar_max_tolerance_kwh"]
    dl_start, dl_end = hq["solar_daylight_hours"]
    known = cfg.get("known_anomalies", {})
    known_grid = set(known.get("grid_missing_hour", []))
    known_solar_missing = set(known.get("solar_missing_hour", []))
    known_over = set(known.get("solar_over_max_hour", []))
    flat_days = set(known.get("solar_flat_day", []))
    known_stream = set(known.get("teleinfo_stream_gap_hour", []))
    known_consumer = {k: set(v)
                      for k, v in known.get("consumer_missing_hour", {}).items()}

    n_hours = n_days * 24
    boundaries = [int((start + datetime.timedelta(hours=i)).timestamp())
                  for i in range(n_hours + 1)]
    hour_of = lambda ts: datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%H")
    iso_label = lambda ts: datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    day_label = lambda ts: datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%d")

    def is_daylight(ts: int) -> bool:
        return dl_start <= int(hour_of(ts)) < dl_end

    print("Fetching hourly series (monthly query_range chunks)...")
    hourly: dict[str, dict[int, float]] = {}
    for key, m in metrics.items():
        hourly[key] = fetch_hourly_series(url, m["selector"], start,
                                          start + datetime.timedelta(
                                              days=n_days))

    def gap_windows(missing: list[int]) -> list[list[str]]:
        wins = []
        for ts in sorted(missing):
            if wins and ts == boundary_next.get(wins[-1][1]):
                wins[-1][1] = ts
            else:
                wins.append([ts, ts])
        return [[iso_label(a), iso_label(b)] for a, b in wins]

    # ts -> next boundary timestamp (for chaining gap windows)
    boundary_next = {}
    for i, b in enumerate(boundaries[:-1]):
        boundary_next[b] = boundaries[i + 1]

    def increments(vals) -> list[tuple[int, float]]:
        out = []
        for i in range(n_hours):
            a, b = boundaries[i], boundaries[i + 1]
            if a in vals and b in vals:
                out.append((i, vals[b] - vals[a]))
        return out

    report: dict = {}
    ok_flags: list[bool] = []

    # ----- grid in / out -----
    for key in ("grid_in", "grid_out"):
        vals = hourly[key]
        missing = [b for b in boundaries if b not in vals]
        unknown = [ts for ts in missing
                   if iso_label(ts) not in known_grid]
        kwh = sum(d for _, d in increments(vals))
        ok = not unknown
        ok_flags.append(ok)
        report[key] = {
            "ok": ok,
            "coverage_pct": round(
                (len(boundaries) - len(missing)) / len(boundaries) * 100, 2),
            "total_kwh": round(kwh, 2),
            "missing_slots": [iso_label(ts) for ts in sorted(missing)],
            "gap_windows": gap_windows(missing),
            "unknown_gap_windows": gap_windows(unknown),
            "known_missing_slots": [iso_label(ts) for ts in sorted(missing)
                                    if iso_label(ts) in known_grid],
        }
        print(f"  {key}: {report[key]['coverage_pct']}% covered, "
              f"{len(missing)} missing hrs, "
              f"{len([w for w in report[key]['gap_windows']])} gap window(s) -> "
              f"{'PASS' if ok else 'FAIL'}")

    # ----- solar (AC + DC channels) -----
    for key in ("solar_total", "solar_dc0", "solar_dc1"):
        vals = hourly[key]
        missing = [b for b in boundaries if b not in vals]
        flat = [ts for ts in sorted(missing)
                if day_label(ts) in flat_days]
        rest = [ts for ts in sorted(missing) if ts not in flat]
        daylight = [ts for ts in rest if is_daylight(ts)]
        night = [ts for ts in rest if not is_daylight(ts)]
        unknown_day = [ts for ts in daylight
                       if iso_label(ts) not in known_solar_missing]

        over = []
        for i, d in increments(vals):
            if d > solar_max + solar_tol:
                over.append({"utc": iso_label(boundaries[i]), "kwh": round(d, 3)})
        unknown_over = [o for o in over if o["utc"] not in known_over]
        ok = not unknown_day and not unknown_over
        ok_flags.append(ok)
        report[key] = {
            "ok": ok,
            "coverage_pct": round(
                (len(boundaries) - len(missing)) / len(boundaries) * 100, 2),
            "total_kwh": round(sum(d for _, d in increments(vals)), 2),
            "daylight_missing_slots": [iso_label(ts) for ts in sorted(daylight)],
            "unknown_daylight_missing_slots": [iso_label(ts) for ts in sorted(unknown_day)],
            "night_missing_count": len(night),
            "flat_day_missing_count": len(flat),
            "missing_windows": gap_windows(missing),
            "over_max_hours": over,
            "unknown_over_max_hours": [o["utc"] for o in unknown_over],
            "solar_max_kwh_per_hour": solar_max + solar_tol,
        }
        print(f"  {key}: {report[key]['coverage_pct']}% covered, "
              f"daylight missing={len(daylight)} (known days excluded), "
              f"night silence={len(night)}, over-max={len(over)} -> "
              f"{'PASS' if ok else 'FAIL'}")

    # ----- tempo: six redundant registers, one true-gap signal -----
    # The six Tempo counters are redundant registers of the same grid draw
    # (one Low + one High per daily color). A counter going silent (whole
    # inactive-color day, single-register hole) is expected and NOT a
    # data-quality gap: as long as at least one register keeps reporting the
    # grid draw is covered. Only boundary hours where NONE of the six is
    # reported are true stream outages; those are the only tempo gaps.
    stream_slots = {b for b in boundaries
                    if all(b not in hourly[k] for k in TEMPO_KEYS)}
    stream_gap_list = sorted(b for b in stream_slots
                             if iso_label(b) not in known_stream)
    days = {day_label(b) for b in boundaries}
    inactive_days = sorted(
        d for d in days
        if all(b in stream_slots
               for b in boundaries if day_label(b) == d))
    kwh = sum(sum(d for _, d in increments(hourly[k]))
              * metrics[k]["scale_to_kwh"] for k in TEMPO_KEYS)
    ok = not stream_gap_list
    ok_flags.append(ok)
    report["tempo"] = {
        "ok": ok,
        "coverage_pct": round(
            (len(boundaries) - len(stream_slots)) / len(boundaries) * 100, 2),
        "total_kwh": round(kwh, 2),
        "inactive_day_count": len(inactive_days),
        "inactive_days": inactive_days,
        "stream_gap_missing": len(stream_gap_list),
        "counter_specific_missing": 0,
    }
    report["teleinfo_stream_gap"] = {
        "hours": [iso_label(b) for b in stream_gap_list],
        "count": len(stream_gap_list),
        "windows": gap_windows(stream_gap_list),
        "ok": ok,
    }
    print(f"  tempo: {report['tempo']['coverage_pct']}% covered, "
          f"inactive-full-days={len(inactive_days)}, "
          f"stream-gap hrs={len(stream_gap_list)} -> "
          f"{'PASS' if ok else 'FAIL'}")

    # ----- high consumers: car / water heater / heaters (kWh counters) -----
    # Same garage power meter as grid in/out, so the same signals apply:
    # missing hourly slots grouped in gap windows, and counter resets (a
    # negative hourly increment). Flat hours are expected - a car only draws
    # while charging - but a window that never moves means a frozen counter.
    for key in CONSUMER_KEYS:
        vals = hourly.get(key)
        if vals is None:
            continue
        missing = [b for b in boundaries if b not in vals]
        acknowledged = known_grid | known_consumer.get(key, set())
        unknown = [ts for ts in missing if iso_label(ts) not in acknowledged]
        scale = metrics[key].get("scale_to_kwh", 1.0)
        deltas = increments(vals)
        resets = [{"utc": iso_label(boundaries[i]), "kwh": round(d, 3)}
                  for i, d in deltas if d < 0]
        flat = sum(1 for _, d in deltas if d == 0)
        frozen = bool(deltas) and flat == len(deltas)
        ok = not unknown and not resets and not frozen
        ok_flags.append(ok)
        report[key] = {
            "ok": ok,
            "coverage_pct": round(
                (len(boundaries) - len(missing)) / len(boundaries) * 100, 2),
            "total_kwh": round(sum(d for _, d in deltas) * scale, 2),
            "missing_slots": [iso_label(ts) for ts in sorted(missing)],
            "missing_days": sorted({day_label(ts) for ts in missing}),
            "gap_windows": gap_windows(missing),
            "unknown_gap_windows": gap_windows(unknown),
            "known_missing_slots": [iso_label(ts) for ts in sorted(missing)
                                    if iso_label(ts) in acknowledged],
            "resets": resets,
            "flat_hours": flat,
            "comparable_hours": len(deltas),
            "frozen_window": frozen,
        }
        m = report[key]
        print(f"  {key}: {m['coverage_pct']}% covered, "
              f"{len(missing)} missing hrs "
              f"({len(m['gap_windows'])} gap window(s)), "
              f"total={m['total_kwh']:.2f} kWh, resets={len(resets)} -> "
              f"{'PASS' if ok else 'FAIL'}")

    report["ok"] = all(ok_flags)
    return report


def format_day_list(days: list[str], limit: int = 12) -> str:
    if not days:
        return "-"
    shown = days[:limit]
    suffix = f" (+{len(days) - limit} more)" if len(days) > limit else ""
    return ", ".join(shown) + suffix


def _hourly_gap_summary(key: str, m: dict) -> str:
    if key in ("grid_in", "grid_out"):
        return f"{len(m['missing_slots'])} missing/silent hours"
    if key in CONSUMER_KEYS:
        parts = [f"{len(m['missing_slots'])} missing hours"]
        n_days_missing = len(m["missing_days"])
        if n_days_missing:
            parts.append(f"{n_days_missing} day"
                         + ("s" if n_days_missing > 1 else ""))
        if m["resets"]:
            parts.append(f"{len(m['resets'])} resets")
        if m["frozen_window"]:
            parts.append("frozen window")
        return ", ".join(parts)
    if key in ("solar_total", "solar_dc0", "solar_dc1"):
        parts = [
            f"{len(m['daylight_missing_slots'])} daylight missing",
            f"{m['night_missing_count']} night-silent",
        ]
        if m["over_max_hours"]:
            parts.append(f"{len(m['over_max_hours'])} over max")
        return ", ".join(parts)
    parts = []
    if m.get("inactive_day_count"):
        parts.append(f"{m['inactive_day_count']} no-data full days"
                     if key == "tempo"
                     else f"{m['inactive_day_count']} inactive days")
    if m.get("stream_gap_missing"):
        parts.append(f"{m['stream_gap_missing']} stream-gap hrs")
    return ", ".join(parts) or "none"


def print_report(report: dict, threshold_pct: float) -> None:
    if "identity" in report:
        id_ = report["identity"]
        print("\n=== identity: Grid In vs sum(Tempo) per day ===")
        print(f"Grid In year total    : {id_['grid_in_total_kwh']:9.2f} kWh")
        print(f"Sum Tempo (any subset): {id_['tempo_sum_total_kwh']:9.2f} kWh")
        print(f"Diff                  : {id_['diff_kwh']:+9.2f} kWh "
              f"({id_['diff_pct']:+.2f}%)")
        print("\nmonth:    grid /  tempo    (kWh)")
        for mo, (g, s) in id_["monthly"].items():
            print(f"  {mo}  {g:7.2f}  {s:7.2f}   diff {g - s:+6.2f}")
        print(f"\nincomplete days (no tempo register reported): "
              f"{len(id_['incomplete_days'])} ({format_day_list([d['date'] for d in id_['incomplete_days']], 5)})")
        print(f"daily aggregates (grid / tempo): {len(id_['daily'])} day(s), "
              f"full table in the JSON report")
        print(f"days diverging >{threshold_pct}% with complete data: "
              f"{len(id_['diverging_days'])}")
        for d in id_["diverging_days"][:10]:
            print("  ", d["date"], d["grid_kwh"], d["tempo_kwh"])

        miss = report["missing"]
        print("\n=== silent days per tempo counter ===")
        for k, v in miss["silent_days_per_counter"].items():
            print(f"  {k:12} {v}")
        print("deficit kWh by which counters silent together:")
        for k, v in miss["deficit_by_missing"].items():
            print(f"  silent={k:42} {v:+8.2f}")

    if "solar" in report:
        s = report["solar"]
        print("\n=== solar consistency ===")
        print(f"AC total      : {s['solar_total_kwh']:9.2f} kWh")
        print(f"DC0 total     : {s['solar_dc0_kwh']:9.2f} kWh")
        print(f"DC1 total     : {s['solar_dc1_kwh']:9.2f} kWh")
        print(f"AC-(DC0+DC1)  : {s['ac_vs_dc_sum_diff_kwh']:+9.2f} kWh")
        print(f"Grid Out total: {s['grid_out_total_kwh']:9.2f} kWh "
              f"(~{s['grid_out_total_kwh'] / s['solar_total_kwh'] * 100:.0f}% exported)" if s['solar_total_kwh'] else "")
        print(f"Auto-consumed : {s['auto_consumed_total_kwh']:9.2f} kWh "
              f"(solar - export)")
        print(f"missing days: {len(s['missing_days'])} ({format_day_list(s['missing_days'])})")
        print(f"negative deltas (resets): {len(s['negative_days'])} "
              f"({format_day_list([d['date'] for d in s['negative_days']])})")
        print(f"flat days: {len(s['flat_days'])} ({format_day_list(s['flat_days'])})")
        print(f"export > production days: {len(s['export_exceeds_production_days'])}")
        for d in s["export_exceeds_production_days"]:
            print("   ", d["date"], "solar", d["solar_kwh"], "export", d["export_kwh"])
        print(f"unexplained anomalies: {len(s['unknown_anomaly_days'])} "
              f"({format_day_list(s['unknown_anomaly_days'])})")
        print("\nmonth:   solar /    out / auto-consumed")
        for mo, (a, o) in s["monthly"].items():
            print(f"  {mo}  {a:7.2f}  {o:7.2f}  {a - o:8.2f}")
        print(f"daily aggregates (solar / export / auto-consumed): "
              f"{len(s['daily'])} day(s), full table in the JSON report")

    if "coverage" in report:
        print("\n=== coverage probe ===")
        for k, v in report["coverage"].items():
            print(f"  {k:14} {v['covered_samples']:>3}/{v['total_samples']} "
                  f"({v['coverage_pct']:5.1f}%)  {v['first']} -> {v['last']}")

    if "hourly" in report:
        h = report["hourly"]
        print("\n=== hourly coverage (per-hour slots of the window) ===")
        for key in ("grid_in", "grid_out", "solar_total", "solar_dc0",
                    "solar_dc1", "tempo", *CONSUMER_KEYS):
            if key not in h:
                continue
            m = h[key]
            status = "PASS" if m["ok"] else "FAIL"
            detail = (f"{m['coverage_pct']:6.2f}%  "
                      f"total={m['total_kwh']:9.2f} kWh")
            if key in ("grid_in", "grid_out") or key in CONSUMER_KEYS:
                n_win = len(m["gap_windows"])
                detail += (f"  missing={len(m['missing_slots'])} "
                           f"({n_win} window(s)")
                if key in CONSUMER_KEYS:
                    detail += (f", {len(m['missing_days'])} day(s)"
                               f", resets={len(m['resets'])}"
                               f", flat={m['flat_hours']}"
                               f"/{m['comparable_hours']}h")
                detail += ")"
                for w in m["gap_windows"][:3]:
                    detail += f"  [{w[0]} -> {w[1]}]"
            elif key in ("solar_total", "solar_dc0", "solar_dc1"):

                detail += (f"  daylight-missing={len(m['daylight_missing_slots'])}"
                           f"  night-silence={m['night_missing_count']}"
                           f"  over-max={len(m['over_max_hours'])}")
            else:
                detail += (f"  inactive-full-days={m['inactive_day_count']}"
                           f"  stream-gap={m['stream_gap_missing']}")
            print(f"  {key:16} {detail}  -> {status}")
        if "teleinfo_stream_gap" in h:
            tsg = h["teleinfo_stream_gap"]
            print(f"  teleinfo stream-gap hours: {tsg['count']}"
                  f" ({len(tsg['windows'])} window(s)) "
                  f"-> {'PASS' if tsg['ok'] else 'FAIL'}")
            if tsg["windows"]:
                limit = 5
                for w in tsg["windows"][:limit]:
                    print(f"     {w[0]} -> {w[1]}")
                if len(tsg["windows"]) > limit:
                    print(f"     (+{len(tsg['windows']) - limit} more windows)")
        for key in ("solar_total", "solar_dc0", "solar_dc1"):
            m = h[key]
            if m["over_max_hours"]:
                print(f"\n  solar >{m['solar_max_kwh_per_hour']} kWh/h "
                      f"(impossible for 1 kW panels) in {len(m['over_max_hours'])} hours:")
                for o in m["over_max_hours"][:15]:
                    print(f"     {o['utc']}  {o['kwh']:.3f} kWh")
        if not h["ok"]:
            print("\n  -> FAIL: some hourly gaps are not acknowledged in "
                  "energy-config.yaml `known_anomalies`; the full lists are "
                  "in the JSON report.")


def render_html(report: dict, path: str, threshold_pct: float) -> None:
    e = html.escape
    rows = []
    title = "Energy data-quality report"

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

    def bars(pairs, unit=""):
        """Horizontal bar chart; pairs = [(label, value), ...]."""
        maxv = max((abs(v) for _, v in pairs), default=0.0) or 1.0
        out = ['<div class="bars">']
        for label, v in pairs:
            pct = abs(v) / maxv * 100
            fill = "bar-fill neg" if v < 0 else "bar-fill"
            out.append(
                f'<div class="bar-row">'
                f'<span class="bar-label">{e(label)}</span>'
                f'<div class="bar-track"><div class="{fill}" '
                f'style="width:{pct:.1f}%"></div></div>'
                f'<span class="bar-value">{v:.2f} {unit}</span>'
                f'</div>')
        out.append('</div>')
        return "".join(out)

    def progress(pct: float) -> str:
        p = min(max(pct, 0.0), 100.0)
        cls = "progress-fill"
        if p < 70:
            cls += " bad"
        elif p < 90:
            cls += " warn"
        return (f'<div class="progress"><div class="{cls}" '
                f'style="width:{p:.1f}%"></div></div>')

    def grouped_bars(groups):
        """Vertical grouped bar chart; groups = [(x_label, [(bar, value)]), ...]."""
        maxv = max((abs(v) for _, bs in groups for _, v in bs),
                   default=0.0) or 1.0
        classes = {name for _, bs in groups for name, _ in bs}
        out = ['<div class="chart">']
        out.append('<div class="chart-groups">')
        for x, bs in groups:
            out.append('<div class="chart-group">')
            out.append('<div class="chart-bars">')
            for label, v in bs:
                h = abs(v) / maxv * 100
                out.append(
                    f'<div class="chart-bar {e(label)}" '
                    f'style="height:{h:.1f}%" '
                    f'title="{e(label)}: {v:.2f} kWh">'
                    f'<span>{v:.0f}</span></div>')
            out.append('</div>')
            out.append(f'<div class="chart-month">{e(x)}</div>')
            out.append('</div>')
        out.append('</div>')
        legend = "".join(
            f'<span><i class="dot {e(name)}"></i>{e(name)}</span>'
            for name in sorted(classes))
        out.append(f'<div class="chart-legend">{legend}</div>')
        out.append('</div>')
        return "".join(out)

    def line_chart(x_labels, series, tooltip_series=None, width=620,
                   height=220):
        """SVG line chart; series = [(name, color, [values]), ...] aligned to
        x_labels. Negative values plot below the zero baseline.

        tooltip_series = [(name, color, [values]), ...] supplies extra series
        that are NOT drawn but are shown in the per-month hover tooltip.
        """
        drawn = list(series)
        tips = (drawn
                + [(n, c, vs) for n, c, vs in (tooltip_series or [])])
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
        # so thin the tick labels out; the last one is always kept.
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

    if "identity" in report:
        id_ = report["identity"]
        diff_ok = (id_["diff_pct"] is None
                   or abs(id_["diff_pct"]) <= threshold_pct * 100)
        rows.append('<div class="cards">' + "".join([
            kpi("Grid In", f"{id_['grid_in_total_kwh']:.2f} kWh", "year total"),
            kpi("Sum Tempo", f"{id_['tempo_sum_total_kwh']:.2f} kWh",
                "any counters subset"),
            kpi("Diff", f"{id_['diff_kwh']:+.2f} kWh",
                f"{id_['diff_pct']:+.2f} %",
                "card ok" if diff_ok else "card bad"),
        ]) + '</div>')
        rows.append("<h2>Identity: Grid In vs Sum(Tempo)</h2>")
        rows.append(bars([
            ("Grid In", id_["grid_in_total_kwh"]),
            ("Sum Tempo", id_["tempo_sum_total_kwh"]),
        ], "kWh"))
        rows.append("<h3>Monthly grid / tempo (kWh)</h3>")
        groups = []
        for mo, (g, s) in id_["monthly"].items():
            short = datetime.datetime.strptime(mo, "%Y-%m").strftime("%b %y")
            groups.append((short, [("grid", g), ("tempo", s)]))
        rows.append(grouped_bars(groups))
        miss = report["missing"]
        rows.append(collapsed(
            f"Incomplete days, no Tempo register reported ({len(id_['incomplete_days'])})",
            table(["Date", "Grid kWh", "Missing counters"],
                  [[d["date"], d["grid_kwh"], ", ".join(d["missing"])]
                   for d in id_["incomplete_days"]])))
        rows.append(collapsed(
            f"Diverging days ({len(id_['diverging_days'])} "
            f"> {threshold_pct:.0%} deviation)",
            table(["Date", "Grid kWh", "Tempo kWh"],
                  [[d["date"], d["grid_kwh"], d["tempo_kwh"]]
                   for d in id_["diverging_days"]])))
        rows.append(collapsed(
            "Silent days per tempo counter",
            table(["Counter", "Silent days"],
                  sorted(miss["silent_days_per_counter"].items()))))
        rows.append(collapsed(
            "Deficit kWh by silent-counter combination",
            table(["Silent combination", "Deficit kWh"],
                  miss["deficit_by_missing"].items())))

    if "solar" in report:
        s = report["solar"]
        auto = s["auto_consumed_total_kwh"]
        rows.append('<div class="cards">' + "".join([
            kpi("Solar AC", f"{s['solar_total_kwh']:.2f} kWh", "produced"),
            kpi("Grid Out", f"{s['grid_out_total_kwh']:.2f} kWh", "exported"),
            kpi("Auto-consumed", f"{auto:.2f} kWh", "solar - export"),
            kpi("AC vs DC sum", f"{s['ac_vs_dc_sum_diff_kwh']:+.2f} kWh",
                "consistency",
                "card ok" if s["ok"] else "card bad"),
        ]) + '</div>')
        rows.append("<h2>Solar consistency</h2>")
        rows.append(bars([
            ("Solar AC", s["solar_total_kwh"]),
            ("Solar DC0", s["solar_dc0_kwh"]),
            ("Solar DC1", s["solar_dc1_kwh"]),
            ("Grid Out", s["grid_out_total_kwh"]),
            ("Auto-consumed", auto),
        ], "kWh"))
        rows.append("<h3>Monthly solar / grid / export / auto-consumed (kWh)</h3>")
        labels, solar_vals, export_vals, auto_vals = [], [], [], []
        for mo, (a, o) in s["monthly"].items():
            labels.append(datetime.datetime.strptime(mo, "%Y-%m")
                          .strftime("%b %y"))
            solar_vals.append(a)
            export_vals.append(-o)
            auto_vals.append(a - o)
        series = [
            ("Solar", "#e6b800", solar_vals),
            ("Auto-consumption", "#2e9e5b", auto_vals),
        ]
        tooltip_extra = [
            ("Export", "#4a90d9", export_vals),
        ]
        id_monthly = (report.get("identity") or {}).get("monthly") or {}
        grid_vals = [id_monthly.get(mo, [None, None])[0] for mo in s["monthly"]]
        if len(grid_vals) == len(solar_vals) \
                and all(v is not None for v in grid_vals):
            series.append(("Grid In", "#8e44ad", grid_vals))
        rows.append(line_chart(labels, series,
                               tooltip_series=tooltip_extra))
        daily = s.get("daily") or []
        if daily:
            # Grid In per day lives in the identity probe, merged here on the
            # date like the monthly chart does.
            id_daily = {d["date"]: d["grid_kwh"] for d in
                        (report.get("identity") or {}).get("daily", [])}
            rows.append("<h3>Daily solar / grid / export / auto-consumed "
                        "(kWh)</h3>")
            d_labels, d_solar, d_export, d_auto, d_grid = [], [], [], [], []
            for d in daily:
                # A day with no solar or no export sample is left out of the
                # chart rather than plotted as a fake 0 kWh dip.
                if d["solar_kwh"] is None or d["export_kwh"] is None:
                    continue
                d_labels.append(d["date"][5:])
                d_solar.append(d["solar_kwh"])
                d_export.append(-d["export_kwh"])
                d_auto.append(d["auto_consumed_kwh"])
                d_grid.append(id_daily.get(d["date"]))
            if len(d_labels) > 1:
                d_series = [("Solar", "#e6b800", d_solar),
                            ("Auto-consumption", "#2e9e5b", d_auto)]
                if all(v is not None for v in d_grid):
                    d_series.append(("Grid In", "#8e44ad", d_grid))
                rows.append(line_chart(
                    d_labels, d_series,
                    tooltip_series=[("Export", "#4a90d9", d_export)]))
            else:
                rows.append(f"<p>{len(daily)} day(s) aggregated</p>")
            num = lambda v: "-" if v is None else f"{v:.2f}"
            rows.append(collapsed(
                f"Daily solar / grid / export / auto-consumed "
                f"({len(daily)} days)",
                table(["Date", "Solar kWh", "Grid In kWh", "Export kWh",
                       "Auto-consumed kWh"],
                      [[d["date"], num(d["solar_kwh"]),
                        num(id_daily.get(d["date"])), num(d["export_kwh"]),
                        num(d["auto_consumed_kwh"])] for d in daily])))
        anomal = []
        for d in s["missing_days"]:
            anomal.append([d, "missing", "", ""])
        for d in s["negative_days"]:
            anomal.append([d["date"], "negative delta", f"{d['delta_kwh']:.3f}", ""])
        for d in s["flat_days"]:
            anomal.append([d, "flat", "", ""])
        for d in s["export_exceeds_production_days"]:
            anomal.append([d["date"], "export > production",
                           d["solar_kwh"], d["export_kwh"]])
        rows.append(collapsed(
            f"Anomalies ({len(anomal)})",
            table(["Date", "Kind", "Solar kWh", "Export kWh"], anomal)))

    if "coverage" in report:
        rows.append("<h2>Coverage probe</h2>")
        rows.append('<div class="bars">')
        for k, v in report["coverage"].items():
            rows.append(
                f'<div class="bar-row"><span class="bar-label">{e(k)}</span>'
                f'<div class="bar-track">{progress(v["coverage_pct"])}</div>'
                f'<span class="bar-value">{v["covered_samples"]}/{v["total_samples"]} '
                f'({v["coverage_pct"]:.1f}%)</span></div>')
        rows.append('</div>')
        rows.append(collapsed(
            "Sampled values (first / last)",
            table(["Metric", "Coverage", "First", "Last"], [
                [k, f"{v['covered_samples']}/{v['total_samples']} "
                    f"({v['coverage_pct']}%)", v["first"], v["last"]]
                for k, v in report["coverage"].items()])))

    if "hourly" in report:
        h = report["hourly"]
        rows.append("<h2>Hourly coverage</h2>")
        rows.append(f'<p class="{"ok" if h["ok"] else "bad"}">Overall: '
                    f"{'PASS' if h['ok'] else 'FAIL'}</p>")
        metrics = [k for k in ("grid_in", "grid_out", "solar_total",
                               "solar_dc0", "solar_dc1", "tempo", *CONSUMER_KEYS)
                   if k in h]
        rows.append('<div class="cards">' + "".join([
            kpi(k.replace("_", " ").title(),
                f"{h[k]['coverage_pct']:.2f} %",
                "PASS" if h[k]["ok"] else "FAIL",
                "card ok" if h[k]["ok"] else "card bad")
            for k in metrics]) + '</div>')
        rows.append('<div class="bars">')
        for k in metrics:
            m = h[k]
            rows.append(
                f'<div class="bar-row"><span class="bar-label">{e(k)}</span>'
                f'<div class="bar-track">{progress(m["coverage_pct"])}</div>'
                f'<span class="bar-value">{m["coverage_pct"]:.2f}% — '
                f'{e(_hourly_gap_summary(k, m))}</span></div>')
        rows.append('</div>')

        rows.append(collapsed(
            "Per-metric detail (energy totals, gap counts)",
            table(["Metric", "Coverage", "Total kWh", "Gaps"],
                  [[k, f"{h[k]['coverage_pct']:.2f}%", h[k]["total_kwh"],
                    _hourly_gap_summary(k, h[k])] for k in metrics])))
        if "teleinfo_stream_gap" in h:
            tsg = h["teleinfo_stream_gap"]
            rows.append(collapsed(
                f"Teleinfo stream-gap windows ({tsg['count']} hours)",
                table(["Start", "End"], tsg["windows"])))
        consumers = [k for k in metrics if k in CONSUMER_KEYS]
        if consumers:
            rows.append(collapsed(
                "Car / water heater / heaters counters: missing samples, "
                "resets, flat hours (same garage power meter as grid in/out)",
                table(["Metric", "Total kWh", "Missing hours", "Missing days",
                       "Gap windows", "Resets", "Flat hours"],
                      [[k, h[k]["total_kwh"], len(h[k]["missing_slots"]),
                        ", ".join(h[k]["missing_days"]) or "-",
                        "; ".join(f"{a} -> {b}"
                                  for a, b in h[k]["gap_windows"]) or "-",
                        len(h[k]["resets"]),
                        f"{h[k]['flat_hours']}/{h[k]['comparable_hours']}"]
                       for k in consumers])))
            reset_rows = [[k, r["utc"], r["kwh"]] for k in consumers
                          for r in h[k]["resets"]]
            if reset_rows:
                rows.append(collapsed(
                    "High consumers: counter resets (negative hourly "
                    "increments)",
                    table(["Metric", "UTC", "kWh"], reset_rows)))
        for key in ("solar_total", "solar_dc0", "solar_dc1"):
            m = h.get(key)
            if m and m["over_max_hours"]:
                rows.append(collapsed(
                    f"{key}: hourly increments beyond the "
                    f"{m['solar_max_kwh_per_hour']} kWh/h physical cap "
                    f"({len(m['over_max_hours'])})",
                    table(["UTC", "kWh"], [[o["utc"], f"{o['kwh']:.3f}"]
                                           for o in m["over_max_hours"]])))

    w = report["window"]
    css = """
 body { font-family: -apple-system, 'Segoe UI', Roboto, Arial, sans-serif;
        margin: 2rem; color: #222; background: #fafafa; }
 h1 { border-bottom: 2px solid #333; padding-bottom: .3rem; }
 h2 { margin-top: 2rem; border-bottom: 1px solid #ccc; padding-bottom: .2rem; }
 h3 { margin-top: 1.2rem; }
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
 .bars { margin: .4rem 0 1rem; }
 .bar-row { display: flex; align-items: center; gap: .6rem; margin: .3rem 0; }
 .bar-label { flex: 0 0 230px; text-align: right; font-size: .8rem; color: #444;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
 .bar-track { flex: 1; background: #eee; border-radius: 4px; height: 16px;
              overflow: hidden; }
 .bar-fill { height: 100%; background: linear-gradient(90deg, #4a90d9, #6fb1e8); }
 .bar-fill.neg { background: linear-gradient(90deg, #d64545, #e88f6f); }
 .bar-value { flex: 0 0 160px; font-size: .8rem; color: #222; }
 .progress { flex: 1; background: #eee; border-radius: 4px; height: 16px;
             overflow: hidden; }
 .progress-fill { height: 100%; background: linear-gradient(90deg, #2e9e5b, #67c98c); }
 .progress-fill.warn { background: linear-gradient(90deg, #d99a2b, #ecc06f); }
 .progress-fill.bad { background: linear-gradient(90deg, #d64545, #e88f6f); }
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
 .chart-bar { position: relative; width: min(22px, 55%); display: flex;
              align-items: flex-start; justify-content: center;
              border-radius: 3px 3px 0 0; }
 .chart-bar span { font-size: .65rem; font-weight: 600; color: #fff;
                   padding-top: 2px; text-shadow: 0 0 2px rgba(0,0,0,.5); }
 .chart-bar.grid { background: linear-gradient(180deg, #4a90d9, #2e6bb0); }
 .chart-bar.tempo { background: linear-gradient(180deg, #e0a04e, #c07f2b); }
 .chart-month { text-align: center; font-size: .68rem; color: #555;
                margin-top: .25rem; white-space: nowrap; }
 .chart-legend { display: flex; gap: 1.2rem; margin-top: .4rem;
                 font-size: .8rem; color: #333; }
 .chart-legend .dot { display: inline-block; width: 12px; height: 12px;
                      border-radius: 3px; margin-right: .35rem;
                      vertical-align: -1px; }
 .chart-legend .dot.grid { background: #4a90d9; }
 .chart-legend .dot.tempo { background: #e0a04e; }
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
 p.ok { color: #2e9e5b; font-weight: 600; }
 p.bad { color: #d64545; font-weight: 600; }
 table { border-collapse: collapse; margin: 1rem 0; width: 100%; }
 th, td { border: 1px solid #aaa; padding: .3rem .7rem; text-align: left; }
 th { background: #eee; }
"""
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{e(title)}</title>
<style>{css}</style></head><body>
<h1>{e(title)}</h1>
<p>VM: {e(report['url'])}<br>
Window: {e(w['start'])} -> {e(w['end'])}<br>
Generated: {e(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}</p>
{''.join(rows)}
</body></html>
"""
    with open(path, "w") as f:
        f.write(page)
    print(f"HTML report saved: {path}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Data-quality report for the energy analysis window")
    ap.add_argument("-u", "--url", default=None,
                    help="VictoriaMetrics base URL (default from config)")
    ap.add_argument("--start", default=None,
                    help="window start YYYY-MM-DD (default from config)")
    ap.add_argument("--end", default=None,
                    help="window end YYYY-MM-DD (default from config)")
    ap.add_argument("--config", default="scripts/energy-config.yaml",
                    help="path to energy-config.yaml")
    ap.add_argument("--output", default="scripts/output/data-quality.json",
                    help="JSON report output path")
    ap.add_argument("--checks", choices=["all", "identity", "solar", "hourly"],
                    default="all", help="which checks to run (default all)")
    ap.add_argument("--html", default=None,
                    help="also render an HTML report to this path")
    ap.add_argument("--threshold", type=float, default=0.05,
                    help="identity divergence threshold (default 0.05)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--no-coverage", action="store_true",
                    help="skip the monthly coverage probe")
    args = ap.parse_args()

    cfg = load_config(args.config)
    url = args.url or cfg.get("vm", {}).get("url", "http://localhost:8428")
    start, end, n_days = window_days(cfg)
    if args.start:
        start = datetime.datetime.fromisoformat(f"{args.start}T00:00:00+00:00")
    if args.end:
        end = datetime.datetime.fromisoformat(f"{args.end}T00:00:00+00:00")
        n_days = (end - start).days

    print(f"VM: {url}")
    print(f"Window: {start:%Y-%m-%d} -> {end:%Y-%m-%d} ({n_days} days)")

    print("Sampling daily max_over_time per counter...")

    report = {"window": {"start": start.isoformat(), "end": end.isoformat()},
              "url": url}
    checks_ok: list[bool] = []

    run_identity = args.checks in ("all", "identity")
    run_solar = args.checks in ("all", "solar")
    run_hourly = args.checks in ("all", "hourly")

    if run_identity:
        times = boundary_times(start, n_days)
        grid_delta = sample_deltas(
            url, cfg["metrics"]["grid_in"]["selector"], times, args.workers)
        tempo_delta = {}
        for key in TEMPO_KEYS:
            tempo_delta[key] = sample_deltas(
                url, cfg["metrics"][key]["selector"], times, args.workers)

        identity = identity_check(url, cfg, start, n_days,
                                  workers=args.workers,
                                  grid_delta=grid_delta,
                                  tempo_delta=tempo_delta)
        tempo_scale = {k: cfg["metrics"][k]["scale_to_kwh"]
                       for k in TEMPO_KEYS}
        missing = missing_classification(grid_delta, tempo_scale,
                                         tempo_delta, start)
        report["identity"] = identity
        report["missing"] = missing
        identity_pass = (identity["diff_pct"] is None
                         or abs(identity["diff_pct"]) <= args.threshold * 100)
        checks_ok.append(identity_pass)
        print(f"\nIdentity threshold: |{identity['diff_pct']}%| <= "
              f"{args.threshold * 100}% -> "
              f"{'PASS' if identity_pass else 'FAIL'}")

    if run_solar:
        solar = solar_check(url, cfg, start, n_days, workers=args.workers)
        report["solar"] = solar
        checks_ok.append(solar["ok"])
        print(f"\nSolar consistency: {'PASS' if solar['ok'] else 'FAIL'}")

    if run_hourly:
        hourly = hourly_check(url, cfg, start, n_days)
        report["hourly"] = hourly
        checks_ok.append(hourly["ok"])
        print(f"\nHourly coverage: {'PASS' if hourly['ok'] else 'FAIL'}")

    if not args.no_coverage:
        print("Monthly coverage probe...")
        report["coverage"] = coverage_probe(url, cfg, start, n_days)

    print_report(report, args.threshold * 100)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved: {args.output}")

    if args.html:
        os.makedirs(os.path.dirname(args.html) or ".", exist_ok=True)
        render_html(report, args.html, args.threshold * 100)

    return 0 if all(checks_ok) else 1


if __name__ == "__main__":
    sys.exit(main())