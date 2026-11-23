#!/usr/bin/env python3
"""Read-only journald/systemd observer; writes aggregated observations only.

First execution initializes journal cursors at the current end of each journal,
without historical backfill. All per-batch API increments and cursor updates
are committed in the same PostgreSQL transaction.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import select
import subprocess
import sys
import time

DB = "trad_bot"
UNITS = (
    "trad-bot-scanner.service",
    "trad-bot-me-inverse-short-oos2-runtime.service",
    "trad-bot-me-inverse-short-oos2-v2-runtime.service",
    "trad-bot-me-inverse-short-v3-runtime.service",
)
MAX_LINES = 3000
LIMITER_MANIFEST = "/etc/tradbot-monitor/limiter_modes.json"
LOG_FIELDS = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z_0-9]*)=([^\s]+)")
VALUE_FILTER = re.compile(r"[^a-zA-Z0-9_./:-]")

def run(args, *, input_text=None, timeout=25):
    return subprocess.run(args, input=input_text, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=True).stdout

def sql_lit(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"

def safe_dimension(value, *, length=90):
    return VALUE_FILTER.sub("_", str(value or ""))[:length]

def db_query(sql):
    return run(["psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-d", DB], input_text=sql)

def systemd_props(unit):
    raw = run(["systemctl", "show", unit, "--property=ActiveState,MainPID,NRestarts,ActiveEnterTimestampMonotonic"], timeout=12)
    p = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    try:
        start_us = int(p.get("ActiveEnterTimestampMonotonic") or "0")
        uptime = max(0, round(time.clock_gettime(time.CLOCK_MONOTONIC) - start_us / 1_000_000, 2)) if start_us else None
    except (ValueError, OSError):
        uptime = None
    return (safe_dimension(p.get("ActiveState", "unknown")),
            int(p.get("MainPID") or 0),
            int(p.get("NRestarts") or 0),
            uptime)

def journal_last_cursor(unit):
    out = run(["journalctl", "-u", unit, "-n", "1", "-o", "json", "-q", "--no-pager"], timeout=15)
    for line in reversed(out.splitlines()):
        try:
            c = json.loads(line).get("__CURSOR")
        except ValueError:
            continue
        if c:
            return c
    return None

class JournalGapError(RuntimeError):
    """Stored cursor could not be verified against the available journal."""


def verify_cursor_exists(unit, cursor):
    """Require an exact journal entry at --cursor; never assume seek implies match."""
    cmd = ["journalctl", "-u", unit, "--cursor=" + cursor,
           "-o", "json", "--no-pager", "-q"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 12)
        if not ready:
            raise JournalGapError("cursor probe timed out")
        line = proc.stdout.readline(1048577)
        if len(line) > 1048576:
            raise JournalGapError("cursor probe line too large")
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            raise JournalGapError("cursor probe missing or malformed JSON")
        if not isinstance(entry, dict) or entry.get("__CURSOR") != cursor:
            raise JournalGapError("stored cursor missing from available journal")
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def read_journal(unit, cursor):
    """Stream at most MAX_LINES journal entries; never buffer full journal."""
    verify_cursor_exists(unit, cursor)
    command = ["journalctl", "-u", unit, "--after-cursor=" + cursor,
               "-o", "json", "--no-pager", "-q"]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)
    events = collections.Counter()
    last_cursor = cursor
    n = 0
    truncated = False
    try:
        for line in proc.stdout:
            n += 1
            if n > MAX_LINES:
                truncated = True
                break
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                raise RuntimeError("Invalid journal JSON; cursor unchanged")
            new_cursor = obj.get("__CURSOR")
            timestamp = obj.get("__REALTIME_TIMESTAMP")
            if not new_cursor or not timestamp:
                raise RuntimeError("Missing journal cursor/timestamp; cursor unchanged")
            last_cursor = new_cursor
            message = obj.get("MESSAGE", "")
            if not isinstance(message, str):
                continue
            if "BYBIT_API_CALL" not in message and "BYBIT_API_SKIP" not in message:
                continue
            fields = dict(LOG_FIELDS.findall(message))
            event_kind = "CALL" if "BYBIT_API_CALL" in message else "SKIP"
            second = int(timestamp) // 1_000_000
            endpoint = safe_dimension(fields.get("endpoint", ""))
            result = safe_dimension(fields.get("result", ""))
            ret_code = safe_dimension(fields.get("ret_code", ""), length=20)
            status_code = safe_dimension(fields.get("status_code", ""), length=20)
            events[(second, unit, endpoint, event_kind, result, ret_code, status_code)] += 1
    finally:
        if truncated:
            proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
    if proc.returncode not in (0, -15) and not truncated:
        raise RuntimeError("journalctl exited abnormally; cursor unchanged")
    return last_cursor, events, n, truncated

def limiter_mode(unit, pid):
    """Return a verified, PID-pinned mode; fail closed to UNKNOWN.

    The root-managed manifest contains no credentials. Its modes describe the
    configuration audited for exactly the observed process PID, not an inferred
    future runtime setting. After any restart, returns UNKNOWN until re-audited.
    """
    try:
        with open(LIMITER_MANIFEST, encoding="utf-8") as f:
            entries = json.load(f)
        info = entries.get("units", {}).get(unit, {})
        expected_pid = info.get("pid")
        mode = info.get("mode")
        if (isinstance(expected_pid, int) and expected_pid > 0 and
                pid == expected_pid and mode in ("ON", "OFF")):
            return mode
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return "UNKNOWN"


def load_cursors():
    raw = db_query("SELECT unit_name, cursor_value FROM monitoring.journal_cursor;")
    return dict(line.split("|", 1) for line in raw.splitlines() if "|" in line)

def commit_batch(unit, cursor, events, props):
    # All metrics and cursor change within one transaction. If SQL fails,
    # retrying after old cursor is safe.
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    parts = ["BEGIN;", "SET LOCAL lock_timeout = '3s';", "SET LOCAL statement_timeout = '20s';"]
    if events:
        values = []
        for (sec, u, endpoint, kind, result, ret_code, status_code), count in events.items():
            stamp = dt.datetime.fromtimestamp(sec, dt.timezone.utc).isoformat()
            values.append("(" + ",".join(map(sql_lit, (stamp, u, endpoint, kind, result, ret_code, status_code))) + "," + str(count) + ")")
        # Chunk to keep individual SQL statements small, limit command size.
        for i in range(0, len(values), 400):
            parts.append("INSERT INTO monitoring.api_metric_sec "
                         "(event_second, unit_name, endpoint, event_kind, result, ret_code, status_code, event_count) VALUES "
                         + ",".join(values[i:i+400]) + " ON CONFLICT "
                         "(event_second, unit_name, endpoint, event_kind, result, ret_code, status_code) "
                         "DO UPDATE SET event_count = monitoring.api_metric_sec.event_count + EXCLUDED.event_count;")
    state, pid, restart_count, uptime = props
    mode = limiter_mode(unit, pid)
    parts.append("INSERT INTO monitoring.service_sample "
                 "(sampled_at, unit_name, active_state, main_pid, restart_count, uptime_sec, limiter_enabled) VALUES ("
                 + ",".join([sql_lit(now), sql_lit(unit), sql_lit(state), str(pid), str(restart_count),
                             str(uptime) if uptime is not None else "NULL", sql_lit(mode)])
                 + ") ON CONFLICT (sampled_at, unit_name) DO NOTHING;")
    parts.append("INSERT INTO monitoring.journal_cursor (unit_name, cursor_value, updated_at) VALUES ("
                 + ",".join((sql_lit(unit), sql_lit(cursor), sql_lit(now)))
                 + ") ON CONFLICT (unit_name) DO UPDATE SET cursor_value=EXCLUDED.cursor_value, updated_at=EXCLUDED.updated_at;")
    parts.append("COMMIT;")
    db_query("\n".join(parts))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="one scan, initialize cursors without backfill")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    if args.interval < 15:
        parser.error("interval must be >=15")
    while True:
        try:
            cursors = load_cursors()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"monitor: cannot load cursors: {type(exc).__name__}", flush=True)
            if args.once:
                return 1
            time.sleep(args.interval)
            continue
        for unit in UNITS:
            try:
                if unit not in cursors:
                    cursor = journal_last_cursor(unit)
                    if cursor is None:
                        print(f"monitor: {unit} has no cursor yet (no backfill)", flush=True)
                        continue
                    events, lines, truncated = collections.Counter(), 0, False
                    phase = "INITIALIZED_AT_TAIL"
                else:
                    cursor, events, lines, truncated = read_journal(unit, cursors[unit])
                    phase = "RUN"
                props = systemd_props(unit)
                commit_batch(unit, cursor, events, props)
                print(f"monitor: {unit} {phase} lines={lines} events={sum(events.values())} "
                      f"truncated={truncated} state={props[0]} pid={props[1]}", flush=True)
            except JournalGapError:
                # Leave cursor and API counters untouched; never silently skip.
                print(f"monitor: {unit} SOURCE_GAP_DETECTED cursor_unverified", flush=True)
                if args.once:
                    return 2
            except Exception as exc:
                # Never output raw journald payload or credential-bearing stderr.
                print(f"monitor: {unit} ERROR {type(exc).__name__}", flush=True)
                if args.once:
                    return 2
        if args.once:
            return 0
        time.sleep(args.interval)

if __name__ == "__main__":
    sys.exit(main())
