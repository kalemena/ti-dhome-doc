"""
vmlib.py - shared VictoriaMetrics helpers for the mathematics/energy analysis.

Phase 1 "shared foundation". Scripts 01..06 build on top of this module so
the raw-read / cache / tariffs logic lives in one place:

  * config loading + window/dates helpers (parse_ts, window_days,
    boundary_times, hour_boundaries),
  * per-day sampling of cumulative counters via instant queries
    (query_max_over_time -> sample_daily_series / sample_deltas),
  * hourly query_range helper (fetch_hourly_series) split per calendar month,
    because a full-year query_range exceeds VictoriaMetrics' ~30M-samples
    per-series cap for the dense zigbee series,
  * delta computation (daily_deltas, hourly_increments),
  * CSV cache (CsvCache): raw per-day and per-hour samples are persisted so
    re-runs are offline, fast and reproducible; the DB is only touched the
    first time,
  * tariff loader (Tariff, load_tariffs): normalizes the five contracts of
    energy-config.yaml (base / blue / tempo / zenfix / zenfix-tempo) into a
    common {metric -> EUR/kWh} representation used by the contract phases,
  * hourly-energy.csv reader (load_hourly_energy) used by phases 3..7 once
    Phase 2 has emitted the canonical 8760-row record.

Counters are always cached in their *native* unit (the raw reading, e.g. Wh
for the teleinfo counters); scaling to kWh happens at consumption time using
the `scale_to_kwh` factors of energy-config.yaml, so a scaling tweak never
invalidates the cache.
"""

import csv
import datetime
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when deps missing
    yaml = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

UTC = datetime.timezone.utc

# The six Tempo teleinfo counters (low/high cost per daily color). These same
# six keys drive the identity check and every contract cost formula.
TEMPO_METRIC_KEYS = [
    "tempo_blue_hc", "tempo_blue_hp",
    "tempo_white_hc", "tempo_white_hp",
    "tempo_red_hc", "tempo_red_hp",
]
HP_METRIC_KEYS = [k for k in TEMPO_METRIC_KEYS if k.endswith("_hp")]
HC_METRIC_KEYS = [k for k in TEMPO_METRIC_KEYS if k.endswith("_hc")]


def _require_deps() -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed (run `make setup`)")
    if requests is None:
        raise RuntimeError("requests is not installed (run `make setup`)")


# --------------------------------------------------------------------------
# Config and date helpers
# --------------------------------------------------------------------------

def load_config(path: str | None) -> dict:
    """Load energy-config.yaml (PyYAML). Raises if unreadable or missing.

    The config file is mandatory: callers pass the path to
    scripts/energy-config.yaml.
    """
    _require_deps()
    if not path:
        raise ValueError("a config file is required (--config "
                         "path/to/energy-config.yaml)")
    if not os.path.exists(path):
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def parse_ts(value) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def window_days(cfg: dict) -> tuple[datetime.datetime, datetime.datetime, int]:
    start = parse_ts(cfg["window"]["start"])
    end = parse_ts(cfg["window"]["end"])
    return start, end, (end - start).days


