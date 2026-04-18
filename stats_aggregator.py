"""
CaddyPanel Stats Aggregator

Incrementally processes Caddy JSON access logs into a SQLite database
for reliable, long-term statistics with efficient querying.

Retention:
- Hourly buckets (hourly_stats): 7 days of detailed per-host, per-path data
- Daily buckets (daily_stats): 365 days of aggregated data

Rollup:
- Hourly -> Daily: consolidates hourly data older than 7 days
- Daily cleanup: deletes daily data older than 365 days

GeoIP:
- Optional: resolves client IPs to country codes via MaxMind GeoLite2 (.mmdb)
- Falls back gracefully if database is not available
"""

import json
import sqlite3
import time as time_module
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re as _re


def _parse_ts(ts_value):
    """Parse a Caddy log timestamp to a Unix epoch float.
    Accepts:
      - numeric (int/float): used directly as epoch
      - string: RFC3339 / ISO 8601 format (e.g. '2024-01-15T10:30:00Z')
    Returns 0.0 if the value cannot be parsed."""
    if isinstance(ts_value, (int, float)):
        return float(ts_value) if ts_value > 0 else 0.0
    if isinstance(ts_value, str) and ts_value:
        try:
            # Try ISO 8601 / RFC3339
            # Python 3.7+ supports datetime.fromisoformat for most formats
            s = ts_value.replace('Z', '+00:00')
            dt = datetime.fromisoformat(s)
            return dt.timestamp()
        except (ValueError, OverflowError):
            pass
    return 0.0
from collections import deque

# --- Optional GeoIP ---
_geoip_reader = None


def configure_geoip(db_path):
    """Configure the GeoIP resolver. Call with the path to a GeoLite2-Country.mmdb file.
    If the file doesn't exist or can't be read, GeoIP will be disabled (countries = 'Unknown')."""
    global _geoip_reader
    _geoip_reader = None
    if db_path:
        try:
            import geoip2.database
            _geoip_reader = geoip2.database.Reader(db_path)
            print(f"GeoIP: loaded database from {db_path}")
        except Exception as e:
            print(f"GeoIP: could not load database from {db_path}: {e}")


def _resolve_country(ip_str):
    """Resolve an IP address string to an ISO 3166-1 alpha-2 country code.
    Returns 'Unknown' if GeoIP is not configured or resolution fails."""
    if not _geoip_reader or not ip_str:
        return 'Unknown'
    try:
        resp = _geoip_reader.country(ip_str)
        return resp.country.iso_code or 'Unknown'
    except Exception:
        return 'Unknown'


def is_geoip_available():
    """Check if GeoIP resolution is available."""
    return _geoip_reader is not None


_db_path = None

VALID_PERIODS = ('24h', '7d', '30d', '90d', '1y')
HOURLY_RETENTION_DAYS = 7
DAILY_RETENTION_DAYS = 365
MAX_INITIAL_LINES = 500000
MAX_INITIAL_BYTES = 50 * 1024 * 1024  # 50 MB
TOP_N_PATHS = 20
TOP_N_UAS = 10
TOP_N_COUNTRIES = 20


