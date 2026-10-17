<script lang="ts">
	import { nasApi } from '#lib/plugins/nas/api.js';
	import { onMount } from 'svelte';
	import type { NASHealth, NASSnapshot, NASStorage } from '#lib/plugins/nas/types.js';
	import { selectedNetwork } from '$stores';
	import { cachedNAS, cacheNAS, NAS_REFRESH_MS } from '#lib/plugins/nas/cache.js';
	import { formatBytes } from '#lib/utils/format-bytes.js';
	import { formatPowerOnTime } from '#lib/plugins/nas/format-power-on-time.js';
	import Button from '$components/common/Button.svelte';
	import Card from '$components/common/Card.svelte';
	import Icon from '$components/common/Icon.svelte';
	import QTSConnectionModal from './QTSConnectionModal.svelte';

	let snapshots: NASSnapshot[] = $state([]);
	let loading = $state(false);
	let error = $state('');
	let mounted = $state(false);
	let connectionNAS: { nas: NASSnapshot; networkId: string } | null = $state(null);
	let revision = 0;
	let busyNetwork: string | null = null;
	let pendingRefresh: ReturnType<typeof setTimeout> | undefined;
	const labels: Record<NASHealth, string> = {
		good: 'Healthy',
		warning: 'Warning',
		critical: 'Critical',
		unknown: 'Unknown'
	};

	async function load(networkId: string, switching = false, refresh = false) {
		if (!switching && !refresh && busyNetwork === networkId) return;
		clearTimeout(pendingRefresh);
		const requestRevision = ++revision;
		busyNetwork = networkId;
		loading = true;
		error = '';
		if (switching) snapshots = cachedNAS(networkId);
		try {
			const result = await nasApi.list(networkId, refresh);
			if (mounted && requestRevision === revision) {
				snapshots = result;
				cacheNAS(networkId, result);
				if (result.some((snapshot) => snapshot.stale)) {
					pendingRefresh = setTimeout(() => {
						if (mounted && requestRevision === revision) void load(networkId);
					}, 1000);
				}
			}
		} catch (cause) {
			if (mounted && requestRevision === revision) {
				snapshots = snapshots.map((snapshot) => ({ ...snapshot, stale: true }));
				cacheNAS(networkId, snapshots);
				error = cause instanceof Error ? cause.message : 'Could not read NAS data.';
			}
		} finally {
			if (mounted && requestRevision === revision) {
				loading = false;
				busyNetwork = null;
			}
		}
	}

	$effect(() => {
		const networkId = $selectedNetwork?.id;
		if (!mounted) return;
		if (networkId) void load(networkId, true);
		else {
			revision++;
			busyNetwork = null;
			snapshots = [];
			loading = false;
			error = '';
		}
	});

	$effect(() => {
		if (connectionNAS && connectionNAS.networkId !== $selectedNetwork?.id) connectionNAS = null;
	});

	onMount(() => {
		mounted = true;
		const timer = setInterval(() => {
			if ($selectedNetwork?.id) void load($selectedNetwork.id);
		}, NAS_REFRESH_MS);
		return () => {
			mounted = false;
			revision++;
			clearInterval(timer);
			clearTimeout(pendingRefresh);
		};
	});

	function observedAt(value: string | null): string {
		return value ? new Date(value).toLocaleString() : 'No current reading';
	}

	function storageGroups(nas: NASSnapshot): {
		title: string;
		items: NASStorage[];
		allocated: boolean;
	}[] {
		return [
			{ title: 'Volumes', items: nas.volumes, allocated: false },
			{ title: 'Storage pools', items: nas.pools, allocated: true },
			{ title: 'RAID groups', items: nas.raids, allocated: true }
		];
	}
</script>

<svelte:head><title>NAS | Eero Dashboard</title></svelte:head>

