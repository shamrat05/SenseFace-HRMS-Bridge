# SenseFace HRMS Bridge

ZKTeco network attendance devices use two main communication models:

- **Pull devices:** Mostly older terminals. HR software connects to the device—commonly on port 4370—and downloads records.
- **Push devices:** Modern terminals such as **SenseFace 2A**. The device sends attendance records by HTTP to a configured server; port-4370 pull libraries do not work with this mode.

This project is a standalone T&A Push/ADMS HTTP receiver for SenseFace 2A and compatible ZKTeco push devices. It stores incoming records in SQLite and exposes them through JSON and CSV APIs for HRMS integration. It requires no CVAccess, Node package, Python package, or cloud service.

## Device configuration

- Device Type: `T&A Push`
- Server Mode: `ADMS`
- Server Address: `192.168.0.101` (this PC)
- Server Port: `8090`
- Domain and proxy: disabled

Keep this PC's LAN address static or reserve it in the router.

Set the terminal's timezone to UTC+06:00 (Dhaka) and enable its automatic time
synchronization. The bridge also supplies its local time during ADMS registration.

## Run

1. Close CVAccess and any previous ADMS receiver using port 8090.
2. Double-click `Start-SenseFace-Bridge.bat`.
3. Keep the window open. The SQLite database is created automatically at `data/senseface_hrms.db`.
4. Check `http://127.0.0.1:8090/health`.

Windows Firewall must allow inbound TCP 8090. The existing `SenseFace_ADMS_8090` rule can be retained.

## Cloud hosting

The bridge supports cloud platforms that provide a public HTTP endpoint and
forward requests to the application's internal port. It reads the standard
`PORT` environment variable used by Railway, Render and similar platforms.
`SENSEFACE_PORT` takes precedence when explicitly configured.

Required hosting configuration:

- Start command: `python -u server.py`
- Bind address: `0.0.0.0` (the default)
- Health check path: `/health`
- Persistent storage mounted at the application's `data` directory, or set
  `SENSEFACE_DATA_DIR` to the persistent mount path
- One running replica when using SQLite

### Railway

1. Deploy the GitHub repository and generate a Public Networking domain.
2. Let Railway provide `PORT`, or set the domain target port to `8090` when
   explicitly setting `SENSEFACE_PORT=8090`.
3. Attach a volume mounted at `/app/data`. Without it, SQLite records are lost
   on redeploy.
4. Set `SENSEFACE_TIMEZONE=Asia/Dhaka` and a strong `SENSEFACE_API_KEY`.
5. Confirm `https://YOUR-DOMAIN/health` returns `status: ok`.

For a SenseFace domain-mode screen that has no port field, configure:

- Domain name: enabled
- Server address: the hostname only, such as
  `senseface-hrms-bridge-production.up.railway.app`
- Do not enter `http://`, `https://`, a path, or a trailing slash
- Proxy: disabled

With no port field the terminal normally uses HTTP port 80. The cloud ingress
must accept plain HTTP on port 80 and forward it to the application; a mandatory
HTTP-to-HTTPS redirect may not be followed by the terminal firmware. This cannot
be repaired inside the Python application because a rejected or redirected
request never reaches it. If the terminal provides HTTPS, enable it and use the
platform's HTTPS endpoint. If it provides a configurable port, Railway TCP Proxy
can instead forward a generated public port to the internal application port.

Successful device connection appears in logs as requests to
`/iclock/cdata` and `/iclock/getrequest`. Browser requests to `/health` only prove
the web service is reachable; they do not prove the terminal has connected.

## Data-safety behavior

- Every raw device POST is committed to SQLite before success is returned.
- SQLite uses WAL mode, full synchronization, and a 30-second busy timeout.
- Re-sent device requests and attendance events are deduplicated by SHA-256 keys.
- Existing records are never cleared by the server.
- `time_status` is `ok` when device time and receipt time are within five minutes;
  otherwise it is `delayed_or_clock_skew`. A delayed offline upload is not
  automatically rewritten because receipt time is not the original punch time.
- Back up the complete `data` directory while the server is stopped, or use SQLite's backup API while running.

## REST API

JSON:

```http
GET /api/v1/attendance?after_id=0&limit=100
```

Optional filters: `employee_id`, `serial_number`, `from`, and `to`.

CSV:

```http
GET /api/v1/attendance.csv?from=2026-06-01&to=2026-07-01
```

The response contains `next_after_id`. Save this cursor in your HRMS and use it on the next request. Processing by monotonically increasing ID plus your own unique constraint gives reliable incremental synchronization.

## Node.js / Next.js example

```js
const response = await fetch(
  'http://192.168.0.101:8090/api/v1/attendance?after_id=0&limit=500',
  { headers: process.env.SENSEFACE_API_KEY
      ? { 'X-API-Key': process.env.SENSEFACE_API_KEY }
      : {} }
);
if (!response.ok) throw new Error(`SenseFace API ${response.status}`);
const page = await response.json();
for (const event of page.data) {
  // Upsert using event.event_key, then persist page.next_after_id.
}
```

## NestJS example

```ts
const page = await fetch(
  `${process.env.SENSEFACE_URL}/api/v1/attendance?after_id=${cursor}&limit=500`,
  { headers: process.env.SENSEFACE_API_KEY
      ? { 'X-API-Key': process.env.SENSEFACE_API_KEY }
      : {} },
).then(r => {
  if (!r.ok) throw new Error(`SenseFace API ${r.status}`);
  return r.json();
});
```

Place a unique database constraint on `event_key` in the consuming application.

## Optional API key

Before starting, set an environment variable. Device endpoints remain available, while `/api/v1/*` requires the header `X-API-Key`.

```powershell
$env:SENSEFACE_API_KEY = 'replace-with-a-long-random-value'
.\Start-SenseFace-Bridge.ps1
```

Do not expose port 8090 directly to the public internet. Put an authenticated HTTPS reverse proxy or VPN in front if remote access is required.

## Clock configuration

The defaults are suitable for Bangladesh:

```powershell
$env:SENSEFACE_TIMEZONE = 'Asia/Dhaka'
$env:SENSEFACE_CLOCK_MAX_SKEW = '300'
```

`event_time` remains the timestamp recorded by the terminal. The API additionally
returns `delivery_delay_seconds` and `time_status`, allowing the HRMS to quarantine
suspicious records instead of silently accepting a bad device clock.

When attendance arrives before its USER record, the bridge creates an employee
placeholder and requests a full `USERINFO` migration from the terminal. USER data
subsequently replaces the placeholder and backfills `employee_name` on attendance.

## Employee directory

Endpoint: GET /api/v1/employees

Employee names are stored in the employees table. Attendance JSON includes employee_name when the device has pushed the corresponding USER record.

## Timezone note

The time differences in the data are intentional, not timing bugs:

- `event_time` — device-local Bangladesh time, e.g. `13:46:45`
- `received_at` — server UTC (+00:00), e.g. `07:46:46`

07:46 UTC + 6 hours = 13:46 Bangladesh time. The two values are consistent once you account for the offset.

Similarly, `updated_at` on employee records stores when the server received/synchronised the data (UTC), not the employee creation time.

UTC storage is safer for databases, but the different formats can be confusing. The HRMS UI should convert `received_at` and `updated_at` to `Asia/Dhaka` before displaying them.

