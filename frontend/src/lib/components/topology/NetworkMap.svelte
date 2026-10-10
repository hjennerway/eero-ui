<!--
  NetworkMap Component
  
  Main topology visualization using @xyflow/svelte.
  Displays eero mesh network with connected devices.
-->
<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		SvelteFlow,
		Controls,
		Background,
		MiniMap,
		BackgroundVariant,
		type NodeTypes,
		type EdgeTypes
	} from '@xyflow/svelte';
	import '@xyflow/svelte/dist/style.css';

	import { goto } from '$app/navigation';
	import {
		topologyStore,
		selectedNode,
		layoutOptionsStore,
		filteredTopology,
		type TopologyNodeData,
		type LayoutType,
		type NodeDetailLevel
	} from '#lib/stores/topology.js';

	import EeroNode from './nodes/EeroNode.svelte';
	import DeviceNode from './nodes/DeviceNode.svelte';
	import GatewayNode from './nodes/GatewayNode.svelte';
	import ClientGroupNode from './nodes/ClientGroupNode.svelte';
	import SwitchNode from './nodes/SwitchNode.svelte';
	import ClientEdge from './ClientEdge.svelte';
	import Icon from '$components/common/Icon.svelte';
	import Button from '$components/common/Button.svelte';
	import type { IconName } from '#lib/icons/paths.js';

	interface Props {
		// Props
		readonly?: boolean;
		onNodeClick?: ((nodeId: string) => void) | undefined;
	}

	let { readonly = false, onNodeClick = undefined }: Props = $props();
	let showOverview = $state(false);

	// Layout options for dropdown
	const layoutOptions: { value: LayoutType; label: string; icon: IconName }[] = [
		{ value: 'hierarchy', label: 'Hierarchy (Top-Down)', icon: 'bar-chart' },
		{ value: 'horizontal', label: 'Horizontal (Left-Right)', icon: 'ruler' },
		{ value: 'radial', label: 'Radial (Circular)', icon: 'target' },
		{ value: 'force', label: 'Force-Directed (Organic)', icon: 'globe' }
	];

	const detailOptions: { value: NodeDetailLevel; label: string }[] = [
		{ value: 'minimal', label: 'Minimal (Icon, Name, IP)' },
		{ value: 'standard', label: 'Standard (+Model, Metrics)' },
		{ value: 'detailed', label: 'Detailed (All Info)' }
	];

	// Custom node types registration
	const nodeTypes: NodeTypes = {
		gateway: GatewayNode,
		eero: EeroNode,
		device: DeviceNode,
		clientGroup: ClientGroupNode,
		switch: SwitchNode
	};
	const edgeTypes: EdgeTypes = { client: ClientEdge };

	// Local reactive state bound to store
	let nodes = $derived($filteredTopology.nodes);
	let edges = $derived($filteredTopology.edges);

	// Load topology on mount
	onMount(() => {
		topologyStore.loadTopology();
	});

	onDestroy(() => {
		topologyStore.clear();
	});

	// Handle node click - xyflow uses { node, event } directly
	function handleNodeClick({ node }: { node: { id: string; data: TopologyNodeData } }) {
		if (node && node.data.type !== 'clientGroup') {
			topologyStore.selectNode(node.id);
			onNodeClick?.(node.id);
		}
	}

	// Handle pane click (deselect)
	function handlePaneClick() {
		topologyStore.selectNode(null);
	}

	// Keyboard shortcuts
	function handleKeyDown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			topologyStore.selectNode(null);
		}
	}

	// Refresh topology
	function refresh() {
		topologyStore.loadTopology();
	}

	// Handle layout change
	function handleLayoutChange(event: Event) {
		const target = event.target as HTMLSelectElement;
		layoutOptionsStore.update((opts) => ({
			...opts,
			layoutType: target.value as LayoutType
		}));
	}

	// Handle detail level change
	function handleDetailChange(event: Event) {
		const target = event.target as HTMLSelectElement;
		layoutOptionsStore.update((opts) => ({
			...opts,
			detailLevel: target.value as NodeDetailLevel
		}));
	}
</script>

<svelte:window onkeydown={handleKeyDown} />

