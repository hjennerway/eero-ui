# ⚙️ Configuration

## Environment Variables

All settings are read once at startup from the environment (`backend/app/config.py`). The prefix is `EERO_DASHBOARD_`.

| Variable | Description | Default |
|----------|-------------|---------|
| `EERO_DASHBOARD_HOST` | Server bind address | `0.0.0.0` |
| `EERO_DASHBOARD_PORT` | Server port | `8000` |
| `EERO_DASHBOARD_DEBUG` | Enable debug logging, CORS for the dev server, and `/api/docs` | `false` |
| `EERO_DASHBOARD_SESSION_SECRET` | **Required.** Session encryption key | *(none — must be set)* |
| `EERO_DASHBOARD_COOKIE_FILE` | Path of the eero credential file, owned by the `eero-api` SDK | `/data/session/session.json` (Docker) / `~/.eero-dashboard/session.json` (local) |
| `EERO_DASHBOARD_VICTORIA_METRICS_URL` | VictoriaMetrics base URL. Used for **both** the collector's writes and the chart reads | `http://127.0.0.1:8428` |
| `EERO_DASHBOARD_COLLECTION_INTERVAL` | Seconds between metrics collection cycles. Clamped to a minimum of `10` | `60` |
| `EERO_DASHBOARD_SWITCHES_FILE` | Path to a private JSON file defining read-only SNMP switches for topology | unset (disabled) |
| `EERO_DASHBOARD_NAS_FILE` | Path to a private JSON file defining QNAP NAS devices for disk health and storage usage | unset (disabled) |
| `EERO_DASHBOARD_DISCOVERY_FILE` | Path to the hourly LAN scanner's JSON snapshot | unset (disabled) |
| `EERO_DASHBOARD_METRICS_RETENTION` | How long the embedded VictoriaMetrics keeps data (`30d`, `6m`, `1y`). Shell-only: read by `container-start.sh`, not by Python | `1y` |
| `EERO_DASHBOARD_SDK_GET_RETRIES` | How many times the SDK retries a failed **GET** against the eero cloud. Clamped to `0`–`3`. Writes are never retried | `1` |
| `EERO_DASHBOARD_SDK_LEGACY_COOKIE` | Also send the session as the legacy `s=` cookie alongside the `X-User-Token` header. Off by default since 6.0 | `false` |
| `EERO_DASHBOARD_EXPERIMENTAL_WRITES` | Enable the write routes that are not verified end-to-end against a live eero network (see [[Roadmap]]). Off: those routes return `403` with `type: "experimental_disabled"` | `false` |
| `EERO_DASHBOARD_ACCOUNT_IDENTITY_WRITES` | Second, independent gate for `PUT /api/account/email`, `PUT /api/account/phone` and their `/verify` routes. Both this **and** `EERO_DASHBOARD_EXPERIMENTAL_WRITES` must be `true` | `false` |

Removed in 6.0 (setting either has no effect): `EERO_EXPORTER_SESSION_PATH`, `EERO_DASHBOARD_METRICS_ENDPOINT_ENABLED`.

## Session Secret

For production, always set a strong session secret:

```bash
export EERO_DASHBOARD_SESSION_SECRET=$(openssl rand -hex 32)
```

The `start.sh` script auto-generates and saves this to `.env` on first run. A secret shorter than 32 characters logs a warning at startup.

## Debug Mode

Enable debug mode for detailed logging:

```bash
EERO_DASHBOARD_DEBUG=true uvicorn app.main:app --reload
```

This also enables the interactive API docs at `/api/docs` and CORS for `http://localhost:5173`.

## Metrics

Metrics are collected by eero-ui's **own collector** (a task inside the FastAPI process) and written into the embedded VictoriaMetrics instance. There is no exporter and nothing is scraped. See [[Metrics]] for the metric contract.

