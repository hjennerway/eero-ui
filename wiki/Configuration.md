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
