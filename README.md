# SenseFace HRMS Bridge

Standalone HTTP receiver for ZKTeco SenseFace 2A and compatible T&A Push/ADMS
terminals. It receives attendance and employee records, stores them in SQLite,
and exposes JSON and CSV APIs for HRMS integration.

No ZKTeco desktop software, Python package, or cloud service is required.

## How it works

```text
SenseFace 2A --T&A Push/ADMS--> Bridge --JSON/CSV API--> HRMS
                                      |
                                      +--> SQLite
```

The terminal pushes records to the bridge. This is different from older ZKTeco
devices that are queried through port 4370.

## Choose a deployment

- **Railway:** easiest public deployment; configure the terminal with Railway's
  domain and enable HTTPS.
- **VPS:** use a public IP and port, or place the bridge behind an HTTPS reverse
  proxy.
- **Local PC:** suitable when the terminal and computer share the same LAN.

Only run one bridge replica when using SQLite.

## Railway setup

1. Deploy this GitHub repository as a Railway service.
2. Use this start command:

   ```text
   python -u server.py
   ```

3. Generate a domain under **Settings > Networking > Public Networking**.
4. Attach a persistent volume mounted at:

   ```text
   /app/data
   ```

   This is required. Without the volume, redeployment can erase the SQLite
   database.

5. Add these service variables:

   ```text
   SENSEFACE_TIMEZONE=Asia/Dhaka
   SENSEFACE_API_KEY=replace-with-a-long-random-secret
   ```

   The bridge automatically reads Railway's `PORT` variable. Do not expose the
   container's internal port directly.

6. Confirm this URL returns `"status": "ok"`:

   ```text
   https://YOUR-SERVICE.up.railway.app/health
   ```

### SenseFace 2A settings for Railway

Open the terminal's **Cloud Server Settings** and configure:

```text
Server/Protocol mode: T&A Push / ADMS
Enable domain name:   Yes
Server address:       YOUR-SERVICE.up.railway.app
Enable HTTPS:         Yes
Proxy:                Disabled
```

Enter only the hostname. Do not include `https://`, a port, `/health`, or a
trailing slash. Railway uses HTTPS port 443 automatically.

Save the settings and restart the terminal. Within about one minute, Railway
logs should contain requests such as:

```text
GET  /iclock/cdata?SN=...
GET  /iclock/getrequest?SN=...
POST /iclock/cdata?SN=...
```

Railway redirects plain HTTP to HTTPS, so **HTTPS must be enabled on the
terminal**. Browser `/health` requests alone do not prove the terminal connected.

## VPS setup

Requirements:

- Python 3.9 or newer
- A static public IP or domain
- Persistent disk storage
- Firewall access to the selected port

Run the bridge:

```bash
export SENSEFACE_TIMEZONE=Asia/Dhaka
export SENSEFACE_API_KEY='replace-with-a-long-random-secret'
export SENSEFACE_PORT=8090
python -u server.py
```

For production, run it through systemd, Docker, or another process supervisor.
Back up the `data` directory.

### Direct VPS IP and port

Allow inbound TCP port 8090, then configure the terminal:

```text
Server/Protocol mode: T&A Push / ADMS
Enable domain name:   No
Server address:       YOUR_PUBLIC_IP
Server port:          8090
Enable HTTPS:         No
Proxy:                Disabled
```

Do not use this option if the public network is untrusted. Prefer HTTPS through
a domain and reverse proxy.

### VPS domain with HTTPS

Point a domain to the VPS and configure Nginx, Caddy, or another reverse proxy to
terminate HTTPS and forward requests to `http://127.0.0.1:8090`. Then use:

```text
Enable domain name:   Yes
Server address:       attendance.example.com
Enable HTTPS:         Yes
Proxy:                Disabled
```

The reverse proxy must preserve request methods, query strings, and POST bodies.

## Local Windows setup