- **Collection interval** — `EERO_DASHBOARD_COLLECTION_INTERVAL` sets how often the collector reads the eero API and writes one batch of samples. Minimum `10` seconds; the default `60` is a sensible floor for the eero cloud's rate limits.
- **Retention** — `EERO_DASHBOARD_METRICS_RETENTION` sets how long VictoriaMetrics keeps data. Accepted suffixes: `d` (days), `w` (weeks), `m` (months), `y` (years).
- **Storage size** — old data is dropped only when it exceeds the retention period. Disk usage grows with the number of devices and the collection interval.
- **External store** — because `EERO_DASHBOARD_VICTORIA_METRICS_URL` serves both the write and the read path, pointing it at an external VictoriaMetrics makes the embedded one idle. It still starts inside the container; that is harmless.

## Network switches

Topology can include managed switches using read-only **SNMP v2c**. The Netgear GS110TP has been verified with firmware 5.4.2.36. The collector uses standard IF-MIB, BRIDGE-MIB, Q-BRIDGE-MIB and LLDP-MIB tables; other models need to expose those tables. No switch settings are changed.

1. Configure a read-only SNMP community on the switch, accessible from the dashboard server. The management webpage's port is independent of SNMP, which normally uses UDP 161. Keep SNMP access restricted to your trusted LAN; v2c does not encrypt the community.
2. Copy `docs/switches.example.json` to a private file outside source control. Set its `host`, `community` and `network_id` (the ID in the dashboard's `/network/<id>` URL). Each switch needs a unique `id` and a display `name`.
3. Set `EERO_DASHBOARD_SWITCHES_FILE` to that file's absolute path and restart the dashboard. For Docker, add these entries to the existing service, preserving its other environment variables and volumes:

```yaml
environment:
  EERO_DASHBOARD_SWITCHES_FILE: /run/eero-ui/switches.json
volumes:
  - ./switches.json:/run/eero-ui/switches.json:ro
```

The container needs LAN access to the switch's UDP port; no extra inbound Docker port is needed. Credentials stay on the server and are never sent to the browser. Leaving the environment variable unset preserves the existing eero-only topology.

Switch data is read when topology loads or refreshes and cached for 30 seconds. There are limits of four switches per network and sixteen total, with a twelve-second deadline per switch. Only explicitly configured private IPv4 addresses are polled. Unreachable switches remain visible without using their old connection data.

Select a switch to see its port status, negotiated speeds, learned MAC counts and reported neighbours. **LLDP** identifies a neighbour reported on that port. Dashed **inferred** links use an unambiguous learned MAC address; they show the path through a port, which can include an unobserved switch. Eero Ethernet addresses are matched as well as their base MACs. Wireless clients stay with their eero, and clients learned behind an eero are not reassigned to its switch port.

Ambiguous observations keep the existing eero association. MAC manufacturers alone cannot reveal cabling, and unmanaged switches cannot reliably be discovered from MAC learning. This integration does not provide VLAN diagrams or SNMP v3 authentication.

To include a known unmanaged switch, add `unmanaged_switches` to its managed upstream switch's private configuration:

```json
"unmanaged_switches": [{
  "id": "office",
  "name": "Office switch",
  "port": 2,
  "eero_ids": ["YOUR_OFFICE_EERO_ID"],
  "devices": [{
    "mac": "02:00:00:00:00:01",
    "ip": "192.168.1.165",
    "name": "Office device"
  }]
}]
```

`port` is the upstream interface ID from `/api/switches`, not an assumed socket number. Wiring is labelled **User supplied**. The unmanaged switch has no invented management address, port inventory, speed or online status. Known clients missing from eero's inventory appear in topology using their configured MAC and IP; activity comes from MAC learning on the upstream port, and clients with no current observation are available with **Offline** enabled. Each device may belong to only one configured unmanaged switch per network. Nothing is polled on the unmanaged switch or its configured client IPs.

## QNAP NAS health and storage

Open **NAS** in the sidebar to see disk status, SMART health summaries, temperatures, disk capacity, volume usage, storage pools and RAID status. This integration uses read-only **SNMP v2c** and QNAP's NAS-MIB under `1.3.6.1.4.1.24681`. It prefers the Storage Manager tables and falls back to older EX/system disk and volume tables. Firmware exposing only a different MIB is not supported yet; unsupported tables appear as unavailable. SNMP v3 and detailed SMART attributes/self-test history are not included.

Some QTS firmware returns numeric volume sizes in KiB despite the MIB declaring bytes. The dashboard checks each volume against its unit-labelled legacy size before applying a conversion. Storage pool and RAID percentages describe allocation, including space assigned to volumes; volume percentages describe file usage. Queries allow three seconds per SNMP response and have a twenty-five-second overall limit per NAS.

1. In QTS, go to **Control Panel → Network & File Services → SNMP**, enable SNMP service and select **SNMP v1/v2**. Set a read-only community and permit access from the dashboard server to the configured UDP port (normally 161). Restrict v2c to your trusted LAN because its community is transmitted unencrypted. See [QNAP's SNMP settings guide](https://docs.qnap.com/operating-system/qts/5.2.x/en-us/configuring-snmp-settings-42DBF952.html).
2. Copy `docs/nas.example.json` to a private `nas.json`. The example uses your QNAP TVS-473e at `192.168.4.2`. Replace `community` and `network_id` with the actual values; the network ID is displayed on the NAS page and in the dashboard's `/network/<id>` URL. Keep credentials outside source control.
3. Set `EERO_DASHBOARD_NAS_FILE` to the private file's path and restart the dashboard. For Docker:

```yaml
environment:
  EERO_DASHBOARD_NAS_FILE: /run/eero-ui/nas.json
volumes:
  - ./nas.json:/run/eero-ui/nas.json:ro
```

The server must reach the NAS's UDP port; the QTS web/SSH ports are separate. There are no SNMP SET operations, disk tests, storage changes or network scans. Credentials remain server-side. Configurations support at most sixteen NAS devices total and four per network, with unique IDs and private LAN IPv4 addresses. Changes to the JSON file take effect on the next request.

The server polls configured NAS devices at startup and every five minutes, even when the NAS page is closed. The page also refreshes every five minutes and keeps readings in memory per network, so returning to it displays the cached data immediately. A full browser reload uses the server cache; the first poll after a server restart can still take time. Expired cached readings are labelled while a background refresh completes. **Refresh** requests a new poll immediately. Failed polls replace old readings with **Unreachable**; a page request error keeps the last readings visibly marked as cached. Missing SMART/status data remains **Unknown**, even when the NAS responds. Capacity usage is calculated from reported total/free bytes; inconsistent or missing readings are unavailable. Volume usage, pool allocation and RAID capacity describe different storage layers and should not be added together.

### Optional disk age and SSD life

QNAP's NAS-MIB does not expose disk age or estimated SSD life remaining. On the **NAS** page, choose **QTS connection** beside your NAS. Enter the QTS username, password and web port, choose HTTP/HTTPS and certificate verification settings, then use **Test connection** to check the details without saving. **Save connection** stores them on the dashboard server and refreshes the readings. A blank password keeps the saved password; **Remove connection** deletes the QTS login while retaining SNMP monitoring. The saved password is never returned to the browser. The account needs permission to read disk health, and interactive two-step login is not supported.

Settings are saved atomically in the private file configured by `EERO_DASHBOARD_NAS_FILE`, with owner-only permissions on POSIX systems. That file and its parent directory must be writable by the dashboard. A file mounted individually or read-only cannot be replaced atomically: use a writable directory/volume mount (such as `/data/session`) for UI-managed settings. Other NAS entries and SNMP credentials are preserved. No restart is needed.

Alternatively, add a `qts` object to the NAS entry in your private configuration; see `docs/nas-qts.example.json`:

```json
"qts": {
  "username": "YOUR_QTS_MONITORING_USER",
  "password": "YOUR_QTS_PASSWORD",
  "port": 443,
  "https": true,
  "verify_tls": true
}
```

The connection uses the same NAS IP as SNMP. Set the QTS web port separately from the SNMP UDP port. HTTPS and certificate verification are enabled by default. For a NAS using a self-signed certificate, either trust its certificate in the server environment or explicitly set `verify_tls` to `false`. An HTTP-only trusted LAN installation can set `https` to `false` and its web port (usually 8080); HTTP transmits login credentials without transport encryption.

The QTS account needs permission to read Storage & Snapshots disk health; some firmware requires membership in the administrators group. Interactive two-step login is not supported. Credentials and session IDs remain on the server and are never included in API/MQTT responses. The integration logs in, reads the disk inventory and health summaries, then logs out; it does not initiate SMART tests or alter disks. QTS reads have their own 25-second overall deadline, in addition to the SNMP poll.

**Power-on time** preserves QTS `power_on_hours` and displays accumulated drive use in years, months, days and hours, not calendar age or NAS uptime. Years and months are approximate: 365 days per year and 30 days per month, with the year's extra days kept in the days component after eleven months. Hover over the reading for the exact total hours. The API also retains `power_on_days` for older clients. **SSD life remaining** is QTS's reported `SSD_life_percentage`, only when the disk is an SSD and firmware reports support. Zero is a valid reading. Missing/unsupported values show **Unavailable**. Without QTS credentials, the page explains that a QTS connection is needed.

Disks are matched by physical enclosure and bay, never SNMP row order. The summary request uses `dev_id` with the inventory ID's colon removed, as QTS's disk-health UI does. Built-in drive summaries may return a decimal bay number even when the inventory uses a hexadecimal `enclosure:bay` ID. A QTS failure leaves SNMP health and storage readings available with a warning. These firmware-dependent requests and lifetime fields were verified against QTS 5.2.9 on a TVS-473.

## MQTT and Home Assistant

Open **MQTT** in the dashboard sidebar, enable MQTT, and enter your broker address and optional username/password. Addresses can be a hostname, `192.168.1.10:1883`, `mqtt://broker:1883`, or `mqtts://broker:8883` for TLS with certificate verification. IPv6 addresses use brackets, for example `mqtt://[::1]:1883`. The broker must be reachable from the dashboard container; `localhost` refers to that container.

Enable **Home Assistant auto discovery** and configure Home Assistant's MQTT integration to use the same broker. The default discovery prefix is `homeassistant`; change it only if your Home Assistant integration uses another prefix. [Home Assistant's MQTT documentation](https://www.home-assistant.io/integrations/mqtt/) describes the integration and discovery settings.

Each eero network appears as one device with four sensors: **Wired clients**, **Wireless clients**, **Latest speed test download**, and **Latest speed test upload**. Client counts include only connected devices of the corresponding connection type; unclassified devices are not guessed. Speeds are measured in megabits per second (`Mbit/s` in Home Assistant discovery) and use existing test results, without running new tests. Data is published on the existing background collection interval (60 seconds by default).

Configured QNAP devices also publish disk and volume information every five minutes when MQTT is enabled, with no browser required. Each volume has a retained JSON state at `eero_ui/<installation-id>/<network-key>/nas/<nas-key>/volumes/<volume-key>/state`. The network, NAS, volume and disk keys are stable hashes of their configured/reported IDs. Volume payloads include:

| Field | Reading |
| --- | --- |
| `capacity_bytes` | Total size in bytes |
| `free_bytes` | Available size in bytes |
| `used_bytes` | Used size in bytes |
| `used_percent` | Percentage used, from 0 to 100 |
| `status`, `health` | Reported status and normalized health |
| `id`, `name`, `raid_level`, `observed_at` | Volume identity, RAID level when reported, and observation timestamp |

Each disk has a retained JSON state at `eero_ui/<installation-id>/<network-key>/nas/<nas-key>/disks/<disk-key>/state`. It includes `health`, `smart_status`, `status`, `temperature_c`, `capacity_bytes`, exact `power_on_hours`, and `ssd_life_remaining_percent`, plus disk ID/name, enclosure, bay, model, legacy `power_on_days`, and `observed_at`. Power-on hours measure accumulated drive use, not NAS uptime. A saved QTS connection is needed for power-on hours and SSD life; unsupported or missing measurements are JSON `null`, while a reported zero remains zero. NAS addresses, login credentials and disk serial numbers are excluded. Pools and RAID groups are not mixed into volume usage.

With auto discovery enabled, each NAS appears as a separate QNAP device with six sensors per volume: total capacity, used space, free space, used percentage, status and health. Each disk adds seven sensors: health, SMART health, status, temperature, capacity, power-on hours and SSD life remaining. Existing volume entity identifiers remain unchanged. Failed NAS polls mark its sensors unavailable and publish unknown values. Broker disconnects also make them unavailable; readings expire after fifteen minutes without a successful observation. Reconnects and Home Assistant restarts reannounce discovery. Removed disks, volumes and NAS devices have their retained state and discovery topics cleared. SNMP-only NAS configurations support volume and disk health sensors; QTS credentials are needed only for disk lifetime readings.

If only wired/wireless client sensors appear, check that the running dashboard includes the latest MQTT changes. Older discovery payloads used `Mbps` for speed sensors; Home Assistant's `data_rate` class requires `Mbit/s`. After updating, saving enabled MQTT settings reconnects and republishes discovery. NAS sensors appear under the configured NAS name, separate from the eero network device. Verify that **NAS** shows disks and volumes and that `EERO_DASHBOARD_NAS_FILE` is configured in the running instance. In Home Assistant's MQTT integration, listening to `homeassistant/sensor/#` (or your configured discovery prefix) and `eero_ui/#` can distinguish missing discovery messages from unavailable NAS readings.

Settings are applied without restarting the dashboard. A blank password field preserves the saved password; **Remove saved password** clears it. Credentials are stored server-side in `mqtt.json` beside the eero session file (inside `/data` with the standard Docker setup), with owner-only permissions on POSIX systems. `EERO_DASHBOARD_MQTT_FILE` optionally overrides that path. The settings API returns only whether a password is saved.

State topics are `eero_ui/<installation-id>/<network-key>/<sensor>/state`. Discovery configuration and states are retained with QoS 1. Sensors have stable identifiers, reannounce when Home Assistant starts, and become unavailable when the publisher disconnects or collection expires. Missing measurements appear as unknown rather than zero. Disabling MQTT retains the entities as unavailable; disabling auto discovery removes their retained discovery configuration from the currently connected broker. MQTT reconnects automatically after connection failures. Moving to another broker leaves any old retained entities on the previous broker unavailable.

## Hourly LAN discovery

An optional ARP scanner finds local IPv4 devices that eero does not report, including devices with static IP addresses. Enable the `eero-discovery` worker and volume in `docker-compose.yml`, then mount its snapshot read-only into the dashboard and set `EERO_DASHBOARD_DISCOVERY_FILE=/discovery/arp.json`.

The worker runs on a **Linux Docker host** with `network_mode: host`, drops all capabilities except `NET_RAW`, and has no dashboard session or eero credentials. The dashboard retains its existing bridge networking and published ports. Set these worker variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `EERO_DISCOVERY_NETWORK_ID` | The eero network to associate with this physical LAN | required |
| `EERO_DISCOVERY_SUBNET` | Explicit, directly attached private IPv4 subnet, `/22`–`/30` | required |
| `EERO_DISCOVERY_INTERVAL` | Seconds between scans, minimum 3600 | `3600` |
| `EERO_DISCOVERY_FILE` | Persistent snapshot output | `/discovery/arp.json` |

Scanning starts immediately on first launch, then every hour by default, even when no browser is open. Restarts preserve the next due time. Each scan has a 45-second deadline, two attempts per host and a 64 Kbit/s sending limit. It uses the host's LAN route and refuses routed targets; it never substitutes the Docker bridge subnet. It sends ARP requests only, with no port scans, camera logins or eero writes.

Topology refreshes the discovery snapshot every minute while open. New devices are grouped under **ARP discovered**, with IP/MAC and last-seen time. Dotted grey links originate at the gateway as placeholders: ARP does **not** identify their switch, port or wireless connection. Devices already reported by eero, configured known clients, switches and eero addresses are deduplicated. Once eero reports a device, a topology refresh replaces its ARP placeholder with the normal device record.

Devices absent from a later scan are retained for seven days, marked unconfirmed and shown with **Offline** enabled. Failed or overdue scans never report old observations as current. ARP is restricted to the scanner's local broadcast domain, does not discover IPv6-only devices, and can miss sleeping or isolated clients.

## Write gates

Every write that eero-ui has **not** verified end-to-end against a live network sits behind `EERO_DASHBOARD_EXPERIMENTAL_WRITES`. With the flag off the routes exist but answer `403 {"type": "experimental_disabled"}`, and the UI hides the controls (the flag is reported by `GET /api/health` as `experimental_writes`). The verified writes — LED on/off and brightness, device rename and type, block and unblock (block live-verified 2026-10-06), guest network enable/disable and password, speed test, reboot one eero — are always available.

Account e-mail and phone changes additionally need `EERO_DASHBOARD_ACCOUNT_IDENTITY_WRITES=true`, because an eero-ui session is the whole eero account: whoever can reach the dashboard could otherwise re-home the account's recovery identity. See [[Security]].

## Upgrading to 6.0

6.0 is a breaking major. Read [[Troubleshooting#backup-and-rollback]] before starting; the full operational runbook (backup, deploy, smoke test, history-continuity check, rollback) is `claude/docs/runbook-6.0-upgrade.md` in the context repo.

What changes for an existing deployment:

- The Prometheus exporter is no longer embedded; eero-ui collects its metrics itself and writes them to VictoriaMetrics, and `EERO_EXPORTER_SESSION_PATH` and `EERO_DASHBOARD_METRICS_ENDPOINT_ENABLED` are removed.
- The `/metrics` endpoint is removed with no replacement; eero-ui is no longer a Prometheus scrape target, and nothing inside the container is scraped.
- Metrics beyond the eight the dashboard uses are no longer collected; run `ghcr.io/fulviofreitas/eero-prometheus-exporter:4.0.0` alongside and point your own Prometheus at it.
- `/api/metrics/*` now requires an authenticated session; the raw PromQL passthrough endpoints and two unused metrics routes are removed.
- Requires `eero-api >= 8.0.5` (8.0.5 sends a current client version string so the cloud advertises the full capability set, and sends the `custom` DHCP mode the API actually accepts); session transport, auth encodings and the credential record changed upstream. The credential file is migrated to schema 2 in place on first start — no re-login is needed.
- `POST /api/networks/{id}/speedtest` returns `202` and the result is read from `GET /api/networks/{id}/speedtests`; `SpeedTestResult` has a single normalised shape.
- Device block and unblock resolve the device MAC server-side; a device without a known MAC is rejected with `422`.
- eero-api errors map to `401/402/403/409/422/429/502/503` instead of a blanket `500`; `GET /api/auth/status` gains `reason`.
- Settings-class writes require confirmation and may restart every eero; unverified writes are hidden unless `EERO_DASHBOARD_EXPERIMENTAL_WRITES=true`.
- Every `POST`/`PUT`/`PATCH`/`DELETE` under `/api` requires the header `X-Requested-With: eero-ui`, else `403 {"type": "csrf"}`. The bundled frontend sends it; any script or non-browser client of your own that writes through the API must add it. See [[Security#csrf]].
- Account e-mail and phone changes additionally require `EERO_DASHBOARD_ACCOUNT_IDENTITY_WRITES=true` on top of the experimental flag. Both default to `false`.

Steps:

1. Back up the `eero-data` volume (session and VictoriaMetrics data) as described in the runbook.
2. Remove `EERO_EXPORTER_SESSION_PATH` and `EERO_DASHBOARD_METRICS_ENDPOINT_ENABLED` from your compose file or manifest. Decide whether you need `EERO_DASHBOARD_EXPERIMENTAL_WRITES` or `EERO_DASHBOARD_ACCOUNT_IDENTITY_WRITES`; leave both unset unless you do.
3. Start the 6.0 image against the same volume. Existing metric history stays continuous because the metric names and label sets are unchanged ([[Metrics#history-continuity]]).
4. Optionally run `scripts/check-history-continuity.sh` against the cutover timestamp.