<div class="nas-page">
	<header class="page-header">
		<div>
			<h1>NAS</h1>
			<p class="text-muted">Disk health, storage status and capacity on your network.</p>
		</div>
		<Button
			variant="secondary"
			disabled={loading || !$selectedNetwork}
			onclick={() => {
				if ($selectedNetwork?.id) void load($selectedNetwork.id, false, true);
			}}><Icon name="refresh" size={16} /> Refresh</Button
		>
	</header>

	{#if error}<p class="notice error" role="alert">{error}</p>{/if}
	{#if loading}<p class="text-muted" role="status">Reading NAS status…</p>{/if}
	{#if !$selectedNetwork}
		<Card title="Select a network"
			><p class="text-muted">Choose a network to see its NAS devices.</p></Card
		>
	{:else if !loading && !error && snapshots.length === 0}
		<Card title="Connect your QNAP">
			<p>No NAS is configured for this network.</p>
			<ol>
				<li>
					In QTS, open <strong>Control Panel → Network &amp; File Services → SNMP</strong> and enable
					SNMP v1/v2 with a read-only community.
				</li>
				<li>
					Add your NAS address and community to the dashboard server’s private NAS configuration,
					using <code>docs/nas.example.json</code> as the template.
				</li>
				<li>Set <code>EERO_DASHBOARD_NAS_FILE</code> to that file and restart the dashboard.</li>
			</ol>
			<p class="text-muted">
				Use network ID <code>{$selectedNetwork.id}</code>. The NAS must be reachable from the
				dashboard server on UDP 161.
			</p>
		</Card>
	{/if}

	{#each snapshots as nas (nas.id)}
		<section class="nas-device" aria-label={nas.name}>
			<div class="device-header">
				<div>
					<h2>{nas.name}</h2>
					<p class="text-muted">{nas.host}{nas.model ? ` · ${nas.model}` : ''}</p>
				</div>
				<div class="observation">
					<strong class:unreachable={nas.status === 'unreachable'}
						>{nas.stale
							? 'Cached reading — awaiting refresh'
							: nas.status === 'online'
								? 'Responding'
								: 'Unreachable'}</strong
					><span class="text-muted">{observedAt(nas.observed_at)}</span>
					<Button
						variant="secondary"
						onclick={() => {
							if ($selectedNetwork?.id) connectionNAS = { nas, networkId: $selectedNetwork.id };
						}}>QTS connection</Button
					>
				</div>
			</div>
			{#each nas.warnings as warning}<p class="notice" role="status">{warning}</p>{/each}
			{#if nas.status === 'online'}
				<Card title="Disk health">
					{#if nas.disks.length === 0}
						<p class="text-muted">Disk health is unavailable.</p>
					{:else}
						<div class="health-summary" aria-label="Disk health summary">
							{#each ['good', 'warning', 'critical', 'unknown'] as category}
								<span class="badge {category}"
									>{nas.disks.filter((disk) => disk.health === category).length}
									{labels[category as NASHealth]}</span
								>
							{/each}
						</div>
						<div class="disk-grid">
							{#each nas.disks as disk (disk.id)}
								<article class="disk">
									<div class="disk-heading">
										<h3>{disk.name}</h3>
										<span class="badge {disk.health}">{labels[disk.health]}</span>
									</div>
									<p class="disk-model text-muted">{disk.model ?? 'Model unavailable'}</p>
									<dl>
										<div>
											<dt>Status</dt>
											<dd>{disk.status ?? 'Unknown'}</dd>
										</div>
										<div>
											<dt>SMART health</dt>
											<dd>{disk.smart_status ?? 'Unknown'}</dd>
										</div>
										<div>
											<dt>Temperature</dt>
											<dd>{disk.temperature_c === null ? '—' : `${disk.temperature_c} °C`}</dd>
										</div>
										<div>
											<dt>Capacity</dt>
											<dd>{formatBytes(disk.capacity_bytes)}</dd>
										</div>
										<div>
											<dt>Power-on time</dt>
											<dd
												title={disk.power_on_hours == null
													? undefined
													: `${disk.power_on_hours.toLocaleString()} total hours`}
											>
												{disk.power_on_hours === undefined && disk.power_on_days != null
													? `${disk.power_on_days.toLocaleString()} days (hours unavailable)`
													: formatPowerOnTime(disk.power_on_hours)}
											</dd>
										</div>
										<div>
											<dt>SSD life remaining</dt>
											<dd>
												{disk.ssd_life_remaining_percent == null
													? 'Unavailable'
													: `${disk.ssd_life_remaining_percent}%`}
											</dd>
										</div>
									</dl>
								</article>
							{/each}
						</div>
					{/if}
					<p class="smart-help text-muted">
						Power-on time is accumulated drive use, with approximate 365-day years and 30-day
						months. Hover over a reading for total hours.
					</p>
					<p class="smart-help text-muted">
						SMART health is the summary reported by QNAP. Detailed attributes and test results are
						available in QTS under Storage &amp; Snapshots → Disks/VJBOD → Health.
					</p>
				</Card>
				{#each storageGroups(nas) as group}
					{#if group.items.length}
						<Card title={group.title}>
							{#if group.allocated}<p class="text-muted">
									Allocation includes space assigned to volumes and storage pools. See Volumes for
									file usage.
								</p>{/if}
							<div class="storage-grid">
								{#each group.items as storage (storage.id)}
									<article class="storage">
										<div class="disk-heading">
											<h3>{storage.name}</h3>
											<span class="badge {storage.health}">{storage.status ?? 'Unknown'}</span>
										</div>
										{#if storage.raid_level}<p class="text-muted">{storage.raid_level}</p>{/if}
										{#if storage.used_percent !== null}<progress
												max="100"
												value={storage.used_percent}
												aria-label={`${storage.name} ${group.allocated ? 'allocated' : 'used'} capacity`}
											></progress>{/if}
										<p class="usage">
											{storage.used_percent === null
												? 'Usage unavailable'
												: `${storage.used_percent.toFixed(1)}% ${group.allocated ? 'allocated' : 'used'}`}
										</p>
										<dl>
											<div>
												<dt>{group.allocated ? 'Allocated' : 'Used'}</dt>
												<dd>{formatBytes(storage.used_bytes)}</dd>
											</div>
											<div>
												<dt>{group.allocated ? 'Unallocated' : 'Free'}</dt>
												<dd>{formatBytes(storage.free_bytes)}</dd>
											</div>
											<div>
												<dt>Total</dt>
												<dd>{formatBytes(storage.capacity_bytes)}</dd>
											</div>
										</dl>
									</article>
								{/each}
							</div>
						</Card>
					{/if}
				{/each}
			{/if}
		</section>
	{/each}
	<p class="text-muted refresh-help">
		Readings are cached and refreshed every 5 minutes in the background. Disk age and SSD life
		require the optional QTS connection. Missing readings appear as unavailable.
	</p>
</div>

{#if connectionNAS}
	{#key `${connectionNAS.networkId}/${connectionNAS.nas.id}`}
		<QTSConnectionModal
			nasId={connectionNAS.nas.id}
			networkId={connectionNAS.networkId}
			name={connectionNAS.nas.name}
			host={connectionNAS.nas.host}
			onClose={() => {
				connectionNAS = null;
			}}
			onSaved={() => {
				const networkId = connectionNAS?.networkId;
				connectionNAS = null;
				if (networkId && networkId === $selectedNetwork?.id) void load(networkId, false, true);
			}}
		/>
	{/key}
{/if}

<style>
	.nas-page,
	.nas-device {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}
	.page-header,
	.device-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-4);
	}
	h1,
	h2,
	h3,
	p {
		margin: 0;
	}
	.page-header p,
	.device-header p {
		margin-top: var(--space-2);
	}
	.observation {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		text-align: right;
		font-size: var(--text-sm);
	}
	.unreachable {
		color: var(--color-danger);
	}
	.notice {
		padding: var(--space-3) var(--space-4);
		border: 1px solid var(--color-warning);
		border-radius: var(--radius-md);
	}
	.notice.error {
		border-color: var(--color-danger);
	}
	.health-summary {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		margin-bottom: var(--space-4);
	}
	.badge {
		display: inline-flex;
		padding: var(--space-1) var(--space-2);
		border-radius: var(--radius-sm);
		font-size: var(--text-xs);
		background: var(--color-bg-tertiary);
		color: var(--color-text-secondary);
	}
	.badge.good {
		color: var(--color-success);
	}
	.badge.warning {
		color: var(--color-warning);
	}
	.badge.critical {
		color: var(--color-danger);
	}
	.disk-grid,
	.storage-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
		gap: var(--space-4);
	}
	.disk,
	.storage {
		padding: var(--space-4);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
	}
	.disk-heading {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
	}
	h3 {
		font-size: var(--text-base);
	}
	.disk-model {
		margin-top: var(--space-2);
		overflow-wrap: anywhere;
		font-size: var(--text-sm);
	}
	dl {
		margin: var(--space-4) 0 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		font-size: var(--text-sm);
	}
	dl div {
		display: flex;
		justify-content: space-between;
		gap: var(--space-3);
	}
	dt {
		color: var(--color-text-secondary);
	}
	dd {
		margin: 0;
		text-align: right;
	}
	progress {
		width: 100%;
		height: 10px;
		margin-top: var(--space-4);
		accent-color: var(--color-accent);
	}
	.usage {
		margin-top: var(--space-2);
		font-size: var(--text-sm);
	}
	.smart-help {
		margin-top: var(--space-4);
		font-size: var(--text-sm);
		line-height: 1.6;
	}
	.refresh-help {
		font-size: var(--text-sm);
	}
	ol {
		padding-left: var(--space-5);
		line-height: 1.8;
	}
	code {
		overflow-wrap: anywhere;
	}
	@media (max-width: 600px) {
		.page-header,
		.device-header {
			align-items: flex-start;
		}
		.device-header {
			flex-direction: column;
		}
		.observation {
			text-align: left;
		}
	}
</style>
