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
     days, and "exported more than produced" anomalies,
  6. hourly data-quality check (per-hour slots): coverage per metric, grid
     in/out gap windows, solar daylight gaps vs expected night silence, and
     solar hourly increments beyond the physical panel cap (1 kW panels ->
     at most ~1 kWh/h; a larger delta means a counter jump / corruption),
  7. optional monthly coverage probe per metric,
  8. optional HTML report rendering.

Usage:
    01-data-quality.py [-u VM_URL] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                       [--config energy-config.yaml] [--output data-quality.json]
                       [--checks all|identity|solar|hourly]
                       [--correct-solar OUTPUT_PREFIX]
                       [--threshold 0.05] [--workers 16] [--html report.html]
                       [--no-coverage]
"""

import argparse
import csv
import datetime
import html
import json
import math
import os
import statistics
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
        if missing:
            incomplete_days.append({"date": day, "grid_kwh": round(g, 2),
                                    "missing": missing})
        elif abs(g - s) > 0.05 * max(g, 0.001):
            diverging_days.append({"date": day, "grid_kwh": round(g, 2),
                                   "tempo_kwh": round(s, 2)})
        mo = day[:7]
        monthly[mo][0] += g
        monthly[mo][1] += s

    diff = tot_grid - tot_tempo
    return {
        "grid_in_total_kwh": round(tot_grid, 2),
        "tempo_sum_total_kwh": round(tot_tempo, 2),
        "diff_kwh": round(diff, 2),
        "diff_pct": round(diff / tot_grid * 100, 2) if tot_grid else None,
        "incomplete_days": incomplete_days,
        "diverging_days": diverging_days,
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
        "missing_days": missing,
        "negative_days": negative,
        "flat_days": flat,
        "export_exceeds_production_days": imports,
        "unknown_anomaly_days": unknown_imports + unknown_flat,
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
    * tempo_*: missing slots per counter; fully-silent days (inactive Tempo
      color) are expected and not reported as gaps, the shared "stream down"
      hours (all counters silent) and per-counter holes are surfaced.

    Gaps recorded in `known_anomalies` (grid_missing_hour, solar_missing_hour,
    solar_over_max_hour) are acknowledged and do not fail the check; the
    report still lists them.
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
    known_counter = known.get("teleinfo_counter_gap_hour", {})

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

    # ----- tempo counters -----
    stream_slots = {b for b in boundaries
                    if all(b not in hourly[k] for k in TEMPO_KEYS)}
    for key in TEMPO_KEYS:
        vals = hourly[key]
        missing = [b for b in boundaries if b not in vals]
        per_day: dict[str, int] = defaultdict(int)
        for b in boundaries:
            if b in vals:
                per_day[day_label(b)] += 1
        inactive = sorted(d for d in per_day if per_day[d] == 0)
        stream = [b for b in missing if b in stream_slots
                  and iso_label(b) not in known_stream]
        counter_specific = [b for b in missing
                            if b not in stream_slots
                            and per_day.get(day_label(b), 0) > 0
                            and iso_label(b) not in known_counter.get(key, [])]
        kwh = sum(d for _, d in increments(vals)) * metrics[key]["scale_to_kwh"]
        ok = not stream and not counter_specific
        ok_flags.append(ok)
        report[key] = {
            "ok": ok,
            "coverage_pct": round(
                (len(boundaries) - len(missing)) / len(boundaries) * 100, 2),
            "total_kwh": round(kwh, 2),
            "inactive_day_count": len(inactive),
            "inactive_days": inactive,
            "stream_gap_missing": len(stream),
            "counter_specific_missing": len(counter_specific),
            "counter_specific_windows": gap_windows(counter_specific),
        }
        print(f"  {key}: {report[key]['coverage_pct']}% covered, "
              f"inactive days={len(inactive)}, "
              f"stream-gap hrs={len(stream)}, "
              f"counter-specific hrs={len(counter_specific)} -> "
              f"{'PASS' if ok else 'FAIL'}")

    stream_gap_list = sorted(stream_slots
                             - {b for b in boundaries
                                if iso_label(b) in known_stream})
    report["teleinfo_stream_gap"] = {
        "hours": [iso_label(b) for b in stream_gap_list],
        "count": len(stream_gap_list),
        "windows": gap_windows(stream_gap_list),
        "ok": not stream_gap_list,
    }
    report["ok"] = all(ok_flags)
    return report


def correct_solar(url: str, cfg: dict, start: datetime.datetime,
                  n_days: int, output_prefix: str) -> None:
    """Backward-redistribute opendtu catch-up flushes.

    opendtu occasionally freezes during daylight (repeated identical cumulative
    YieldTotal frames) and later flushes the accumulated production into a
    single hourly jump that exceeds the physical 1 kW panel cap. The cumulative
    counters are the source of truth *after* the flush (the energy was really
    produced), so the jump is re-sliced back over the frozen/failed daylight
    hours following an hour-of-day solar production template. The sum over
    each corrected window is preserved (mass conserving), the counter continues
    where it was after the flush, and raw VictoriaMetrics data is never
    modified. Outputs a corrected hourly series CSV and a corrections log
    (raw vs corrected vs confidence per hour).
    """
    metrics = cfg["metrics"]
    hq = cfg["hourly_quality"]
    solar_max = hq["solar_max_kwh_per_hour"]
    solar_tol = hq["solar_max_tolerance_kwh"]
    over_max = solar_max + solar_tol
    flat_kwh = 0.05
    dl_start, dl_end = hq["solar_daylight_hours"]
    solar_keys = ("solar_total", "solar_dc0", "solar_dc1")

    n_hours = n_days * 24
    end = start + datetime.timedelta(days=n_days)
    boundaries = [int((start + datetime.timedelta(hours=i)).timestamp())
                  for i in range(n_hours + 1)]

    def hour_of(ts: int) -> int:
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).hour

    def iso_label(ts: int) -> str:
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

    def day_label(ts: int) -> str:
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).strftime("%Y-%m-%d")

    def is_daylight(ts: int) -> bool:
        return dl_start <= hour_of(ts) < dl_end

    print("Fetching solar series for the correction pass...")
    hourly = {key: fetch_hourly_series(url, metrics[key]["selector"], start,
                                       end)
              for key in solar_keys}

    def slot_increments(vals: dict) -> list[float | None]:
        inc: list[float | None] = [None] * n_hours
        for i in range(n_hours):
            a = boundaries[i]
            b = boundaries[i + 1]
            if a in vals and b in vals:
                inc[i] = vals[b] - vals[a]
        return inc

    def present_hops(vals: dict) -> list[tuple[int, int, int, float]]:
        idx = [i for i in range(n_hours) if boundaries[i] in vals]
        hops = []
        for prev, cur in zip(idx, idx[1:]):
            hops.append((prev, cur, cur - prev,
                         vals[boundaries[cur]] - vals[boundaries[prev]]))
        return hops

    def find_windows(vals: dict, inc: list[float | None]) -> list[dict]:
        wins = []
        for prev, cur, hspan, delta in present_hops(vals):
            if delta <= over_max:
                continue
            if hspan > 1:
                seg = list(range(prev, cur))
                release = cur - 1
            else:
                release = prev
                s = prev - 1
                while (s >= 0 and inc[s] is not None
                       and 0 <= inc[s] <= flat_kwh
                       and is_daylight(boundaries[s])):
                    s -= 1
                seg = list(range(s + 1, prev + 1))
            raw = sum(x for x in (inc[i] for i in seg) if x is not None)
            wins.append({"seg": seg, "release": release,
                         "delta": delta, "raw_sum": raw if raw else delta})
        wins.sort(key=lambda w: w["seg"][0])
        merged = []
        for w in wins:
            if merged and w["seg"][0] <= merged[-1]["seg"][-1] + 1:
                merged[-1]["seg"] = list(
                    range(merged[-1]["seg"][0],
                          max(w["seg"][-1], merged[-1]["seg"][-1]) + 1))
                merged[-1]["raw_sum"] += w["raw_sum"]
                merged[-1]["release"] = max(merged[-1]["release"], w["release"])
            else:
                merged.append({"seg": list(w["seg"]), "release": w["release"],
                               "raw_sum": w["raw_sum"]})
        return merged

    def build_template(inc: list[float | None], polluted: set) -> tuple[list[float], str]:
        by_hour: list[list[float]] = [[] for _ in range(24)]
        for i in range(n_hours):
            if inc[i] is None:
                continue
            if not is_daylight(boundaries[i]):
                continue
            if -flat_kwh <= inc[i] <= over_max:
                d = day_label(boundaries[i])
                if d not in polluted:
                    by_hour[hour_of(boundaries[i])].append(inc[i])
        profile: list[float | None] = [None] * 24
        for h in range(24):
            if by_hour[h]:
                profile[h] = statistics.median(by_hour[h])
        if profile.count(None) <= 20:
            arr = [max(p, 0.0) if p is not None else 0.0 for p in profile]
            for h in range(24):
                if not (dl_start <= h < dl_end):
                    arr[h] = 0.0
            kern = [math.exp(-(d * d) / 2.0) for d in range(-3, 4)]
            ks = sum(kern)
            out = []
            for h in range(24):
                s = 0.0
                for off, w in zip(range(-3, 4), kern):
                    s += w * arr[(h + off) % 24]
                out.append(s / ks)
            for h in range(24):
                if not (dl_start <= h < dl_end):
                    out[h] = 0.0
            return out, "median"
        gauss = []
        for h in range(24):
            gauss.append(math.exp(-((h - 12.0) ** 2) / (2 * 2.5 ** 2))
                         if dl_start <= h < dl_end else 0.0)
        return gauss, "gaussian"

    def reslice(seg: list[int], raw_sum: float, templ: list[float]) -> tuple[list[float], list[int]]:
        w = [templ[hour_of(boundaries[i])] for i in seg]
        tot_w = sum(w)
        if tot_w <= 1e-9:
            w = [1.0 if is_daylight(boundaries[i]) else 0.0 for i in seg]
            tot_w = sum(w)
        if tot_w <= 1e-9:
            return [], []
        return [raw_sum * w[k] / tot_w for k in range(len(seg))], w

    all_rows: list[tuple] = []
    corrections: list[dict] = []
    summary: dict = {}

    for key in solar_keys:
        vals = hourly[key]
        inc = slot_increments(vals)
        wins = find_windows(vals, inc)
        polluted = {day_label(boundaries[i]) for w in wins for i in w["seg"]}
        templ, kind = build_template(inc, polluted)

        corrected = list(inc)
        conf = [1.0 if d is not None else 0.0 for d in inc]
        flag = ["ok" if d is not None else "missing" for d in inc]

        for w in wins:
            seg = list(w["seg"])
            raw_sum = w["raw_sum"]
            guard = 0
            while guard < 72:
                new, weights = reslice(seg, raw_sum, templ)
                if not new:
                    break
                if max(new) <= over_max + 1e-9:
                    break
                s0 = seg[0]
                if (s0 < 1 or inc[s0 - 1] is None
                        or not is_daylight(boundaries[s0 - 1])):
                    break
                seg.insert(0, s0 - 1)
                if inc[s0 - 1] is not None and inc[s0 - 1] >= 0:
                    raw_sum += inc[s0 - 1]
                guard += 1
            new, weights = reslice(seg, raw_sum, templ)
            if not new:
                continue
            for k, i in enumerate(seg):
                corrected[i] = new[k]
                conf[i] = 0.6
                flag[i] = "flush" if i == w["release"] else "redist"
            release_kwh = new[seg.index(w["release"])]
            corrections.append({
                "metric": key,
                "release_hour": iso_label(boundaries[w["release"]]),
                "window_start": iso_label(boundaries[seg[0]]),
                "window_end": iso_label(boundaries[seg[-1] + 1]),
                "slots": len(seg),
                "window_kwh": round(w["raw_sum"], 3),
                "release_raw_kwh": round(inc[w["release"]], 3)
                if inc[w["release"]] is not None else None,
                "release_kwh": round(release_kwh, 3),
                "per_slot_max_kwh": round(max(new), 3),
                "template": kind,
            })
        max_inc = max((c for c in corrected if c is not None), default=0.0)
        true_kwh = max(vals.values()) - min(vals.values())
        corr_kwh = sum(c or 0.0 for c in corrected)
        missing_slots = sum(1 for d in inc if d is None)
        summary[key] = {
            "events": len(wins),
            "corrected_slots": sum(1 for i in range(n_hours)
                                   if corrected[i] is not None and flag[i] != "ok"),
            "max_corrected_kwh": round(max_inc, 3),
            "corrected_total_kwh": round(corr_kwh, 2),
            "counter_total_kwh": round(true_kwh, 2),
            "unallocated_kwh_in_missing_hours": round(max(true_kwh - corr_kwh, 0.0), 2),
            "missing_hour_count": missing_slots,
        }
        print(f"  {key}: {len(wins)} flush window(s), "
              f"max corrected hourly increment = {max_inc:.3f} kWh/h "
              f"(cap {over_max:.2f}), "
              f"{missing_slots} missing hours left as-is")
        for i in range(n_hours):
            all_rows.append((key, iso_label(boundaries[i]), inc[i],
                             corrected[i], conf[i], flag[i]))

    os.makedirs(os.path.dirname(output_prefix) or ".", exist_ok=True)
    with open(f"{output_prefix}-hourly.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "utc_hour", "raw_kwh", "corrected_kwh",
                    "confidence", "flag"])
        for key, utc, raw, corr, c, fl in all_rows:
            w.writerow([key, utc,
                        "" if raw is None else round(raw, 4),
                        "" if corr is None else round(corr, 4),
                        round(c, 3), fl])
    print(f"Corrected hourly series: {output_prefix}-hourly.csv")

    with open(f"{output_prefix}-corrections.json", "w") as f:
        json.dump({"window": {"start": start.isoformat(), "end": end.isoformat()},
                   "summary": summary, "events": corrections}, f, indent=2)
    print(f"Corrections log:        {output_prefix}-corrections.json")


def format_day_list(days: list[str], limit: int = 12) -> str:
    if not days:
        return "-"
    shown = days[:limit]
    suffix = f" (+{len(days) - limit} more)" if len(days) > limit else ""
    return ", ".join(shown) + suffix


def _hourly_gap_summary(key: str, m: dict) -> str:
    if key in ("grid_in", "grid_out"):
        return f"{len(m['missing_slots'])} missing/silent hours"
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
        parts.append(f"{m['inactive_day_count']} inactive days")
    if m.get("stream_gap_missing"):
        parts.append(f"{m['stream_gap_missing']} stream-gap hrs")
    if m.get("counter_specific_missing"):
        parts.append(f"{m['counter_specific_missing']} per-counter hrs")
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
        print(f"\nincomplete days (some tempo counter silent): "
              f"{len(id_['incomplete_days'])} ({format_day_list([d['date'] for d in id_['incomplete_days']], 5)})")
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

    if "coverage" in report:
        print("\n=== coverage probe ===")
        for k, v in report["coverage"].items():
            print(f"  {k:14} {v['covered_samples']:>3}/{v['total_samples']} "
                  f"({v['coverage_pct']:5.1f}%)  {v['first']} -> {v['last']}")

    if "hourly" in report:
        h = report["hourly"]
        print("\n=== hourly coverage (per-hour slots of the window) ===")
        for key in ("grid_in", "grid_out", "solar_total", "solar_dc0",
                    "solar_dc1", *TEMPO_KEYS):
            if key not in h:
                continue
            m = h[key]
            status = "PASS" if m["ok"] else "FAIL"
            detail = f"{m['coverage_pct']:6.2f}%  total={m['total_kwh']:9.2f} kWh"
            if key in ("grid_in", "grid_out"):
                n_win = len(m["gap_windows"])
                detail += (f"  missing={len(m['missing_slots'])} "
                           f"({n_win} window(s))")
                for w in m["gap_windows"][:3]:
                    detail += f"  [{w[0]} -> {w[1]}]"
            elif key in ("solar_total", "solar_dc0", "solar_dc1"):
                detail += (f"  daylight-missing={len(m['daylight_missing_slots'])}"
                           f"  night-silence={m['night_missing_count']}"
                           f"  over-max={len(m['over_max_hours'])}")
            else:
                detail += (f"  inactive-days={m['inactive_day_count']}"
                           f"  stream-gap={m['stream_gap_missing']}"
                           f"  counter-specific={m['counter_specific_missing']}")
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

    if "identity" in report:
        id_ = report["identity"]
        rows.append(f"<h2>Identity: Grid In vs Tempo</h2>")
        rows.append(table(["Metric", "kWh"], [
            ["Grid In total", id_["grid_in_total_kwh"]],
            ["Tempo sum (any subset)", id_["tempo_sum_total_kwh"]],
            ["Diff", f"{id_['diff_kwh']:+} ({id_['diff_pct']:+}%)"],
        ]))
        rows.append("<h3>Monthly grid / tempo (kWh)</h3>")
        rows.append(table(["Month", "Grid", "Tempo", "Diff"],
                          [[mo, g, s, round(g - s, 2)]
                           for mo, (g, s) in id_["monthly"].items()]))
        rows.append(f"<p>Incomplete days: {len(id_['incomplete_days'])} &nbsp; "
                    f"Diverging ({threshold_pct}%) days: "
                    f"{len(id_['diverging_days'])}</p>")
        miss = report["missing"]
        rows.append("<h3>Silent days per tempo counter</h3>")
        rows.append(table(["Counter", "Silent days"], miss["silent_days_per_counter"].items()))

    if "solar" in report:
        s = report["solar"]
        rows.append("<h2>Solar consistency</h2>")
        rows.append(table(["Metric", "kWh"], [
            ["Solar AC", s["solar_total_kwh"]],
            ["Solar DC0", s["solar_dc0_kwh"]],
            ["Solar DC1", s["solar_dc1_kwh"]],
            ["AC - (DC0+DC1)", s["ac_vs_dc_sum_diff_kwh"]],
            ["Grid Out", s["grid_out_total_kwh"]],
        ]))
        rows.append("<p>Missing days: {} &nbsp; Resets: {} &nbsp; Flat days: "
                    "{} &nbsp; Export&gt;production: {} &nbsp; Unknown "
                    "anomalies: {}</p>".format(
                        len(s["missing_days"]), len(s["negative_days"]),
                        len(s["flat_days"]),
                        len(s["export_exceeds_production_days"]),
                        len(s["unknown_anomaly_days"])))
        rows.append("<h3>Monthly solar / export / auto-consumed (kWh)</h3>")
        rows.append(table(["Month", "Solar", "Grid Out", "Auto-consumed"],
                          [[mo, a, o, round(a - o, 2)]
                           for mo, (a, o) in s["monthly"].items()]))

    if "coverage" in report:
        rows.append("<h2>Coverage probe</h2>")
        rows.append(table(["Metric", "Coverage", "First", "Last"], [
            [k, f"{v['covered_samples']}/{v['total_samples']} "
                f"({v['coverage_pct']}%)", v["first"], v["last"]]
            for k, v in report["coverage"].items()]))

    if "hourly" in report:
        h = report["hourly"]
        rows.append("<h2>Hourly coverage</h2>")
        rows.append(f"<p>Overall: {'PASS' if h['ok'] else 'FAIL'}</p>")
        rows.append(table(["Metric", "Coverage", "Total kWh", "Gaps"],
                          [[key, f"{m['coverage_pct']}%", m["total_kwh"],
                            _hourly_gap_summary(key, m)]
                           for key, m in h.items()
                           if isinstance(m, dict) and "coverage_pct" in m]))
        if "teleinfo_stream_gap" in h:
            tsg = h["teleinfo_stream_gap"]
            rows.append(f"<h3>Teleinfo stream-gap hours ({tsg['count']})</h3>")
            rows.append(table(["Start", "End"], tsg["windows"][:50]))
        for key in ("solar_total", "solar_dc0", "solar_dc1"):
            m = h.get(key)
            if m and m["over_max_hours"]:
                rows.append("<h3>Solar hourly increments beyond the "
                            f"{m['solar_max_kwh_per_hour']} kWh/h physical cap"
                            "</h3>")
                rows.append(table(["UTC", "kWh"], [[o["utc"], o["kwh"]]
                                                   for o in m["over_max_hours"]]))

    w = report["window"]
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{e(title)}</title>
<style>
 body {{ font-family: sans-serif; margin: 2rem; }}
 h1 {{ border-bottom: 2px solid #333; padding-bottom: .3rem; }}
 h2 {{ margin-top: 2rem; }}
 table {{ border-collapse: collapse; margin: 1rem 0; }}
 th, td {{ border: 1px solid #aaa; padding: .3rem .7rem; text-align: left; }}
 th {{ background: #eee; }}
 p {{ color: #444; }}
</style></head><body>
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
    ap.add_argument("--correct-solar", default=None, metavar="PREFIX",
                    help="run the solar catch-up correction pass and write "
                         "PREFIX-hourly.csv + PREFIX-corrections.json "
                         "(raw VM data is never modified)")
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

    if args.correct_solar:
        correct_solar(url, cfg, start, n_days, args.correct_solar)
        return 0

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