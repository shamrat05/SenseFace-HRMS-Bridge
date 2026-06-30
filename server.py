#!/usr/bin/env python3
"""Standalone ZKTeco SenseFace T&A Push to HRMS bridge."""

import csv
import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "senseface_hrms.db"
HOST = os.getenv("SENSEFACE_HOST", "0.0.0.0")
PORT = int(os.getenv("SENSEFACE_PORT", "8090"))
API_KEY = os.getenv("SENSEFACE_API_KEY", "")
TIMEZONE_NAME = os.getenv("SENSEFACE_TIMEZONE", "Asia/Dhaka")
CLOCK_SYNC_INTERVAL = max(int(os.getenv("SENSEFACE_CLOCK_SYNC_INTERVAL", "3600")), 60)
CLOCK_MAX_SKEW = max(int(os.getenv("SENSEFACE_CLOCK_MAX_SKEW", "300")), 0)
LOCK = threading.Lock()
LAST_TIME_SYNC = {}
ATT_RE = re.compile(r"^(?:PIN=)?([^\t]+)\t(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\t([^\t]*)(?:\t([^\t]*))?(?:\t([^\t]*))?(?:\t([^\t]*))?")
USER_RE = re.compile(r"^(?:USER\s+)?PIN=([^\t]+)\t(.*)$")


try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise SystemExit(f"Unknown SENSEFACE_TIMEZONE: {TIMEZONE_NAME}") from exc


def now():
    return datetime.now(timezone.utc).isoformat()