1. Reserve a static LAN IP for the computer.
2. Allow inbound TCP 8090 in Windows Firewall.
3. Close other ADMS receivers using port 8090.
4. Run `Start-SenseFace-Bridge.bat`.
5. Open `http://127.0.0.1:8090/health`.

Configure the terminal:

```text
Server/Protocol mode: T&A Push / ADMS
Enable domain name:   No
Server address:       COMPUTER_LAN_IP
Server port:          8090
Enable HTTPS:         No
Proxy:                Disabled
```

The terminal and computer must be able to reach each other on the LAN.

## Verify the connection

Open:

```text
GET /health
```

After the terminal connects, `devices` should contain its serial number. Make a
test punch, then open:

```text
GET /api/v1/attendance?after_id=0&limit=100
```

If `SENSEFACE_API_KEY` is configured, include:

```http
X-API-Key: your-secret
```

Troubleshooting checklist:

1. Verify the terminal date, time, timezone, gateway, and DNS.
2. Confirm `/health` works from outside the hosting network.
3. Look for `/iclock/` requests in server logs.
4. For Railway, confirm domain mode and HTTPS are both enabled.
5. Confirm the database volume or disk is writable and persistent.

## Attendance API

JSON:

```http
GET /api/v1/attendance?after_id=0&limit=500
```

CSV:

```http
GET /api/v1/attendance.csv?from=2026-06-01&to=2026-07-01
```

Optional filters:

- `after_id`
- `limit` (maximum 10,000)
- `employee_id`
- `serial_number`
- `from`
- `to`

Save `next_after_id` in the HRMS and use it as the next cursor. Add a unique
constraint on `event_key` in the consuming system.

Example:

```js
const response = await fetch(
  `${process.env.SENSEFACE_URL}/api/v1/attendance?after_id=${cursor}&limit=500`,
  { headers: { "X-API-Key": process.env.SENSEFACE_API_KEY } },
);
if (!response.ok) throw new Error(`SenseFace API ${response.status}`);
const page = await response.json();
```

## Employee API

```http
GET /api/v1/employees
```

Attendance includes `employee_name`. If attendance arrives before its USER
record, the bridge creates a placeholder, requests a full USERINFO sync, and
backfills the name after the terminal supplies it.

## Time handling

- `event_time` is the terminal's local punch time.
- `received_at` and employee `updated_at` are stored in UTC.
- `time_status=ok` means event and receipt times are within the configured
  tolerance.
- `time_status=delayed_or_clock_skew` means the punch was uploaded later or the
  device clock may have been wrong.

Defaults:

```text
SENSEFACE_TIMEZONE=Asia/Dhaka
SENSEFACE_CLOCK_MAX_SKEW=300
```

The bridge supplies its local time during ADMS registration. Also configure the
terminal's timezone and automatic time synchronization. Offline punches retain
the time recorded by the terminal; receipt time cannot reconstruct a wrong
offline device clock.

## Configuration variables

| Variable | Default | Purpose |
|---|---:|---|
| `PORT` | `8090` | Standard cloud-assigned port |
| `SENSEFACE_PORT` | unset | Overrides `PORT` |
| `SENSEFACE_HOST` | `0.0.0.0` | Bind address |
| `SENSEFACE_DATA_DIR` | `./data` | Persistent data directory |
| `SENSEFACE_TIMEZONE` | `Asia/Dhaka` | Terminal local timezone |
| `SENSEFACE_CLOCK_MAX_SKEW` | `300` | Allowed clock difference in seconds |
| `SENSEFACE_API_KEY` | empty | Protects `/api/v1/*` when set |

## Data safety and security

- Raw requests and parsed events are committed to SQLite before success is
  returned.
- WAL mode, full synchronization, and a busy timeout are enabled.
- Repeated requests and attendance events are deduplicated.
- Existing events are not automatically deleted or rewritten.
- Device endpoints must remain reachable by the terminal.
- Protect HRMS API endpoints with `SENSEFACE_API_KEY`.
- Use HTTPS for internet deployments.
- Back up persistent storage regularly.

## License

See [LICENSE](LICENSE).