<div class="topology-container">
	<!-- Controls bar -->
	<div class="map-controls">
		<div class="control-group">
			<label class="control-label" for="topology-layout-select">Layout:</label>
			<select
				id="topology-layout-select"
				class="control-select"
				value={$layoutOptionsStore.layoutType}
				onchange={handleLayoutChange}
			>
				{#each layoutOptions as opt}
					<!-- Native <option> cannot render an <Icon>; label text stands alone here. -->
					<option value={opt.value}>{opt.label}</option>
				{/each}
			</select>
		</div>

		<div class="control-group">
			<label class="control-label" for="topology-detail-select">Detail:</label>
			<select
				id="topology-detail-select"
				class="control-select"
				value={$layoutOptionsStore.detailLevel}
				onchange={handleDetailChange}
			>
				{#each detailOptions as opt}
					<option value={opt.value}>{opt.label}</option>
				{/each}
			</select>
		</div>

		<div class="control-divider"></div>

		<label class="control-option">
			<input type="checkbox" bind:checked={$layoutOptionsStore.showDevices} />
			<span>Devices</span>
		</label>
		<label class="control-option">
			<input type="checkbox" bind:checked={$layoutOptionsStore.showOfflineDevices} />
			<span>Offline</span>
		</label>
		<label class="control-option">
			<input type="checkbox" bind:checked={showOverview} />
			<span>Overview</span>
		</label>

		<Button
			size="sm"
			class="topology-refresh-btn"
			icon="refresh"
			label="Refresh topology"
			onclick={refresh}
			loading={$filteredTopology.loading}
		/>
	</div>

	{#if $filteredTopology.switchWarning}
		<p class="switch-warning" role="status">{$filteredTopology.switchWarning}</p>
	{/if}
	<div class="map-canvas">
		{#if $filteredTopology.loading && nodes.length === 0}
			<div class="loading-overlay">
				<span class="loading-spinner"></span>
				<span>Loading network topology...</span>
			</div>
		{:else if $filteredTopology.error}
			<div class="error-overlay">
				<span class="error-icon"><Icon name="alert-triangle" size={36} /></span>
				<span>{$filteredTopology.error}</span>
				<Button variant="primary" onclick={refresh}>Retry</Button>
			</div>
		{:else if nodes.length === 0}
			<div class="empty-overlay">
				<span class="empty-icon"><Icon name="router" size={36} /></span>
				<span>No topology data available</span>
				<Button variant="primary" onclick={refresh}>Refresh</Button>
			</div>
		{:else}
			{#key `${$layoutOptionsStore.layoutType}-${$layoutOptionsStore.detailLevel}-${$layoutOptionsStore.showDevices}-${$layoutOptionsStore.showOfflineDevices}`}
				<SvelteFlow
					{nodes}
					{edges}
					{nodeTypes}
					{edgeTypes}
					fitView
					fitViewOptions={{ padding: 0.12 }}
					minZoom={0.05}
					maxZoom={2}
					nodesDraggable={!readonly}
					nodesConnectable={false}
					elementsSelectable={true}
					edgesFocusable={false}
					panOnScroll={true}
					zoomOnScroll={true}
					onnodeclick={handleNodeClick}
					onpaneclick={handlePaneClick}
				>
					<Background variant={BackgroundVariant.Dots} gap={20} size={1} />

					<Controls showZoom={true} showFitView={true} showLock={!readonly} />

					{#if showOverview}
						<MiniMap
							nodeColor={(node) => {
								if (node.type === 'clientGroup') return 'transparent';
								if (node.type === 'gateway') return 'var(--color-accent)';
								if (node.type === 'eero') {
									return node.data.status === 'online'
										? 'var(--color-success)'
										: 'var(--color-danger)';
								}
								return node.data.connectionType === 'wired'
									? 'var(--color-success)'
									: 'var(--color-accent)';
							}}
							maskColor="var(--color-overlay)"
						/>
					{/if}
				</SvelteFlow>
			{/key}
		{/if}
	</div>
	<!-- Legend for client groups and eero links -->
	<div class="edge-legend">
		<div class="legend-item">
			<span class="legend-swatch wired"></span>
			<span>Wired clients</span>
		</div>
		<div class="legend-item">
			<span class="legend-swatch wireless"></span>
			<span>Wireless clients</span>
		</div>
		<div class="legend-item">
			<span class="legend-line mesh"></span>
			<span>Eero links</span>
		</div>
		{#if nodes.some((node) => node.type === 'switch')}
			<div class="legend-item">
				<span class="legend-line switch-link"></span>
				<span>Switch links (LLDP)</span>
			</div>
			<div class="legend-item">
				<span class="legend-line inferred"></span>
				<span>Inferred wired path</span>
			</div>
		{/if}
		{#if nodes.some((node) => node.data.evidence === 'manual')}
			<div class="legend-item">
				<span class="legend-line manual"></span>
				<span>User-supplied wiring</span>
			</div>
		{/if}
	</div>

	<!-- Selected node details panel -->
	{#if $selectedNode}
		<div class="details-panel" class:switch-details={$selectedNode.data.type === 'switch'}>
			<div class="details-header">
				<h3>{$selectedNode.data.label}</h3>
				<button
					class="close-btn"
					onclick={() => topologyStore.selectNode(null)}
					aria-label="Close details"
				>
					<Icon name="close" size={16} />
				</button>
			</div>

			<div class="details-content">
				<dl>
					<dt>Type</dt>
					<dd class="capitalize">
						{$selectedNode.data.unmanaged ? 'Unmanaged switch' : $selectedNode.data.type}
					</dd>

					<dt>Status</dt>
					<dd>
						<span
							class="status-badge"
							class:online={$selectedNode.data.status === 'online'}
							class:unknown={$selectedNode.data.status === 'unknown'}
						>
							{$selectedNode.data.status}
						</span>
					</dd>

					{#if $selectedNode.data.model}
						<dt>Model</dt>
						<dd>{$selectedNode.data.model}</dd>
					{/if}

					{#if $selectedNode.data.ipAddress || $selectedNode.data.ip}
						<dt>IP Address</dt>
						<dd class="mono">{$selectedNode.data.ipAddress || $selectedNode.data.ip}</dd>
					{/if}

					{#if $selectedNode.data.mac}
						<dt>MAC</dt>
						<dd class="mono">{$selectedNode.data.mac}</dd>
					{/if}
					{#if $selectedNode.data.portName}
						<dt>Switch port</dt>
						<dd>{$selectedNode.data.portName}</dd>
					{/if}
					{#if $selectedNode.data.evidence}
						<dt>Connection evidence</dt>
						<dd>
							{$selectedNode.data.evidence === 'manual'
								? 'Wiring supplied by the user'
								: $selectedNode.data.evidence === 'lldp'
									? 'Reported by LLDP neighbour discovery'
									: 'Inferred from learned MAC address; may be behind another device'}
						</dd>
					{/if}
					{#if $selectedNode.data.observedAt}
						<dt>Last read</dt>
						<dd>{new Date($selectedNode.data.observedAt).toLocaleString()}</dd>
					{/if}

					{#if $selectedNode.data.connectionType}
						<dt>Connection</dt>
						<dd class="capitalize">{$selectedNode.data.connectionType}</dd>
					{/if}

					{#if $selectedNode.data.eeroLabel}
						<dt>
							{$selectedNode.data.evidence === 'mac'
								? 'Path via'
								: $selectedNode.data.status === 'offline'
									? 'Last connected to'
									: 'Connected to'}
						</dt>
						<dd>{$selectedNode.data.eeroLabel}</dd>
					{/if}

					{#if $selectedNode.data.meshQuality !== undefined}
						<dt>Mesh Quality</dt>
						<dd>{$selectedNode.data.meshQuality}/5</dd>
					{/if}

					{#if $selectedNode.data.deviceCount !== undefined}
						<dt>Connected Devices</dt>
						<dd>{$selectedNode.data.deviceCount}</dd>
					{/if}

					{#if $selectedNode.data.signal !== undefined}
						<dt>Signal Strength</dt>
						<dd>{$selectedNode.data.signal} dBm</dd>
					{/if}

					{#if $selectedNode.data.manufacturer}
						<dt>Manufacturer</dt>
						<dd>{$selectedNode.data.manufacturer}</dd>
					{/if}

					{#if $selectedNode.data.profileName}
						<dt>Profile</dt>
						<dd>{$selectedNode.data.profileName}</dd>
					{/if}
				</dl>
				{#each $selectedNode.data.warnings ?? [] as warning}
					<p role="status">{warning}</p>
				{/each}
				{#if $selectedNode.data.ports}
					<p class="port-help">
						Learned addresses include devices behind other switches and eeros.
					</p>
					<table class="port-table">
						<caption>Switch ports</caption>
						<thead><tr><th>Port</th><th>Link</th><th>MACs</th></tr></thead>
						<tbody>
							{#each $selectedNode.data.ports as port (port.id)}
								<tr
									><td>{port.name}{port.kind === 'lag' ? ' (LAG)' : ''}</td><td
										>{port.status === 'up'
											? port.speed_mbps
												? `${port.speed_mbps} Mbps`
												: 'Up'
											: port.status}</td
									><td>{port.macs.length}</td></tr
								>
								{#each port.neighbors as neighbor}
									<tr
										><td colspan="3" class="neighbor"
											>LLDP: {neighbor.name || neighbor.chassis_mac || 'Neighbour'}{neighbor.port
												? ` · port ${neighbor.port}`
												: ''}</td
										></tr
									>
								{/each}
							{/each}
						</tbody>
					</table>
				{/if}
			</div>

			{#if $selectedNode.data.type !== 'switch' && !$selectedNode.data.topologyOnly}
				<div class="details-actions">
					<Button
						variant="primary"
						class="details-actions-btn"
						onclick={() => {
							const data = $selectedNode?.data;
							if (!data) return;
							if (data.type === 'gateway' || data.type === 'eero') {
								goto(`/eeros/${data.id}`);
							} else {
								goto(`/devices/${data.id}`);
							}
						}}
					>
						View Details
					</Button>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.switch-warning {
		margin: 0;
		padding: 10px 16px;
		color: var(--color-warning);
		font-size: 12px;
	}
	.port-help {
		font-size: 12px;
		color: var(--color-text-secondary);
		margin: 16px 0 8px;
	}
	.port-table {
		width: 100%;
		font-size: 12px;
		text-align: left;
		border-collapse: collapse;
	}
	.port-table caption {
		text-align: left;
		font-weight: 600;
		margin-bottom: 8px;
	}
	.port-table th,
	.port-table td {
		padding: 6px 4px;
		border-bottom: 1px solid var(--color-border);
	}
	.neighbor {
		color: var(--color-text-secondary);
		overflow-wrap: anywhere;
	}
	.topology-container {
		--xy-edge-label-background-color: var(--color-bg-secondary);
		--xy-edge-label-color: var(--color-text-primary);
		display: flex;
		flex-direction: column;
		width: 100%;
		height: 100%;
		min-height: 500px;
		position: relative;
		background: var(--color-bg-primary);
		border-radius: var(--radius-lg);
		overflow: hidden;
	}

	.map-canvas {
		position: relative;
		flex: 1;
		min-height: 350px;
	}

	:global(.svelte-flow__node-clientGroup) {
		pointer-events: none;
	}

	/* Override Svelte Flow default styles to follow the active theme */
	:global(.svelte-flow) {
		background: var(--color-bg-primary) !important;
	}

	:global(.svelte-flow__attribution) {
		display: none;
	}

	:global(.svelte-flow__controls) {
		background: var(--color-bg-secondary);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-md);
	}

	:global(.svelte-flow__controls-button) {
		background: var(--color-bg-secondary);
		border-bottom: 1px solid var(--color-border);
		fill: var(--color-text-secondary);
		width: 28px;
		height: 28px;
	}

	:global(.svelte-flow__controls-button:hover) {
		background: var(--color-bg-tertiary);
	}

	:global(.svelte-flow__controls-button:last-child) {
		border-bottom: none;
	}

	:global(.svelte-flow__minimap) {
		background: var(--color-bg-secondary) !important;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
	}

	/* Was a fixed 5%-white dot; invisible on a light background. Token-derived so it stays
	   faintly visible on both themes (see --chart-grid in app.css). */
	:global(.svelte-flow__background pattern circle) {
		fill: var(--chart-grid);
	}

	/* Map controls */
	.map-controls {
		margin: 12px;
		z-index: var(--z-dropdown);
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
		background: var(--color-bg-secondary);
		padding: 8px 14px;
		border-radius: var(--radius-lg);
		border: 1px solid var(--color-border);
		box-shadow: var(--shadow-md);
	}

	.control-group {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.control-label {
		font-size: var(--text-xs);
		color: var(--color-text-muted);
		white-space: nowrap;
	}

	.control-select {
		background: var(--color-bg-tertiary);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-sm);
		padding: 4px 8px;
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
		cursor: pointer;
		min-width: 140px;
	}

	.control-select:hover {
		border-color: var(--color-accent);
	}

	.control-select:focus {
		border-color: var(--color-accent);
	}

	.control-select:focus-visible {
		box-shadow: var(--focus-ring);
	}

	.control-select option {
		background: var(--color-bg-secondary);
		color: var(--color-text-primary);
	}

	.control-divider {
		width: 1px;
		height: 20px;
		background: var(--color-border);
	}

	.control-option {
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
		cursor: pointer;
		user-select: none;
	}

	.control-option input[type='checkbox'] {
		width: 12px;
		height: 12px;
		cursor: pointer;
	}

	.control-option:hover {
		color: var(--color-text-primary);
	}

	/* Layout-only: positions the canonical Button at the toolbar's trailing edge. */
	:global(.topology-refresh-btn) {
		margin-left: auto;
	}

	/* Edge legend */
	.edge-legend {
		flex-shrink: 0;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 12px;
		background: var(--color-bg-secondary);
		padding: 8px 14px;
		border-top: 1px solid var(--color-border);
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
	}

	.legend-item {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.legend-line {
		width: 24px;
		height: 2px;
		border-radius: 1px;
	}

	.legend-swatch {
		width: 14px;
		height: 14px;
		border-radius: 3px;
		border: 1px solid var(--color-accent);
		background: var(--color-info-bg);
	}

	.legend-swatch.wired {
		border-color: var(--color-success);
		background: var(--color-success-bg);
	}

	.legend-line.mesh {
		background: linear-gradient(
			90deg,
			var(--color-success),
			var(--color-accent),
			var(--color-warning)
		);
	}

	.legend-line.switch-link {
		background: var(--color-success);
	}

	.legend-line.inferred {
		border-top: 2px dashed var(--color-success);
	}
	.legend-line.manual {
		border-top: 2px dashed var(--color-text-muted);
	}

	/* Loading/Error/Empty overlays. --color-overlay tracks --color-bg-primary per theme, so the
	   text tokens rendered on top always have the contrast that theme expects (previously a fixed
	   black scrim paired with light-theme muted text — dark-on-dark). */
	.loading-overlay,
	.error-overlay,
	.empty-overlay {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 16px;
		background: var(--color-overlay);
		color: var(--color-text-secondary);
		z-index: var(--z-sticky);
	}

	.loading-spinner {
		width: 32px;
		height: 32px;
		border: 3px solid var(--color-border);
		border-top-color: var(--color-accent);
		border-radius: 50%;
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.error-icon {
		color: var(--color-danger);
	}

	.empty-icon {
		color: var(--color-text-muted);
	}

	/* Details panel */
	.details-panel {
		position: absolute;
		top: 70px;
		right: 12px;
		width: 260px;
		max-height: calc(100% - 90px);
		overflow: auto;
		background: var(--color-bg-secondary);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-xl);
		z-index: var(--z-sticky);
		box-shadow: var(--shadow-lg);
	}

	.details-panel.switch-details {
		width: 340px;
	}

	.details-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 14px 16px;
		border-bottom: 1px solid var(--color-border);
	}

	.details-header h3 {
		margin: 0;
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--color-text-primary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.close-btn {
		background: none;
		border: none;
		color: var(--color-text-muted);
		cursor: pointer;
		padding: 0;
		line-height: 1;
		transition: color var(--transition-fast);
	}

	.close-btn:hover {
		color: var(--color-text-primary);
	}

	.details-content {
		padding: 12px 16px;
	}

	.details-content dl {
		margin: 0;
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 8px 12px;
		font-size: var(--text-xs);
	}

	.details-content dt {
		color: var(--color-text-muted);
	}

	.details-content dd {
		margin: 0;
		color: var(--color-text-primary);
		text-align: right;
	}

	.details-content .mono {
		font-family: var(--font-mono);
		font-size: 11px;
	}

	.details-content .capitalize {
		text-transform: capitalize;
	}

	.status-badge {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 10px;
		font-weight: 500;
		text-transform: uppercase;
		background: var(--color-danger-bg);
		color: var(--color-danger);
	}

	.status-badge.online {
		background: var(--color-success-bg);
		color: var(--color-success);
	}
	.status-badge.unknown {
		background: var(--color-bg-tertiary);
		color: var(--color-text-secondary);
	}

	.details-actions {
		padding: 12px 16px;
		border-top: 1px solid var(--color-border);
	}

	/* Layout-only: the canonical Button fills the details panel's action row. */
	:global(.details-actions-btn) {
		width: 100%;
	}

	@media (max-width: 600px) {
		.map-controls {
			gap: 8px;
			margin: 8px;
		}
		.control-select {
			min-width: 0;
			max-width: 180px;
		}
		.edge-legend {
			gap: 8px;
			padding: 8px;
			font-size: 10px;
		}
		.details-panel {
			top: 120px;
			right: 8px;
			max-width: calc(100% - 16px);
		}
	}
</style>
