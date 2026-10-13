<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$api/client';
	import type {
		VPNConnection,
		VPNConnectionUpdate,
		VPNHistory,
		VPNSnapshot,
		VPNTunnel
	} from '$api/types';
	import Button from '$components/common/Button.svelte';
	import Card from '$components/common/Card.svelte';
	import StatusBadge from '$components/common/StatusBadge.svelte';
	import TimeRangeSelector from '$components/common/TimeRangeSelector.svelte';
	import TimeSeriesChart from '$components/charts/TimeSeriesChart.svelte';
	import { seriesColor } from '#lib/charts/defaults.js';

	let snapshot: VPNSnapshot | null = $state(null);
	let connection: VPNConnection | null = $state(null);
	let history: VPNHistory | null = $state(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let saving = $state(false);
	let controlling: 'connect' | 'disconnect' | null = $state(null);
	let controlError: string | null = $state(null);
	let controlMessage: string | null = $state(null);
	let error: string | null = $state(null);
	let historyError: string | null = $state(null);
	let connectionError: string | null = $state(null);
	let showConnection = $state(false);
	let enabled = $state(true);
	let host = $state('192.168.4.10');
	let port = $state(22);
	let username = $state('root');
	let tunnelInterface = $state('tun0');
	let controlMode: 'openvpn' | 'network' = $state('openvpn');
	let controlTarget = $state('');
	let password = $state('');
	let tunnels: VPNTunnel[] | null = $state(null);
	let discovering = $state(false);
	let tunnelError: string | null = $state(null);
	let tunnelKey: string | null = $state(null);
	const connectionKey = $derived(JSON.stringify([host, port, username, password]));
	const availableTunnels: VPNTunnel[] | null = $derived.by(() =>
		tunnelKey === connectionKey ? tunnels : null
	);
	const matchingTunnels: VPNTunnel[] = $derived.by(
		() => availableTunnels?.filter((tunnel) => tunnel.control_mode === controlMode) ?? []
	);
	let timeRange: '1h' | '6h' | '24h' = $state('1h');
	let active = false;
	let historyGeneration = 0;
	const canControl = $derived.by(() =>
		Boolean(connection?.enabled && connection.control_target && connection.host_key)
	);
	const ranges: { value: '1h' | '6h' | '24h'; label: string }[] = [
		{ value: '1h', label: '1h' },
		{ value: '6h', label: '6h' },
		{ value: '24h', label: '24h' }
	];
	const chartGap = $derived(timeRange === '1h' ? 90000 : timeRange === '6h' ? 240000 : 600000);
	const throughput = $derived.by(() => [
		{
			label: 'Download',
			data: history?.download_mbps ?? [],
			borderColor: seriesColor(0),
			maxGapMs: chartGap
		},
		{
			label: 'Upload',
			data: history?.upload_mbps ?? [],
			borderColor: seriesColor(1),
			maxGapMs: chartGap
		}
	]);
	const clientHistory = $derived.by(() => [
		{
			label: 'Connected clients',
			data: history?.connected_clients ?? [],
			borderColor: seriesColor(2),
			stepped: true,
			maxGapMs: chartGap
		}
	]);
	const statusHistory = $derived.by(() => [
		{
			label: 'Connection status',
			data: history?.connection_status ?? [],
			borderColor: seriesColor(3),
			stepped: true,
			maxGapMs: chartGap
		}
	]);

	function speed(value: number | null | undefined) {
		return value == null
			? '—'
			: value.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
	}
	function fillConnection(value: VPNConnection) {
		connection = value;
		enabled = value.enabled;
		host = value.host;
		port = value.port;
		username = value.username;
		tunnelInterface = value.interface;
		controlMode = value.control_mode;
		controlTarget = value.control_target;
		password = '';
		tunnels = null;
		tunnelKey = null;
	}
	function connectionUpdate(): VPNConnectionUpdate {
		return {
			enabled,
			host,
			port,
			username,
			interface: tunnelInterface,
			control_mode: controlMode,
			control_target: controlTarget.trim(),
			password: password || null
		};
	}
	function selectOnlyTunnel() {
		if (matchingTunnels.some((tunnel) => tunnel.name === controlTarget)) return;
		if (matchingTunnels.length === 1) controlTarget = matchingTunnels[0].name;
		else if (matchingTunnels.length > 1) controlTarget = '';
	}
	function changeTunnelType(event: Event) {
		controlMode = (event.currentTarget as HTMLSelectElement).value as 'openvpn' | 'network';
		controlTarget = '';
		selectOnlyTunnel();
	}
	async function findTunnels() {
		if (discovering || saving || controlling) return;
		discovering = true;
		tunnelError = null;
		const key = connectionKey;
		try {
			const result = await api.vpn.tunnels(connectionUpdate());
			if (!active || !showConnection || key !== connectionKey) return;
			tunnels = result;
			tunnelKey = key;
			if (result.length === 1) controlMode = result[0].control_mode;
			selectOnlyTunnel();
		} catch (cause) {
			if (active && key === connectionKey) {
				tunnels = null;
				tunnelKey = null;
				tunnelError = cause instanceof Error ? cause.message : 'Could not find VPN tunnels.';
			}
		} finally {
			if (active) discovering = false;
		}
	}
	async function loadHistory() {
		const current = ++historyGeneration;
		try {
			const result = await api.vpn.history(timeRange);
			if (active && current === historyGeneration) {
				history = result;
				historyError = null;
			}
		} catch (cause) {
			if (active && current === historyGeneration)
				historyError = cause instanceof Error ? cause.message : 'Could not load VPN history.';
		}
	}
	async function refresh() {
		if (refreshing || saving || controlling) return;
		refreshing = true;
		try {
			const result = await api.vpn.get();
			if (!active) return;
			snapshot = result;
			error = null;
		} catch (cause) {
			if (active) {
				error = cause instanceof Error ? cause.message : 'Could not load VPN metrics.';
				if (snapshot)
					snapshot = {
						...snapshot,
						stale: true,
						status: 'unknown',
						connected_clients: null,
						connected_client_ips: null,
						download_mbps: null,
						upload_mbps: null
					};
			}
		} finally {
			if (active) {
				refreshing = false;
				loading = false;
			}
		}
		if (active) await loadHistory();
	}
	async function saveConnection(event: SubmitEvent) {
		event.preventDefault();
		if (saving || controlling || refreshing || discovering) return;
		saving = true;
		connectionError = null;
		const update = connectionUpdate();
		password = '';
		tunnels = null;
		tunnelKey = null;
		try {
			const result = await api.vpn.saveConnection(update);
			if (!active) return;
			fillConnection(result);
			showConnection = false;
			history = null;
		} catch (cause) {
			if (active)
				connectionError =
					cause instanceof Error ? cause.message : 'Could not save the SSH connection.';
		} finally {
			if (active) saving = false;
		}
		if (active) await refresh();
	}
	async function controlTunnel(action: 'connect' | 'disconnect') {
		if (!canControl || controlling || refreshing || saving) return;
		controlling = action;
		controlError = controlMessage = null;
		try {
			const result = await api.vpn.control(action);
			if (!active) return;
			snapshot = result;
			error = null;
			controlMessage = `${action === 'connect' ? 'Connect' : 'Disconnect'} requested. Status updates every 30 seconds.`;
		} catch (cause) {
			if (active)
				controlError = cause instanceof Error ? cause.message : 'Could not control the tunnel.';
		} finally {
			if (active) controlling = null;
		}
		if (active) await refresh();
	}
	onMount(() => {
		active = true;
		void api.vpn
			.connection()
			.then((value) => {
				if (active) {
					fillConnection(value);
					if (showConnection) void findTunnels();
				}
			})
			.catch((cause) => {
				if (active)
					connectionError =
						cause instanceof Error ? cause.message : 'Could not read connection settings.';
			});
		void refresh();
		const timer = setInterval(() => {
			if (document.visibilityState !== 'hidden') void refresh();
		}, 30000);
		const onVisibility = () => {
			if (document.visibilityState === 'visible') void refresh();
		};
		document.addEventListener('visibilitychange', onVisibility);
		return () => {
			active = false;
			historyGeneration++;
			clearInterval(timer);
			document.removeEventListener('visibilitychange', onVisibility);
			password = '';
			tunnels = null;
			tunnelKey = null;
		};
	});
</script>

<svelte:head><title>VPN | Eero Dashboard</title></svelte:head>

<div class="vpn-page">
	<header class="page-header">
		<div>
			<h1>VPN</h1>
			<p class="text-muted">NordVPN tunnel · OpenWrt {snapshot?.host ?? '192.168.4.10'}</p>
		</div>
		<div class="header-actions">
			<Button
				variant="primary"
				loading={controlling === 'connect'}
				disabled={!canControl || saving || refreshing || controlling !== null}
				onclick={() => void controlTunnel('connect')}>Connect</Button
			>
			<Button
				loading={controlling === 'disconnect'}
				disabled={!canControl || saving || refreshing || controlling !== null}
				onclick={() => void controlTunnel('disconnect')}>Disconnect</Button
			>
			<Button
				icon="settings"
				disabled={saving || controlling !== null}
				onclick={() => {
					showConnection = !showConnection;
					password = '';
					tunnels = null;
					tunnelKey = null;
					tunnelError = null;
					if (showConnection && connection) void findTunnels();
				}}>Connection</Button
			>
			<Button
				icon="refresh"
				loading={refreshing}
				disabled={saving || controlling !== null}
				onclick={() => void refresh()}>Refresh</Button
			>
		</div>
	</header>
	<p class="schedule">Recorded every 30 seconds, including when this page is closed.</p>
	{#if connection && !connection.control_target}<p class="text-muted">
			Set the tunnel type and name in Connection to enable Connect and Disconnect.
		</p>{/if}
	{#if controlMessage}<p role="status" class="text-muted">{controlMessage}</p>{/if}
	{#if controlError}<p class="error-banner" role="alert">{controlError}</p>{/if}
	{#if error}<p class="error-banner" role="alert">{error}</p>{/if}
	{#if showConnection}
		<Card title="OpenWrt SSH connection">
			<form onsubmit={saveConnection}>
				<div class="connection-fields">
					<label for="vpn-host"
						>OpenWrt address<input id="vpn-host" class="input" bind:value={host} required /></label
					>
					<label for="vpn-port"
						>SSH port<input
							id="vpn-port"
							class="input"
							type="number"
							min="1"
							max="65535"
							bind:value={port}
							required
						/></label
					>
					<label for="vpn-username"
						>Username<input
							id="vpn-username"
							class="input"
							bind:value={username}
							autocomplete="username"
							required
						/></label
					>
					<label for="vpn-password"
						>SSH password<input
							id="vpn-password"
							class="input"
							type="password"
							bind:value={password}
							autocomplete="current-password"
							placeholder={connection?.password_set
								? 'Leave blank to keep saved password'
								: 'Leave blank for passwordless SSH'}
						/></label
					>
					<label for="vpn-interface"
						>Tunnel interface<input
							id="vpn-interface"
							class="input"
							bind:value={tunnelInterface}
							required
						/></label
					>
					<label for="vpn-control-mode"
						>Tunnel type<select
							id="vpn-control-mode"
							class="input"
							bind:value={controlMode}
							onchange={changeTunnelType}
							disabled={discovering}
						>
							<option value="openvpn">OpenVPN service</option>
							<option value="network">OpenWrt network interface (WireGuard / OpenVPN)</option>
						</select></label
					>
					<label for="vpn-control-target"
						>Tunnel name
						{#if availableTunnels !== null}
							<select
								id="vpn-control-target"
								class="input"
								bind:value={controlTarget}
								aria-describedby="vpn-control-help"
								disabled={discovering}
							>
								<option value="">Monitoring only</option>
								{#if controlTarget && !matchingTunnels.some((tunnel) => tunnel.name === controlTarget)}
									<option value={controlTarget}>{controlTarget} (current selection)</option>
								{/if}
								{#each matchingTunnels as tunnel (tunnel.name)}
									<option value={tunnel.name}>{tunnel.name}</option>
								{/each}
							</select>
						{:else}
							<input
								id="vpn-control-target"
								class="input"
								bind:value={controlTarget}
								maxlength="64"
								pattern="[a-zA-Z0-9_][a-zA-Z0-9_.-]*"
								placeholder="Optional, e.g. nordvpn"
								aria-describedby="vpn-control-help"
								disabled={discovering}
							/>
						{/if}</label
					>
				</div>
				<Button
					loading={discovering}
					disabled={saving || controlling !== null}
					onclick={() => void findTunnels()}>Find tunnels</Button
				>
				{#if discovering}<p role="status" class="text-muted">Finding VPN tunnels…</p>{/if}
				{#if tunnelError}<p role="alert" class="error-banner">{tunnelError}</p>{/if}
				{#if availableTunnels !== null && matchingTunnels.length === 0}<p
						role="status"
						class="text-muted"
					>
						No tunnels found for this type.
					</p>{/if}
				<p id="vpn-control-help" class="text-muted">
					{#if availableTunnels !== null}
						Choose a tunnel from OpenWrt. A single available tunnel is selected automatically.
						Choose Monitoring only to leave tunnel controls disabled.
					{:else}
						Find tunnels using the SSH details above, or enter a tunnel name manually. Leave blank
						for monitoring only.
					{/if}
					Disconnecting affects clients using this tunnel.
				</p>
				<label class="enabled-toggle"
					><input type="checkbox" bind:checked={enabled} /> Enable VPN monitoring</label
				>
				<p class="text-muted">
					The dashboard tests SSH before saving. Passwords stay on the dashboard server.
				</p>
				{#if connectionError}<p role="alert" class="error-banner">{connectionError}</p>{/if}
				<Button
					type="submit"
					variant="primary"
					loading={saving}
					disabled={refreshing || discovering || controlling !== null}>Test and save</Button
				>
			</form>
		</Card>
	{/if}
	{#if loading}<p role="status">Loading VPN metrics…</p>{:else if snapshot}
		{#if snapshot.error}<p class="error-banner" role="alert">{snapshot.error}</p>{/if}
		{#if snapshot.stale}<p class="stale-banner" role="status">
				Current metrics are unavailable or stale. Waiting for the next reading.
			</p>{/if}
		{#if snapshot.history_error}<p class="stale-banner" role="alert">
				{snapshot.history_error}
			</p>{/if}
		<div class="metrics-grid" aria-live="polite">
			<Card title="Connection status">
				<StatusBadge status={snapshot.status} />
				<p class="metric-note">
					{snapshot.interface} · SSH {snapshot.stale
						? 'unconfirmed'
						: snapshot.reachable
							? 'reachable'
							: 'unavailable'}
				</p>
			</Card>
			<Card title="Connected clients"
				><div class="metric-value">
					{snapshot.stale ? '—' : (snapshot.connected_clients ?? '—')}
				</div>
				<p class="metric-note">Active connections through the tunnel</p></Card
			>
			<Card title="Tunnel download"
				><div class="metric-value">
					{speed(snapshot.stale ? null : snapshot.download_mbps)} <span>Mbps</span>
				</div>
				<p class="metric-note">Average traffic since the previous reading</p></Card
			>
			<Card title="Tunnel upload"
				><div class="metric-value">
					{speed(snapshot.stale ? null : snapshot.upload_mbps)} <span>Mbps</span>
				</div>
				<p class="metric-note">Average traffic since the previous reading</p></Card
			>
		</div>
		<p class="reading-time">
			{snapshot.collected_at
				? `Last checked ${new Date(snapshot.collected_at).toLocaleString()}`
				: 'Waiting for the first 30-second collection.'}{snapshot.download_mbps == null &&
			snapshot.reachable &&
			!snapshot.stale
				? ' · Throughput appears after two readings.'
				: ''}
		</p>
		<Card title="Client IP addresses">
			{#if snapshot.stale || snapshot.connected_client_ips == null}
				<p class="text-muted">Client IP addresses are unavailable. Waiting for the next reading.</p>
			{:else if snapshot.connected_client_ips.length === 0}
				<p class="text-muted">No active clients through the tunnel.</p>
			{:else}
				<ul class="client-ips" aria-label="Connected client IP addresses">
					{#each snapshot.connected_client_ips as ip (ip)}
						<li><code>{ip}</code></li>
					{/each}
				</ul>
			{/if}
			<p class="metric-note">Active clients using the tunnel, sorted by IP address.</p>
		</Card>
		{#if snapshot.warnings.length}<details>
				<summary>About these readings</summary>{#each snapshot.warnings as warning}<p
						class="text-muted"
					>
						{warning}
					</p>{/each}
			</details>{/if}
	{/if}
	<div class="history-header">
		<h2>VPN history</h2>
		<TimeRangeSelector
			options={ranges}
			value={timeRange}
			onChange={(range) => {
				timeRange = range;
				history = null;
				void loadHistory();
			}}
			label="VPN history time range"
		/>
	</div>
	{#if historyError}<p class="error-banner" role="alert">{historyError}</p>{/if}
	<Card title="Tunnel throughput"
		><TimeSeriesChart
			title="Tunnel throughput"
			datasets={throughput}
			yAxisLabel="Mbps"
			error={historyError}
		/></Card
	>
	<div class="history-grid">
		<Card title="Connected clients"
			><TimeSeriesChart
				title="Connected clients"
				datasets={clientHistory}
				yAxisLabel="Clients"
				error={historyError}
			/></Card
		>
		<Card title="Connection status"
			><TimeSeriesChart
				title="Connection status"
				datasets={statusHistory}
				yAxisLabel="Status"
				error={historyError}
			/>
			<p class="metric-note">
				1 = connected · 0 = disconnected · −1 = unknown or unreachable
			</p></Card
		>
	</div>
	<p class="text-muted">
		Throughput measures tunnel traffic, averaged over each collection interval. Client counts
		include unique source IPs with replied connections NATed through the tunnel; idle clients and
		traffic without NAT are excluded.
	</p>
</div>

<style>
	.vpn-page {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}
	.page-header,
	.header-actions,
	.history-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: var(--space-3);
		flex-wrap: wrap;
	}
	h1,
	h2,
	.page-header p {
		margin: 0;
	}
	h2 {
		font-size: 1.125rem;
	}
	.schedule,
	.reading-time,
	.metric-note {
		color: var(--color-text-muted);
		font-size: 0.8125rem;
		margin: 0;
	}
	.metrics-grid {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: var(--space-4);
	}
	.history-grid,
	.connection-fields {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-4);
	}
	.metric-value {
		font-size: 1.75rem;
		font-weight: 600;
		margin-bottom: var(--space-2);
		font-variant-numeric: tabular-nums;
	}
	.metric-value span {
		font-size: 0.875rem;
		color: var(--color-text-muted);
		font-weight: 400;
	}
	.metric-note {
		margin-top: var(--space-2);
	}
	.client-ips {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
		gap: var(--space-2) var(--space-4);
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.client-ips li {
		padding: var(--space-2) 0;
		overflow-wrap: anywhere;
	}
	form,
	.connection-fields label {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}
	form {
		gap: var(--space-4);
	}
	.connection-fields label {
		font-size: 0.875rem;
	}
	.enabled-toggle {
		display: flex;
		align-items: center;
		gap: var(--space-2);
	}
	.error-banner,
	.stale-banner {
		padding: var(--space-3);
		border-radius: var(--radius-md);
		margin: 0;
	}
	.error-banner {
		background: var(--color-danger-bg);
		color: var(--color-danger);
	}
	.stale-banner {
		background: var(--color-warning-bg);
		color: var(--color-warning);
	}
	summary {
		cursor: pointer;
		font-size: 0.875rem;
		color: var(--color-text-secondary);
	}
	@media (max-width: 1100px) {
		.metrics-grid {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
	@media (max-width: 768px) {
		.metrics-grid,
		.history-grid,
		.connection-fields {
			grid-template-columns: 1fr;
		}
	}
</style>