def iso_utc(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def date_utc(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def boundary_times(start: datetime.datetime, n_days: int,
                   offset_s: int = 1) -> list[int]:
    """One timestamp per day boundary of the window (n_days + 1 values).

    A small offset avoids instants where the sample gating (relabel) may not
    have flushed the day's last bucket yet. Mirrors the probe used during the
    analysis sessions.
    """
    return [
        int((start + datetime.timedelta(days=i)).timestamp()) + offset_s
        for i in range(n_days + 1)
    ]


def hour_boundaries(start: datetime.datetime, n_days: int) -> list[int]:
    """n_days*24 + 1 hourly timestamps, from `start` to start + n_days."""
    return [int((start + datetime.timedelta(hours=i)).timestamp())
            for i in range(n_days * 24 + 1)]


def utc_hours_labels(start: datetime.datetime, n_days: int) -> list[str]:
    """Start-of-hour labels (YYYY-MM-DD HH:00) for the n_days*24 hour slots."""
    return [iso_utc(int((start + datetime.timedelta(hours=i)).timestamp()))
            for i in range(n_days * 24)]


# --------------------------------------------------------------------------
# VictoriaMetrics query layer (requests)
# --------------------------------------------------------------------------

def vm_get(url: str, path: str, params: dict, timeout: int = 120) -> dict:
    _require_deps()
    resp = requests.get(url.rstrip("/") + path, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"VM query failed ({data.get('errorType')}): "
                           f"{data.get('error')} query={params.get('query')}")
    return data["data"]


def query_max_over_time(url: str, selector: str, time_ts: int,
                        lookback: str = "2d") -> float | None:
    """Instant value of max_over_time(selector[lookback]) at `time_ts`.

    Used to sample a cumulative counter once per day without scanning the
    whole year (each query only walks the lookback window).
    """
    data = vm_get(url, "/api/v1/query", {
        "query": f"max_over_time({selector}[{lookback}])",
        "time": time_ts,
    }, timeout=60)
    return float(data["result"][0]["value"][1]) if data["result"] else None


def sample_daily_series(url: str, selector: str, times: list[int],
                        workers: int = 16) -> list[float | None]:
    """Per-boundary counter readings for the given timestamps (parallel)."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda t: query_max_over_time(url, selector, t),
                           times))


def sample_deltas(url: str, selector: str, times: list[int],
                  workers: int = 16) -> list[float | None]:
    return daily_deltas(sample_daily_series(url, selector, times, workers))


def fetch_hourly_series(url: str, selector: str, start: datetime.datetime,
                        end: datetime.datetime) -> dict[int, float]:
    """Counter readings at hourly boundaries over [start, end).

    Fetched with query_range(step=1h) split per calendar month: a full-year
    query_range exceeds VictoriaMetrics' per-series sample cap (~30k
    samples/day) for the dense zigbee series. Values are raw (native unit).
    """
    _require_deps()
    vals: dict[int, float] = {}
    month = start
    while month < end:
        nxt = (month.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)
        nxt = min(nxt, end)
        data = vm_get(url, "/api/v1/query_range", {
            "query": selector,
            "start": month.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": nxt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "step": "3600s",
        }, timeout=120)
        for series in data["result"]:
            for ts, value in series["values"]:
                vals[int(ts)] = float(value)
        month = nxt
    return vals


# --------------------------------------------------------------------------
# Delta computation
# --------------------------------------------------------------------------

def daily_deltas(values: list[float | None]) -> list[float | None]:
    """Daily increments between consecutive boundaries (None-propagating)."""
    return [
        None if values[i - 1] is None or values[i] is None
        else values[i] - values[i - 1]
        for i in range(1, len(values))
    ]


def hourly_increments(vals: dict[int, float], start: datetime.datetime,
                      n_days: int) -> list[float | None]:
    """Hourly increments of a counter, aligned over the n_days*24 slots.

    A slot is None when either of its two boundary readings is missing.
    """
    boundaries = hour_boundaries(start, n_days)
    out: list[float | None] = [None] * (n_days * 24)
    for i in range(n_days * 24):
        a, b = boundaries[i], boundaries[i + 1]
        if a in vals and b in vals:
            out[i] = vals[b] - vals[a]
    return out


# --------------------------------------------------------------------------
# CSV cache
# --------------------------------------------------------------------------

def _read_csv_rows(path: str) -> list[list[str]]:
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        return [row for row in reader if row]


def _write_csv(path: str, header: list[str], rows: list[list[str]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


class CsvCache:
    """Plain-CSV cache of raw VictoriaMetrics samples.

    Raw daily readings (per metric, one file per metric+window) and raw hourly
    readings are persisted under `cache_dir`. A complete file short-circuits
    the DB query; generation is deterministic, so re-runs are offline and
    reproducible. Scaled to kWh is never cached, only native readings.
    """

    def __init__(self, cache_dir: str = "scripts/cache"):
        self.dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _daily_path(self, key: str, start: datetime.datetime,
                    n_days: int) -> str:
        end = start + datetime.timedelta(days=n_days)
        return os.path.join(
            self.dir, f"{key}-{start:%Y%m%d}-{end:%Y%m%d}.daily.csv")

    def _hourly_path(self, key: str, start: datetime.datetime,
                     end: datetime.datetime) -> str:
        return os.path.join(
            self.dir, f"{key}-{start:%Y%m%d}-{end:%Y%m%d}.hourly.csv")

    def daily(self, key: str, start: datetime.datetime, n_days: int,
              url: str | None = None, selector: str | None = None,
              workers: int = 16) -> list[float | None]:
        """Day-boundary readings for a counter (n_days + 1 values).

        Calls the VM only on a cache miss. `url` and `selector` are required
        on a miss.
        """
        path = self._daily_path(key, start, n_days)
        if os.path.exists(path):
            rows = _read_csv_rows(path)
            if len(rows) == n_days + 1:
                return [float(row[1]) if row[1] else None for row in rows]
        if url is None or selector is None:
            raise ValueError(f"cache miss for {key} and no url/selector given")
        times = boundary_times(start, n_days)
        vals = sample_daily_series(url, selector, times, workers=workers)
        _write_csv(path, ["date", "value"], [
            [(start + datetime.timedelta(days=i)).strftime("%Y-%m-%d"),
             "" if v is None else repr(v)]
            for i, v in enumerate(vals)
        ])
        return vals

    def hourly(self, key: str, start: datetime.datetime, n_days: int,
               url: str | None = None,
               selector: str | None = None) -> dict[int, float]:
        """Hourly counter readings over the window (dict ts -> value)."""
        end = start + datetime.timedelta(days=n_days)
        path = self._hourly_path(key, start, end)
        if os.path.exists(path):
            return {int(row[0]): float(row[2]) for row in _read_csv_rows(path)}
        if url is None or selector is None:
            raise ValueError(f"cache miss for {key} and no url/selector given")
        vals = fetch_hourly_series(url, selector, start, end)
        _write_csv(path, ["unix_ts", "utc", "value"], [
            [str(ts), iso_utc(ts), repr(v)] for ts, v in sorted(vals.items())
        ])
        return vals

    def clear(self) -> None:
        for name in os.listdir(self.dir):
            if name.endswith(".daily.csv") or name.endswith(".hourly.csv"):
                os.remove(os.path.join(self.dir, name))


# --------------------------------------------------------------------------
# Tariffs
# --------------------------------------------------------------------------

def _norm_hp_hours(value) -> list[list[int]]:
    """Normalize hp_hours to a list of [start, end) UTC hour windows."""
    if not value:
        return []
    if isinstance(value[0], (int, float)):
        return [[int(value[0]), int(value[1])]]
    return [[int(w[0]), int(w[1])] for w in value]


class Tariff:
    """A contract expressed as {tempo metric -> EUR/kWh} rates.

    `cost` prices a yearly energy split: sum(kWh_per_counter * rate).
    """

    def __init__(self, name: str, description: str, kind: str,
                 rates: dict[str, float], hp_hours: list[list[int]] = None):
        self.name = name
        self.description = description
        self.kind = kind            # flat | hp_hc | tempo
        self.rates = rates          # {tempo_*_hc/hp -> EUR/kWh}
        self.hp_hours = hp_hours or []  # [[start, end), ...] UTC

    def cost(self, kwh_by_metric: dict[str, float]) -> float:
        """Yearly cost (EUR) pricing the six tempo counters at their rates."""
        return sum(kwh_by_metric.get(metric, 0.0) * rate
                   for metric, rate in self.rates.items())

    def total_kwh(self, kwh_by_metric: dict[str, float]) -> float:
        return sum(kwh_by_metric.get(metric, 0.0) for metric in self.rates)

    def price_per_kwh(self, kwh_by_metric: dict[str, float]) -> float:
        total = self.total_kwh(kwh_by_metric)
        return self.cost(kwh_by_metric) / total if total else 0.0

    def __repr__(self) -> str:  # pragma: no cover
        return (f"Tariff({self.name!r}, kind={self.kind!r}, "
                f"rates={self.rates!r})")


def load_tariffs(cfg: dict) -> dict[str, Tariff]:
    """Normalize the contracts of energy-config.yaml `tariffs`.

    * flat:         one uniform rate on all six counters;
    * hp_hc:        hc_rate on *_hc counters, hp_rate on *_hp counters;
    * tempo:        explicit rates per color x period (e.g. blue_hc).
    """
    _require_deps()
    tariffs: dict[str, Tariff] = {}
    for name, t in cfg.get("tariffs", {}).items():
        kind = t.get("kind")
        rates: dict[str, float] = {}
        if kind == "flat":
            rate = float(t["rate_per_kwh"])
            rates = {metric: rate for metric in TEMPO_METRIC_KEYS}
        elif kind == "hp_hc":
            hc = float(t["hc_rate_per_kwh"])
            hp = float(t["hp_rate_per_kwh"])
            rates = {metric: hc for metric in HC_METRIC_KEYS}
            rates.update({metric: hp for metric in HP_METRIC_KEYS})
        elif kind == "tempo":
            for simple, rate in t.get("rates", {}).items():
                rates[f"tempo_{simple}"] = float(rate)
        else:
            raise ValueError(f"tariff {name!r}: unknown kind {kind!r}")
        tariffs[name] = Tariff(name, t.get("description", ""), kind, rates,
                               _norm_hp_hours(t.get("hp_hours")))
    return tariffs


def cost_all(tariffs: dict[str, Tariff],
             kwh_by_metric: dict[str, float]) -> dict[str, float]:
    """Yearly cost per contract for a given six-counter energy split."""
    return {name: t.cost(kwh_by_metric) for name, t in tariffs.items()}


def is_hp_utc(ts: int, hp_hours: list[list[int]]) -> bool:
    """True when the UTC hour of `ts` falls in a High-price window."""
    h = datetime.datetime.fromtimestamp(ts, UTC).hour
    return any(s <= h < e for s, e in hp_hours)


# --------------------------------------------------------------------------
# Canonical hourly-energy.csv reader (Phase 2 output, used by phases 3..7)
# --------------------------------------------------------------------------

def load_hourly_energy(path: str) -> dict[str, list]:
    """Read the Phase 2 canonical record into a column->list dict.

    `utc_hour` is kept as a string; every other column is parsed as float.
    """
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        out: dict[str, list] = {col: [] for col in cols}
        for row in reader:
            for col in cols:
                raw = row.get(col, "")
                out[col].append(raw if col == "utc_hour" else float(raw))
    return out


def write_json(path: str, obj) -> None:
    import json
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)


def read_json(path: str):
    import json
    with open(path) as f:
        return json.load(f)