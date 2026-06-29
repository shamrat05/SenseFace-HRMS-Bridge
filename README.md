# SenseFace HRMS Bridge

Standalone receiver for ZKTeco SenseFace attendance terminals using the T&A Push/ADMS HTTP protocol. It requires no CVAccess, Node package, Python package, or cloud service.

## Device configuration

- Device Type: `T&A Push`
- Server Mode: `ADMS`
- Server Address: `192.168.0.101` (this PC)
- Server Port: `8090`
- Domain and proxy: disabled

Keep this PC's LAN address static or reserve it in the router.

## Run

1. Close CVAccess and any previous ADMS receiver using port 8090.
2. Double-click `Start-SenseFace-Bridge.bat`.
3. Keep the window open. The SQLite database is created automatically at `data/senseface_hrms.db`.
4. Check `http://127.0.0.1:8090/health`.

Windows Firewall must allow inbound TCP 8090. The existing `SenseFace_ADMS_8090` rule can be retained.

## Data-safety behavior

- Every raw device POST is committed to SQLite before success is returned.
- SQLite uses WAL mode, full synchronization, and a 30-second busy timeout.
- Re-sent device requests and attendance events are deduplicated by SHA-256 keys.
- Existing records are never cleared by the server.
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