def init_stats_db(db_path):
    """Initialize the stats database. Create tables if they don't exist.
    Must be called before any other function."""
    global _db_path
    _db_path = str(db_path)

    conn = sqlite3.connect(_db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hourly_stats (
            bucket_hour TEXT NOT NULL,
            host TEXT NOT NULL,
            total INTEGER DEFAULT 0,
            status_1xx INTEGER DEFAULT 0,
            status_2xx INTEGER DEFAULT 0,
            status_3xx INTEGER DEFAULT 0,
            status_4xx INTEGER DEFAULT 0,
            status_5xx INTEGER DEFAULT 0,
            total_duration REAL DEFAULT 0,
            total_size INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            top_paths TEXT DEFAULT '{}',
            top_uas TEXT DEFAULT '{}',
            top_countries TEXT DEFAULT '{}',
            PRIMARY KEY (bucket_hour, host)
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            bucket_date TEXT NOT NULL,
            host TEXT NOT NULL,
            total INTEGER DEFAULT 0,
            status_1xx INTEGER DEFAULT 0,
            status_2xx INTEGER DEFAULT 0,
            status_3xx INTEGER DEFAULT 0,
            status_4xx INTEGER DEFAULT 0,
            status_5xx INTEGER DEFAULT 0,
            total_duration REAL DEFAULT 0,
            total_size INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            top_paths TEXT DEFAULT '{}',
            top_uas TEXT DEFAULT '{}',
            top_countries TEXT DEFAULT '{}',
            PRIMARY KEY (bucket_date, host)
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_hourly_hour ON hourly_stats(bucket_hour);
        CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_stats(bucket_date);
    """)
    conn.commit()

    # Migration: add top_countries column to existing tables
    for table in ('hourly_stats', 'daily_stats'):
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if 'top_countries' not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN top_countries TEXT DEFAULT '{{}}'")
                print(f"Stats DB migration: added top_countries to {table}")
        except Exception as e:
            print(f"Stats DB migration check for {table}: {e}")

    conn.close()


def _get_conn(timeout=10):
    """Get a connection to the stats database."""
    if _db_path is None:
        raise RuntimeError("Stats DB not initialized. Call init_stats_db() first.")
    return sqlite3.connect(_db_path, timeout=timeout)


def _top_n_from_dict(d, n):
    """Keep top N items from a dict by value, return as sorted dict."""
    if not d:
        return {}
    return dict(sorted(d.items(), key=lambda x: x[1], reverse=True)[:n])


def _merge_top_json(existing_json, new_counts, n):
    """Merge new count dict into an existing JSON top-N dict.

    existing_json: str (JSON dict like {"path": count, ...})
    new_counts: dict ({"path": count, ...})
    n: keep top N entries after merge

    Returns: str (JSON dict)
    """
    merged = {}
    if existing_json:
        try:
            merged = json.loads(existing_json)
        except (json.JSONDecodeError, TypeError):
            merged = {}
    for k, v in new_counts.items():
        merged[k] = merged.get(k, 0) + v
    return json.dumps(_top_n_from_dict(merged, n))


# ---------------------------------------------------------------------------
# Log processing
# ---------------------------------------------------------------------------

def process_new_logs(log_file_path):
    """Process new Caddy access log entries since last processing.

    Uses byte offset tracking for efficiency. On first run (no offset stored),
    reads only the tail of the log file to avoid processing huge history.

    Returns the number of new entries processed.
    """
    if _db_path is None:
        return 0

    log_path = Path(log_file_path) if not isinstance(log_file_path, Path) else log_file_path
    if not log_path.exists():
        return 0

    current_size = log_path.stat().st_size

    conn = _get_conn()
    conn.execute("BEGIN IMMEDIATE")

    # Read last processed state
    last_ts = 0.0
    last_offset = 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_processed_ts'").fetchone()
    if row:
        try:
            last_ts = float(row[0])
        except (ValueError, TypeError):
            pass
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_processed_offset'").fetchone()
    if row:
        try:
            last_offset = int(row[0])
        except (ValueError, TypeError):
            pass

    # Detect log rotation (file got smaller than our last offset)
    if current_size < last_offset:
        last_offset = 0
        last_ts = 0.0

    if current_size <= last_offset:
        conn.execute("ROLLBACK")
        conn.close()
        return 0

    # Read new log entries
    new_entries = []
    max_ts = last_ts
    new_offset = last_offset
    is_first_run = (last_offset == 0 and last_ts == 0.0)

    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            if is_first_run:
                # First run: seek near end of file to avoid reading huge history
                if current_size > MAX_INITIAL_BYTES:
                    f.seek(current_size - MAX_INITIAL_BYTES)
                    f.readline()  # discard partial first line
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if len(new_entries) >= MAX_INITIAL_LINES:
                        break
                    try:
                        entry = json.loads(line)
                        ts = _parse_ts(entry.get('ts', 0))
                        if ts > 0:
                            entry['_ts_epoch'] = ts
                            new_entries.append(entry)
                            max_ts = max(max_ts, ts)
                    except (json.JSONDecodeError, ValueError):
                        continue
                new_offset = f.tell()
            else:
                # Subsequent runs: read from last offset
                f.seek(last_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        ts = _parse_ts(entry.get('ts', 0))
                        if ts > 0:
                            entry['_ts_epoch'] = ts
                            new_entries.append(entry)
                            max_ts = max(max_ts, ts)
                    except (json.JSONDecodeError, ValueError):
                        continue
                new_offset = f.tell()
    except IOError as e:
        print(f"Error reading log file {log_path}: {e}")
        conn.execute("ROLLBACK")
        conn.close()
        return 0

    if not new_entries:
        # No new processable entries, update offset anyway
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_processed_offset', ?)",
            (str(new_offset),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_file_size', ?)",
            (str(current_size),),
        )
        conn.commit()
        conn.close()
        return 0

    # Aggregate new entries by (bucket_hour, host)
    buckets = {}
    for entry in new_entries:
        ts = entry.get('_ts_epoch', _parse_ts(entry.get('ts', 0)))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        bucket_hour = dt.strftime('%Y-%m-%dT%H')

        request = entry.get('request', {})
        if not isinstance(request, dict):
            request = {}
        host = request.get('host', entry.get('host', 'Unknown'))
        status = int(entry.get('status', 0))
        duration = float(entry.get('duration', 0))
        size = int(entry.get('size', 0))

        uri = request.get('uri', '/')
        path = uri.split('?')[0] if isinstance(uri, str) and uri else '/'

        headers = request.get('headers', {})
        if not isinstance(headers, dict):
            headers = {}
        ua_list = headers.get('User-Agent', ['Unknown'])
        if not isinstance(ua_list, list):
            ua_list = ['Unknown']
        ua_full = ua_list[0] if ua_list else 'Unknown'
        ua_simple = ua_full.split('/')[0].split('(')[0].strip() or 'Unknown'

        key = (bucket_hour, host)
        if key not in buckets:
            buckets[key] = {
                'total': 0, 'status_1xx': 0, 'status_2xx': 0, 'status_3xx': 0,
                'status_4xx': 0, 'status_5xx': 0, 'total_duration': 0.0,
                'total_size': 0, 'error_count': 0,
                'top_paths': {}, 'top_uas': {}, 'top_countries': {},
            }

        b = buckets[key]
        b['total'] += 1
        if 100 <= status <= 199:
            b['status_1xx'] += 1
        elif 200 <= status <= 299:
            b['status_2xx'] += 1
        elif 300 <= status <= 399:
            b['status_3xx'] += 1
        elif 400 <= status <= 499:
            b['status_4xx'] += 1
        elif 500 <= status <= 599:
            b['status_5xx'] += 1
            b['error_count'] += 1
        b['total_duration'] += duration
        b['total_size'] += size
        b['top_paths'][path] = b['top_paths'].get(path, 0) + 1
        b['top_uas'][ua_simple] = b['top_uas'].get(ua_simple, 0) + 1

        # GeoIP: resolve client IP to country
        client_ip = request.get('remote_ip', request.get('client_ip', ''))
        if not client_ip and isinstance(headers, dict):
            # Fallback: check X-Forwarded-For or X-Real-IP headers
            xff = headers.get('X-Forwarded-For', [])
            if xff and isinstance(xff, list) and xff[0]:
                client_ip = xff[0].split(',')[0].strip()
            else:
                xri = headers.get('X-Real-Ip', [])
                if xri and isinstance(xri, list) and xri[0]:
                    client_ip = xri[0]
        country = _resolve_country(client_ip)
        if country != 'Unknown':
            b['top_countries'][country] = b['top_countries'].get(country, 0) + 1

    # Truncate top paths/UAs/countries per bucket to limit stored size
    for b in buckets.values():
        b['top_paths'] = _top_n_from_dict(b['top_paths'], TOP_N_PATHS)
        b['top_uas'] = _top_n_from_dict(b['top_uas'], TOP_N_UAS)
        b['top_countries'] = _top_n_from_dict(b['top_countries'], TOP_N_COUNTRIES)

    # Upsert into hourly_stats
    for (bucket_hour, host), data in buckets.items():
        existing = conn.execute(
            "SELECT top_paths, top_uas, top_countries FROM hourly_stats WHERE bucket_hour = ? AND host = ?",
            (bucket_hour, host),
        ).fetchone()

        if existing:
            merged_paths = _merge_top_json(existing[0], data['top_paths'], TOP_N_PATHS)
            merged_uas = _merge_top_json(existing[1], data['top_uas'], TOP_N_UAS)
            merged_countries = _merge_top_json(existing[2], data['top_countries'], TOP_N_COUNTRIES)

            conn.execute(
                """UPDATE hourly_stats SET
                    total = total + ?, status_1xx = status_1xx + ?,
                    status_2xx = status_2xx + ?, status_3xx = status_3xx + ?,
                    status_4xx = status_4xx + ?, status_5xx = status_5xx + ?,
                    total_duration = total_duration + ?, total_size = total_size + ?,
                    error_count = error_count + ?, top_paths = ?, top_uas = ?,
                    top_countries = ?
                WHERE bucket_hour = ? AND host = ?""",
                (
                    data['total'], data['status_1xx'], data['status_2xx'],
                    data['status_3xx'], data['status_4xx'], data['status_5xx'],
                    data['total_duration'], data['total_size'], data['error_count'],
                    merged_paths, merged_uas, merged_countries, bucket_hour, host,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO hourly_stats
                    (bucket_hour, host, total, status_1xx, status_2xx,
                     status_3xx, status_4xx, status_5xx, total_duration,
                     total_size, error_count, top_paths, top_uas, top_countries)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    bucket_hour, host, data['total'], data['status_1xx'],
                    data['status_2xx'], data['status_3xx'], data['status_4xx'],
                    data['status_5xx'], data['total_duration'], data['total_size'],
                    data['error_count'],
                    json.dumps(data['top_paths']),
                    json.dumps(data['top_uas']),
                    json.dumps(data['top_countries']),
                ),
            )

    # Update meta
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_processed_ts', ?)",
        (str(max_ts),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_processed_offset', ?)",
        (str(new_offset),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_file_size', ?)",
        (str(current_size),),
    )

    conn.commit()
    conn.close()

    print(f"Stats: processed {len(new_entries)} new log entries.")
    return len(new_entries)


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------

def rollup_old_buckets():
    """Consolidate hourly_stats older than HOURLY_RETENTION_DAYS into daily_stats.
    Delete daily_stats older than DAILY_RETENTION_DAYS.
    Throttled: only runs once every 6 hours."""
    if _db_path is None:
        return

    conn = _get_conn()

    # Check throttle
    last_rollup = 0.0
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_rollup_ts'").fetchone()
    if row:
        try:
            last_rollup = float(row[0])
        except (ValueError, TypeError):
            pass

    if (time_module.time() - last_rollup) < 6 * 3600:
        conn.close()
        return

    conn.execute("BEGIN IMMEDIATE")
    now = datetime.now(timezone.utc)

    # 1. Hourly -> Daily rollup (only complete days older than retention)
    cutoff_date = (now - timedelta(days=HOURLY_RETENTION_DAYS)).strftime('%Y-%m-%d')

    hourly_rows = conn.execute(
        "SELECT * FROM hourly_stats WHERE substr(bucket_hour, 1, 10) < ?",
        (cutoff_date,),
    ).fetchall()

    if hourly_rows:
        daily_buckets = {}
        for row in hourly_rows:
            bucket_date = row[0][:10]  # '2026-04-17T08' -> '2026-04-17'
            host = row[1]
            key = (bucket_date, host)

            if key not in daily_buckets:
                daily_buckets[key] = {
                    'total': 0, 'status_1xx': 0, 'status_2xx': 0, 'status_3xx': 0,
                    'status_4xx': 0, 'status_5xx': 0, 'total_duration': 0.0,
                    'total_size': 0, 'error_count': 0,
                    'top_paths': {}, 'top_uas': {}, 'top_countries': {},
                }

            b = daily_buckets[key]
            b['total'] += row[2]
            b['status_1xx'] += row[3]
            b['status_2xx'] += row[4]
            b['status_3xx'] += row[5]
            b['status_4xx'] += row[6]
            b['status_5xx'] += row[7]
            b['total_duration'] += row[8]
            b['total_size'] += row[9]
            b['error_count'] += row[10]

            # Merge top_paths / top_uas from JSON
            try:
                for path, count in json.loads(row[11] or '{}').items():
                    b['top_paths'][path] = b['top_paths'].get(path, 0) + count
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                for ua, count in json.loads(row[12] or '{}').items():
                    b['top_uas'][ua] = b['top_uas'].get(ua, 0) + count
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                for country, count in json.loads(row[13] or '{}').items():
                    b['top_countries'][country] = b['top_countries'].get(country, 0) + count
            except (json.JSONDecodeError, TypeError):
                pass

        # Upsert daily_stats (REPLACE with freshly computed totals for idempotency)
        for (bucket_date, host), data in daily_buckets.items():
            top_paths_json = json.dumps(_top_n_from_dict(data['top_paths'], TOP_N_PATHS))
            top_uas_json = json.dumps(_top_n_from_dict(data['top_uas'], TOP_N_UAS))
            top_countries_json = json.dumps(_top_n_from_dict(data['top_countries'], TOP_N_COUNTRIES))

            existing = conn.execute(
                "SELECT total FROM daily_stats WHERE bucket_date = ? AND host = ?",
                (bucket_date, host),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE daily_stats SET
                        total = ?, status_1xx = ?, status_2xx = ?, status_3xx = ?,
                        status_4xx = ?, status_5xx = ?, total_duration = ?,
                        total_size = ?, error_count = ?, top_paths = ?, top_uas = ?,
                        top_countries = ?
                    WHERE bucket_date = ? AND host = ?""",
                    (
                        data['total'], data['status_1xx'], data['status_2xx'],
                        data['status_3xx'], data['status_4xx'], data['status_5xx'],
                        data['total_duration'], data['total_size'], data['error_count'],
                        top_paths_json, top_uas_json, top_countries_json, bucket_date, host,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO daily_stats
                        (bucket_date, host, total, status_1xx, status_2xx,
                         status_3xx, status_4xx, status_5xx, total_duration,
                         total_size, error_count, top_paths, top_uas, top_countries)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bucket_date, host, data['total'], data['status_1xx'],
                        data['status_2xx'], data['status_3xx'], data['status_4xx'],
                        data['status_5xx'], data['total_duration'], data['total_size'],
                        data['error_count'], top_paths_json, top_uas_json, top_countries_json,
                    ),
                )

        # Delete rolled-up hourly data
        conn.execute(
            "DELETE FROM hourly_stats WHERE substr(bucket_hour, 1, 10) < ?",
            (cutoff_date,),
        )
        print(f"Stats: rolled up hourly data before {cutoff_date} into daily_stats.")

    # 2. Delete old daily data
    daily_cutoff = (now - timedelta(days=DAILY_RETENTION_DAYS)).strftime('%Y-%m-%d')
    deleted = conn.execute(
        "DELETE FROM daily_stats WHERE bucket_date < ?", (daily_cutoff,)
    ).rowcount
    if deleted:
        print(f"Stats: pruned {deleted} daily_stats rows before {daily_cutoff}.")

    # Update last_rollup_ts
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_rollup_ts', ?)",
        (str(time_module.time()),),
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _empty_stats(period, host):
    """Return an empty stats dict with the correct structure."""
    return {
        "total_requests": 0,
        "requests_by_host": {},
        "status_codes_dist": {"1xx": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0},
        "top_paths": [],
        "top_user_agents": [],
        "top_countries": [],
        "geoip_available": is_geoip_available(),
        "avg_response_time_ms": 0.0,
        "avg_response_size_kb": 0.0,
        "error_rate_percent": 0.0,
        "requests_timeseries": [],
        "data_from_utc": "N/A",
        "data_to_utc": "N/A",
        "period": period,
        "host": host,
        "log_read_error": None,
    }


def _build_hourly_timeseries(data, period, now):
    """Build a complete hourly timeseries with gap-filling."""
    hours = 24 if period == '24h' else 168
    start = now - timedelta(hours=hours)
    current = start.replace(minute=0, second=0, microsecond=0)

    result = []
    while current <= now:
        key = current.strftime('%Y-%m-%dT%H')
        result.append({
            'time': current.strftime('%Y-%m-%d %H:%M'),
            'count': data.get(key, 0),
        })
        current += timedelta(hours=1)

    return result


def _build_daily_timeseries(data, period, now):
    """Build a complete daily timeseries with gap-filling."""
    days_map = {'30d': 30, '90d': 90, '1y': 365}
    days = days_map.get(period, 30)
    start = now - timedelta(days=days)
    current = start.replace(hour=0, minute=0, second=0, microsecond=0)

    result = []
    while current <= now:
        key = current.strftime('%Y-%m-%d')
        result.append({
            'time': key,
            'count': data.get(key, 0),
        })
        current += timedelta(days=1)

    return result


def get_stats(period='7d', host=None):
    """Get aggregated statistics for a given time period.

    period: '24h', '7d', '30d', '90d', '1y'
    host: optional host name to filter by (None = all hosts)

    Returns: dict with stats data suitable for JSON API response.
    """
    if _db_path is None:
        return _empty_stats(period, host)

    if period not in VALID_PERIODS:
        period = '7d'

    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)

    rows = []
    timeseries_key_type = 'hourly'

    try:
        if period in ('24h', '7d'):
            hours = 24 if period == '24h' else 168
            cutoff = (now - timedelta(hours=hours)).strftime('%Y-%m-%dT%H')

            query = "SELECT * FROM hourly_stats WHERE bucket_hour >= ?"
            params = [cutoff]
            if host:
                query += " AND host = ?"
                params.append(host)
            rows = conn.execute(query, params).fetchall()
            timeseries_key_type = 'hourly'

        elif period in ('30d', '90d'):
            days = 30 if period == '30d' else 90
            period_start = (now - timedelta(days=days)).strftime('%Y-%m-%d')
            boundary_date = (now - timedelta(days=HOURLY_RETENTION_DAYS)).strftime('%Y-%m-%d')
            boundary_hour = boundary_date + 'T00'

            # Daily stats for dates before the hourly boundary
            query = "SELECT * FROM daily_stats WHERE bucket_date >= ? AND bucket_date < ?"
            params = [period_start, boundary_date]
            if host:
                query += " AND host = ?"
                params.append(host)
            rows = list(conn.execute(query, params).fetchall())

            # Hourly stats for the recent part (not yet rolled up)
            query = "SELECT * FROM hourly_stats WHERE bucket_hour >= ?"
            params = [boundary_hour]
            if host:
                query += " AND host = ?"
                params.append(host)
            rows.extend(conn.execute(query, params).fetchall())

            timeseries_key_type = 'daily'

        elif period == '1y':
            period_start = (now - timedelta(days=365)).strftime('%Y-%m-%d')
            boundary_date = (now - timedelta(days=HOURLY_RETENTION_DAYS)).strftime('%Y-%m-%d')
            boundary_hour = boundary_date + 'T00'

            # Daily stats for the full period
            query = "SELECT * FROM daily_stats WHERE bucket_date >= ?"
            params = [period_start]
            if host:
                query += " AND host = ?"
                params.append(host)
            rows = list(conn.execute(query, params).fetchall())

            # Hourly stats for the recent part
            query = "SELECT * FROM hourly_stats WHERE bucket_hour >= ?"
            params = [boundary_hour]
            if host:
                query += " AND host = ?"
                params.append(host)
            rows.extend(conn.execute(query, params).fetchall())

            timeseries_key_type = 'daily'

    except Exception as e:
        print(f"Error querying stats for period={period}, host={host}: {e}")
        conn.close()
        return _empty_stats(period, host)

    conn.close()

    # Aggregate all rows into the final stats dict
    total_requests = 0
    requests_by_host = {}
    status_codes_dist = {"1xx": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
    path_counts = {}
    ua_counts = {}
    country_counts = {}
    total_duration = 0.0
    total_size = 0
    error_count = 0

    ts_hourly = {}
    ts_daily = {}
    earliest_bucket = None

    for row in rows:
        r = dict(row)
        row_total = r['total']
        total_requests += row_total

        if not host:
            h = r['host']
            requests_by_host[h] = requests_by_host.get(h, 0) + row_total

        status_codes_dist['1xx'] += r['status_1xx']
        status_codes_dist['2xx'] += r['status_2xx']
        status_codes_dist['3xx'] += r['status_3xx']
        status_codes_dist['4xx'] += r['status_4xx']
        status_codes_dist['5xx'] += r['status_5xx']
        total_duration += r['total_duration']
        total_size += r['total_size']
        error_count += r['error_count']

        # Merge top paths/UAs
        try:
            for p, c in json.loads(r['top_paths'] or '{}').items():
                path_counts[p] = path_counts.get(p, 0) + c
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            for u, c in json.loads(r['top_uas'] or '{}').items():
                ua_counts[u] = ua_counts.get(u, 0) + c
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            for country, c in json.loads(r.get('top_countries') or '{}').items():
                country_counts[country] = country_counts.get(country, 0) + c
        except (json.JSONDecodeError, TypeError):
            pass

        # Timeseries bucket key
        if 'bucket_hour' in r and r['bucket_hour']:
            key = r['bucket_hour']
            if timeseries_key_type == 'daily':
                daily_key = key[:10]
                ts_daily[daily_key] = ts_daily.get(daily_key, 0) + row_total
            else:
                ts_hourly[key] = ts_hourly.get(key, 0) + row_total
            if earliest_bucket is None or key < earliest_bucket:
                earliest_bucket = key
        elif 'bucket_date' in r and r['bucket_date']:
            key = r['bucket_date']
            ts_daily[key] = ts_daily.get(key, 0) + row_total
            if earliest_bucket is None or key < earliest_bucket:
                earliest_bucket = key

    # Build timeseries
    if timeseries_key_type == 'hourly':
        timeseries = _build_hourly_timeseries(ts_hourly, period, now)
    else:
        timeseries = _build_daily_timeseries(ts_daily, period, now)

    # Top paths and UAs
    top_paths = [
        {"path": p, "count": c}
        for p, c in sorted(path_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    ]
    top_user_agents = [
        {"agent": u, "count": c}
        for u, c in sorted(ua_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    ]
    top_countries = [
        {"country": country, "count": c}
        for country, c in sorted(country_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    ]

    # Data period label
    if earliest_bucket:
        if 'T' in earliest_bucket:
            dt_from = datetime.strptime(earliest_bucket, '%Y-%m-%dT%H').replace(
                tzinfo=timezone.utc
            )
        else:
            dt_from = datetime.strptime(earliest_bucket, '%Y-%m-%d').replace(
                tzinfo=timezone.utc
            )
        data_from_utc = dt_from.strftime('%Y-%m-%d %H:%M:%S UTC')
    else:
        data_from_utc = "N/A"

    return {
        "total_requests": total_requests,
        "requests_by_host": dict(
            sorted(requests_by_host.items(), key=lambda x: x[1], reverse=True)[:7]
        ),
        "status_codes_dist": status_codes_dist,
        "top_paths": top_paths,
        "top_user_agents": top_user_agents,
        "top_countries": top_countries,
        "geoip_available": is_geoip_available(),
        "avg_response_time_ms": (total_duration / total_requests * 1000) if total_requests else 0,
        "avg_response_size_kb": (total_size / total_requests / 1024) if total_requests else 0,
        "error_rate_percent": (error_count / total_requests * 100) if total_requests else 0,
        "requests_timeseries": timeseries,
        "data_from_utc": data_from_utc,
        "data_to_utc": now.strftime('%Y-%m-%d %H:%M:%S UTC'),
        "period": period,
        "host": host,
        "log_read_error": None,
    }


def get_available_hosts():
    """Return a sorted list of all hosts that have stats data."""
    if _db_path is None:
        return []

    conn = _get_conn()
    hosts = set()
    try:
        for row in conn.execute("SELECT DISTINCT host FROM hourly_stats"):
            hosts.add(row[0])
        for row in conn.execute("SELECT DISTINCT host FROM daily_stats"):
            hosts.add(row[0])
    except Exception:
        pass
    finally:
        conn.close()

    return sorted(hosts)


def has_data():
    """Check if the stats DB contains any aggregated data."""
    if _db_path is None:
        return False

    conn = _get_conn()
    try:
        count = conn.execute("SELECT COALESCE(SUM(total), 0) FROM hourly_stats").fetchone()[0]
        count += conn.execute("SELECT COALESCE(SUM(total), 0) FROM daily_stats").fetchone()[0]
        return count > 0
    except Exception:
        return False
    finally:
        conn.close()