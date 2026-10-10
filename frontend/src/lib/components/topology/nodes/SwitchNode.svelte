<script lang="ts">
	import { Handle, Position } from '@xyflow/svelte';
	import Icon from '$components/common/Icon.svelte';
	import type { TopologyNodeData } from '#lib/stores/topology.js';
	let { data, selected = false }: { data: TopologyNodeData; selected?: boolean } = $props();
	let ports = $derived((data.ports ?? []).filter((p) => p.kind === 'physical'));
</script>

<div
	class="switch-node"
	class:selected
	class:unreachable={data.status === 'unreachable' || data.unmanaged}
>
	<Handle
		type="target"
		position={data.layoutType === 'horizontal' ? Position.Left : Position.Top}
	/>
	<div class="heading">
		<Icon name="ethernet" size={16} /><strong title={data.label}>{data.label}</strong>
	</div>
	<div class="address">{data.unmanaged ? 'Unmanaged switch' : data.ipAddress}</div>
	{#if !data.unmanaged}
		<div class="ports" aria-label="Switch port status">
			{#each ports as port (port.id)}
				<span
					class:up={port.status === 'up'}
					title={`${port.name}: ${port.status}${port.speed_mbps ? ` · ${port.speed_mbps} Mbps` : ''}`}
				></span>
			{/each}
		</div>
	{/if}
	<div class="summary">
		{data.unmanaged
			? 'User-supplied wiring · status unknown'
			: data.status === 'unreachable'
				? 'Unreachable'
				: `${ports.filter((p) => p.status === 'up').length} active ports`}
	</div>
	<Handle
		type="source"
		position={data.layoutType === 'horizontal' ? Position.Right : Position.Bottom}
	/>
</div>

<style>
	.switch-node {
		width: 180px;
		padding: 12px;
		border: 2px solid var(--color-success);
		border-radius: 10px;
		background: var(--color-bg-secondary);
		color: var(--color-text-primary);
	}
	.switch-node.selected {
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-accent) 35%, transparent);
	}
	.switch-node.unreachable {
		border-color: var(--color-text-muted);
	}
	.heading {
		display: flex;
		gap: 8px;
		align-items: center;
		font-size: 12px;
	}
	strong {
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
	}
	.address {
		margin-top: 6px;
		font: 11px var(--font-mono);
		color: var(--color-text-secondary);
	}
	.ports {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin-top: 8px;
	}
	.ports span {
		width: 7px;
		height: 7px;
		border-radius: 1px;
		background: var(--color-border);
	}
	.ports span.up {
		background: var(--color-success);
	}
	.summary {
		margin-top: 6px;
		font-size: 10px;
		color: var(--color-text-secondary);
	}
</style>