def device_now():
    """Current wall-clock value expected by the attendance terminal."""
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def timezone_hours():
    offset = datetime.now(LOCAL_TZ).utcoffset()
    return int(offset.total_seconds() // 3600) if offset else 0


def timestamp_diagnostics(event_time, received_at):
    """Compare the device's naive local timestamp with server local time."""
    try:
        event = datetime.strptime(event_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
        received = datetime.fromisoformat(received_at).astimezone(LOCAL_TZ)
        difference = round((received - event).total_seconds())
        status = "ok" if abs(difference) <= CLOCK_MAX_SKEW else "delayed_or_clock_skew"
        return difference, status
    except ValueError:
        return None, "invalid_device_time"


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def initialize():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS devices(
          serial_number TEXT PRIMARY KEY, ip TEXT, firmware TEXT,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS raw_requests(
          id INTEGER PRIMARY KEY AUTOINCREMENT, request_hash TEXT UNIQUE NOT NULL,
          serial_number TEXT NOT NULL, table_name TEXT, query_string TEXT,
          body BLOB NOT NULL, received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attendance(
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
          serial_number TEXT NOT NULL, employee_id TEXT NOT NULL,
          event_time TEXT NOT NULL, status TEXT, verify_mode TEXT,
          work_code TEXT, reserved TEXT, raw_line TEXT NOT NULL,
          received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS employees(
          serial_number TEXT NOT NULL, employee_id TEXT NOT NULL,
          name TEXT, privilege TEXT, card TEXT, raw_line TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(serial_number, employee_id));
        CREATE INDEX IF NOT EXISTS attendance_time_idx ON attendance(event_time);
        CREATE INDEX IF NOT EXISTS attendance_employee_idx ON attendance(employee_id);
        CREATE INDEX IF NOT EXISTS attendance_device_idx ON attendance(serial_number);
        """)
        columns = {row[1] for row in con.execute("PRAGMA table_info(attendance)")}
        if "delivery_delay_seconds" not in columns:
            con.execute("ALTER TABLE attendance ADD COLUMN delivery_delay_seconds INTEGER")
        if "time_status" not in columns:
            con.execute("ALTER TABLE attendance ADD COLUMN time_status TEXT NOT NULL DEFAULT 'unknown'")
        pending = con.execute("""SELECT id,event_time,received_at FROM attendance
          WHERE time_status = 'unknown' OR delivery_delay_seconds IS NULL""").fetchall()
        for row in pending:
            delay_seconds, time_status = timestamp_diagnostics(row[1], row[2])
            con.execute("""UPDATE attendance SET delivery_delay_seconds=?,time_status=?
              WHERE id=?""", (delay_seconds, time_status, row[0]))


def save_push(sn, table, query, body, remote_ip):
    received = now()
    request_hash = hashlib.sha256(sn.encode() + b"\0" + query.encode() + b"\0" + body).hexdigest()
    text = body.decode("utf-8", "replace")
    inserted = 0
    with LOCK, db() as con:
        con.execute("""INSERT INTO devices(serial_number,ip,first_seen,last_seen)
          VALUES(?,?,?,?) ON CONFLICT(serial_number) DO UPDATE SET
          ip=excluded.ip,last_seen=excluded.last_seen""", (sn, remote_ip, received, received))
        con.execute("""INSERT OR IGNORE INTO raw_requests
          (request_hash,serial_number,table_name,query_string,body,received_at)
          VALUES(?,?,?,?,?,?)""", (request_hash, sn, table, query, body, received))
        if table == "ATTLOG" or not table:
            for line in text.splitlines():
                match = ATT_RE.match(line.strip())
                if not match:
                    continue
                employee, event_time, status, verify, work, reserved = match.groups()
                delay_seconds, time_status = timestamp_diagnostics(event_time, received)
                event_key = hashlib.sha256((sn + "\0" + line.strip()).encode()).hexdigest()
                cur = con.execute("""INSERT OR IGNORE INTO attendance
                  (event_key,serial_number,employee_id,event_time,status,verify_mode,
                   work_code,reserved,raw_line,received_at,delivery_delay_seconds,time_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (event_key, sn, employee, event_time, status or "", verify or "",
                   work or "", reserved or "", line.strip(), received,
                   delay_seconds, time_status))
                inserted += cur.rowcount
        for line in text.splitlines():
            match = USER_RE.match(line.strip())
            if not match:
                continue
            employee_id, fields_text = match.groups()
            fields = {}
            for item in fields_text.split("\t"):
                if "=" in item:
                    key, value = item.split("=", 1)
                    fields[key] = value
            con.execute("""INSERT INTO employees
              (serial_number,employee_id,name,privilege,card,raw_line,updated_at)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(serial_number,employee_id) DO UPDATE SET
              name=excluded.name,privilege=excluded.privilege,card=excluded.card,
              raw_line=excluded.raw_line,updated_at=excluded.updated_at""",
              (sn, employee_id, fields.get("Name", ""), fields.get("Pri", ""),
               fields.get("Card", ""), line.strip(), received))
    return inserted


def rows_json(rows):
    return [dict(row) for row in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "SenseFaceHRMS/1.0"

    def log_message(self, fmt, *args):
        print(f"{now()} {self.client_address[0]} {fmt % args}", flush=True)

    def send(self, status=200, body=b"OK", content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=200):
        self.send(status, json.dumps(value, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def authorized(self):
        return not API_KEY or self.headers.get("X-API-Key") == API_KEY

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        uri = urlparse(self.path)
        q = parse_qs(uri.query)
        sn = q.get("SN", ["UNKNOWN"])[0]
        if uri.path == "/iclock/cdata":
            stamp = "0"
            reply = "\n".join([
                f"GET OPTION FROM: {sn}", f"ATTLOGStamp={stamp}", "OPERLOGStamp=0",
                "ATTPHOTOStamp=0", "ErrorDelay=60", "Delay=10",
                "TransTimes=00:00", "TransInterval=1",
                "TransFlag=TransData AttLog OpLog", f"TimeZone={timezone_hours()}",
                f"DateTime={device_now()}",
                "Realtime=1", "Encrypt=0", "PushProtVer=2.4.1", ""
            ])
            self.send(body=reply)
        elif uri.path == "/iclock/getrequest":
            current = time.monotonic()
            with LOCK:
                last_sync = LAST_TIME_SYNC.get(sn, 0)
                if current - last_sync >= CLOCK_SYNC_INTERVAL:
                    LAST_TIME_SYNC[sn] = current
                    command_id = int(time.time())
                    reply = f"C:{command_id}:DATA UPDATE options DateTime={device_now()}\n"
                else:
                    reply = "\n"
            self.send(body=reply)
        elif uri.path == "/health":
            with db() as con:
                count = con.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
                suspicious = con.execute("""SELECT COUNT(*) FROM attendance
                  WHERE time_status != 'ok'""").fetchone()[0]
                devices = rows_json(con.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall())
            self.json({"status": "ok", "server_time": device_now(),
                       "timezone": TIMEZONE_NAME, "attendance_count": count,
                       "suspicious_time_count": suspicious, "devices": devices})
        elif uri.path in ("/api/v1/attendance", "/api/v1/attendance.csv"):
            if not self.authorized():
                return self.json({"error": "unauthorized"}, 401)
            self.attendance(uri, q)
        elif uri.path == "/api/v1/employees":
            if not self.authorized():
                return self.json({"error": "unauthorized"}, 401)
            with db() as con:
                records = rows_json(con.execute(
                    "SELECT * FROM employees ORDER BY serial_number, employee_id"
                ).fetchall())
            self.json({"count": len(records), "data": records})
        else:
            self.json({"error": "not_found"}, 404)

    def attendance(self, uri, q):
        limit = min(max(int(q.get("limit", ["100"])[0]), 1), 10000)
        after_id = max(int(q.get("after_id", ["0"])[0]), 0)
        clauses, args = ["id > ?"], [after_id]
        for key, column in (("employee_id", "employee_id"), ("serial_number", "serial_number")):
            if q.get(key):
                clauses.append(f"{column} = ?")
                args.append(q[key][0])
        if q.get("from"):
            clauses.append("event_time >= ?"); args.append(q["from"][0])
        if q.get("to"):
            clauses.append("event_time <= ?"); args.append(q["to"][0])
        qualified = [clause.replace("id >", "a.id >").replace("event_time", "a.event_time").replace("employee_id", "a.employee_id").replace("serial_number", "a.serial_number") for clause in clauses]
        sql = """SELECT a.*, e.name AS employee_name FROM attendance a
          LEFT JOIN employees e ON e.serial_number=a.serial_number AND e.employee_id=a.employee_id
          WHERE """ + " AND ".join(qualified) + " ORDER BY a.id LIMIT ?"
        with db() as con:
            records = rows_json(con.execute(sql, args + [limit]).fetchall())
        if uri.path.endswith(".csv"):
            output = io.StringIO()
            fields = list(records[0]) if records else ["id","serial_number","employee_id","event_time","status","verify_mode","work_code"]
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader(); writer.writerows(records)
            self.send(body=output.getvalue(), content_type="text/csv; charset=utf-8")
        else:
            self.json({"count": len(records), "next_after_id": records[-1]["id"] if records else after_id, "data": records})

    def do_POST(self):
        uri = urlparse(self.path)
        q = parse_qs(uri.query)
        if uri.path not in ("/iclock/cdata", "/iclock/devicecmd", "/iclock/registry"):
            return self.json({"error": "not_found"}, 404)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        sn = q.get("SN", q.get("sn", ["UNKNOWN"]))[0]
        table = q.get("table", [""])[0].upper()
        inserted = save_push(sn, table, uri.query, body, self.client_address[0])
        self.send(body=f"OK: {inserted}" if inserted else "OK")


def main():
    initialize()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"SenseFace HRMS Bridge listening on http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()




