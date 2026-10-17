# Dashboard plugins and upstream updates

NAS, VPN and virtualization are bundled plugins. All three are enabled by default, preserving `/nas`, `/vpn`, `/virtualization`, their `/api` endpoints, saved configuration files and MQTT topic/entity IDs. MQTT settings, broker connectivity, eero metrics and live WAN sensors remain core functionality. Switches and discovery also remain in core for now.

Set `EERO_DASHBOARD_PLUGINS=nas,vpn,virtualization` to enable all three, `EERO_DASHBOARD_PLUGINS=vpn` to enable only VPN, or an empty value to run without plugins. Restart the backend after changing this setting. Disabled plugins have no API routes, sidebar links or running collectors, and their Python modules are not imported. Existing retained Home Assistant entities from a disabled plugin expire; disabling a plugin does not erase its saved configuration or retained discovery records.

Plugins are trusted source code shipped with the application. Adding one requires a rebuild; this is not a remote plugin marketplace or a sandbox for untrusted code.

## Adding a plugin

1. Create `backend/app/plugins/<id>/plugin.py`. Export a `Plugin(id, router, lifespan)` from `app.plugins`. The router is mounted under `/api/<id>` by default. An optional `api_prefix` preserves existing routes; virtualization uses `/api/networks` for its existing network-scoped endpoints. Require `require_auth` on the router and preserve the application's normal CSRF protection on writes. Use an async context manager for startup and shutdown; stop workers before returning from shutdown. The registry discovers installed packages and imports only configured IDs. Plugin request-validation errors omit invalid input values so credentials are not echoed.
2. Keep collectors, settings helpers, routes and models within the plugin package. If publishing MQTT, subclass `MQTTIntegration`, register the adapter with `app.state.mqtt_service.add_extension(id, adapter)`, and publish through the shared broker. Hooks cover reconnect, messages, discovery cleanup, snapshots and shutdown. Core MQTT owns the broker thread, lock, availability and network/WAN sensors.
3. Create `frontend/src/lib/plugins/<id>/plugin.ts` exporting a `DashboardPlugin` manifest with `id`, `label`, `icon`, and a lazy `load()` returning its Svelte page. The shared registry discovers manifests, intersects them with authenticated `/api/plugins` discovery, and supplies sidebar links and the `/<id>` page host. Use an ID that does not collide with an existing core route.
4. Put frontend API methods, types and components beside the manifest. Reuse `fetchWithHandling` for authenticated requests, error handling and CSRF headers. Optional `mqttInfo()` contributes plugin-specific help to the MQTT page. Register cached account data with `onSessionReset` to clear it on logout or session expiry.
5. Add the ID to the configured plugin list and test enabled and disabled operation. Existing plugin tests cover monitoring, controls, credentials, Home Assistant discovery and MQTT reconnect behavior.

## Keeping the fork synchronized

The `upstream` Git remote points to `https://github.com/fulviofreitas/eero-ui.git`. Preserve merge history instead of rebasing the shared `master` branch:

```sh
git status --short
git fetch upstream master
git merge --no-edit upstream/master
# Resolve conflicts, then run checks before pushing.
```

The `Sync upstream` GitHub workflow checks every Monday at 07:23 UTC and can also be run manually. It merges upstream into `automation/upstream-sync` and opens or updates a PR against `master`. It never merges the PR automatically. Conflicts fail the run and list affected files in the run summary, leaving `master` untouched. The automation branch is reserved for the workflow; make manual resolutions on a separate branch.

After pushing the workflow, enable scheduled workflows on the fork and allow GitHub Actions to create pull requests in repository Actions settings. With the default `GITHUB_TOKEN`, generated PRs do not trigger other workflows automatically; run CI manually before merging. For normal PR-triggered CI, configure an optional `UPSTREAM_SYNC_TOKEN` secret with access to repository contents, pull requests and workflows (upstream updates may change workflow files). The workflow uses that token for checkout, pushing and creating the PR.

Verification commands:

```sh
cd backend
uv sync --extra dev
uv run pytest tests
cd ../frontend
npm ci
npm run check
npm run lint
npm test
npm run build
```
