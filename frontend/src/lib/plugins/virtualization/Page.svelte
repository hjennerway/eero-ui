<script lang="ts">
	import { virtualizationApi } from './api';
	import type {
		VirtualizationConnection,
		VirtualizationConnectionUpdate,
		VirtualizationSnapshot
	} from './types';
	import { selectedNetworkId } from '$stores';
	import Button from '$components/common/Button.svelte';
	import ConnectionForm from './ConnectionForm.svelte';
	import ProxmoxNode from './ProxmoxNode.svelte';
	import VirtualizationMap from './VirtualizationMap.svelte';
	import { startVisibleRefresh } from './virtualizationPolling.js';

	let snapshot = $state<VirtualizationSnapshot | null>(null);
	let connection = $state<VirtualizationConnection | null>(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let saving = $state(false);
	let error = $state<string | null>(null);
	let connectionError = $state<string | null>(null);
	let showConnection = $state(false);
	let query = $state('');
	let showStopped = $state(true);
	let listView = $state(false);
	let generation = 0;

	async function refresh(networkId: string, force: boolean, current = generation) {
		if (current !== generation || refreshing || !snapshot?.configured || saving) return;
		refreshing = true;
		error = null;
		try {
			const result = await virtualizationApi.refresh(networkId, force);
			if (current === generation) snapshot = result;
		} catch (cause) {
			if (current === generation)
				error = cause instanceof Error ? cause.message : 'Could not refresh inventory.';
		} finally {
			if (current === generation) refreshing = false;
		}
	}

	$effect(() => {
		const networkId = $selectedNetworkId;
		const current = ++generation;
		let stopPolling = () => {};
		let active = true;
		snapshot = null;
		connection = null;
		error = connectionError = null;
		refreshing = saving = false;
		showConnection = false;
		loading = !!networkId;
		if (networkId) {
			void Promise.all([virtualizationApi.get(networkId), virtualizationApi.connection(networkId)])
				.then(([inventory, settings]) => {
					if (!active) return;
					snapshot = inventory;
					connection = settings;
					showConnection = !settings.configured;
					listView = window.matchMedia('(max-width: 768px)').matches;
					stopPolling = startVisibleRefresh(() => {
						void refresh(networkId, false, current);
					});
				})
				.catch((cause) => {
					if (active)
						error = cause instanceof Error ? cause.message : 'Could not load virtualization.';
				})
				.finally(() => {
					if (active) loading = false;
				});
		}
		return () => {
			active = false;
			generation++;
			stopPolling();
		};
	});

	async function saveConnection(update: VirtualizationConnectionUpdate) {
		const networkId = $selectedNetworkId;
		if (!networkId || saving) return;
		const current = generation;
		saving = true;
		connectionError = null;
		try {
			const result = await virtualizationApi.saveConnection(networkId, update);
			if (current !== generation) return;
			connection = result;
			const inventory = await virtualizationApi.get(networkId);
			if (current !== generation) return;
			snapshot = inventory;
			showConnection = false;
			saving = false;
			await refresh(networkId, true, current);
		} catch (cause) {
			if (current === generation)
				connectionError = cause instanceof Error ? cause.message : 'Could not save the connection.';
		} finally {
			if (current === generation) saving = false;
		}
	}

	const filteredNodes = $derived.by(() => {
		const search = query.trim().toLowerCase();
		return (snapshot?.nodes ?? [])
			.map((node) => ({
				...node,
				guests: node.guests.filter(
					(guest) =>
						(showStopped || guest.status === 'running') &&
						(!search ||
							node.name.toLowerCase().includes(search) ||
							[
								guest.name,
								guest.os_name ?? '',
								String(guest.vmid),
								...guest.ips,
								...guest.macs,
								...guest.docker.containers.map((container) => container.name)
							].some((value) => value.toLowerCase().includes(search)))
				)
			}))
			.filter((node) => !search || node.name.toLowerCase().includes(search) || node.guests.length);
	});
</script>

<svelte:head><title>Virtualization | Eero Dashboard</title></svelte:head>

<div class="virtualization-page">
	<header class="page-header">
		<div>
			<h1>Virtualization</h1>
			<p class="text-muted">Proxmox nodes, their guests, and Docker containers</p>
		</div>
		<div class="header-actions">
			<Button
				icon="settings"
				onclick={() => (showConnection = !showConnection)}
				disabled={!connection}>Connection</Button
			>
			<Button
				icon="refresh"
				loading={refreshing}
				disabled={!snapshot?.configured || saving}
				onclick={() => {
					if ($selectedNetworkId) void refresh($selectedNetworkId, true);
				}}>Refresh</Button
			>
		</div>
	</header>
	<p class="refresh-schedule">
		Background refresh every 10 minutes · Every 60 seconds while this page is visible
	</p>
	{#if error}<div role="alert" class="error-banner">{error}</div>{/if}
	{#if !$selectedNetworkId}
		<p class="empty-message">Select a network to view its virtualization inventory.</p>
	{:else if loading}
		<p class="empty-message" role="status">Loading virtualization…</p>
	{:else}
		{#if connection && showConnection}
			<ConnectionForm {connection} {saving} error={connectionError} onsave={saveConnection} />
		{/if}
		{#if snapshot?.configured}
			<div class="inventory-toolbar">
				<label class="search-label" for="virtualization-search"
					>Search<input
						id="virtualization-search"
						class="input"
						type="search"
						bind:value={query}
						placeholder="Node, guest, OS, or container"
					/></label
				>
				<label class="stopped-toggle"
					><input type="checkbox" bind:checked={showStopped} /> Include stopped</label
				>
				<Button icon={listView ? 'topology' : 'server'} onclick={() => (listView = !listView)}
					>{listView ? 'Diagram view' : 'List view'}</Button
				>
			</div>
			<div class="inventory-status" aria-live="polite">
				<span
					>{refreshing
						? 'Refreshing inventory…'
						: snapshot.collected_at
							? `Last updated ${new Date(snapshot.collected_at).toLocaleString()}`
							: 'Waiting for the first inventory'}</span
				>
				{#if snapshot.stale}<span class="stale-label">Last known inventory</span>{/if}
				{#if snapshot.error}<span class="inventory-error">{snapshot.error}</span>{/if}
			</div>
			{#if filteredNodes.length}
				{#if listView}
					<div class="node-list">
						{#each filteredNodes as node (node.id)}<ProxmoxNode
								data={{ node, showStopped }}
							/>{/each}
					</div>
				{:else}
					<VirtualizationMap nodes={filteredNodes} {showStopped} />
					<p class="diagram-tip">
						Drag a node heading to rearrange it. Drag the canvas to pan; use the controls to zoom or
						fit the diagram.
					</p>
				{/if}
			{:else if snapshot.collected_at}
				<p class="empty-message">
					{query
						? 'No matching nodes or guests.'
						: 'No nodes were returned. Check the monitoring token permissions.'}
				</p>
			{/if}
		{/if}
	{/if}
</div>

<style>
	.virtualization-page {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}
	.page-header,
	.header-actions,
	.inventory-toolbar,
	.inventory-status {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-3);
		align-items: center;
	}
	.page-header {
		justify-content: space-between;
		align-items: flex-start;
	}
	h1 {
		margin-bottom: var(--space-1);
	}
	.page-header p {
		margin: 0;
	}
	.refresh-schedule,
	.diagram-tip {
		margin: 0;
		font-size: 0.8rem;
		color: var(--color-text-secondary);
	}
	.inventory-toolbar {
		align-items: end;
	}
	.search-label {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: 0.8rem;
		flex: 1;
		min-width: 220px;
	}
	.stopped-toggle {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding-bottom: var(--space-2);
		font-size: 0.85rem;
	}
	.stopped-toggle input {
		width: auto;
	}
	.inventory-status {
		font-size: 0.8rem;
		color: var(--color-text-secondary);
	}
	.stale-label {
		color: var(--color-warning);
	}
	.error-banner,
	.inventory-error {
		color: var(--color-danger);
	}
	.error-banner {
		border: 1px solid var(--color-danger);
		padding: var(--space-3);
		border-radius: var(--radius-md);
	}
	.node-list {
		display: grid;
		gap: var(--space-4);
	}
	.empty-message {
		padding: var(--space-5) 0;
		color: var(--color-text-secondary);
	}
</style>
