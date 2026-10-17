<script lang="ts">
	import { SvelteFlow, Controls, Background, type Node, type NodeTypes } from '@xyflow/svelte';
	import '@xyflow/svelte/dist/style.css';
	import type { VirtualizationNode } from './types';
	import ProxmoxNode from './ProxmoxNode.svelte';
	import { theme } from '#lib/stores/ui.js';
	let { nodes: inventory, showStopped }: { nodes: VirtualizationNode[]; showStopped: boolean } =
		$props();
	const nodeTypes: NodeTypes = { proxmox: ProxmoxNode };
	let positions = $state<Record<string, { x: number; y: number }>>({});
	const nodes = $derived<Node[]>(
		inventory.map((node, index) => ({
			id: node.id,
			type: 'proxmox',
			position: positions[node.id] ?? { x: index * 660, y: 0 },
			style: 'width: 620px; border: none; background: transparent; padding: 0;',
			dragHandle: '.node-handle',
			data: { node, showStopped }
		}))
	);
</script>

<div class="virtualization-map" aria-label="Virtualization diagram">
	<SvelteFlow
		{nodes}
		edges={[]}
		{nodeTypes}
		fitView
		colorMode={$theme}
		minZoom={0.15}
		maxZoom={1.5}
		nodesConnectable={false}
		elementsSelectable={false}
		onnodedragstop={({ targetNode }) => {
			if (targetNode) positions = { ...positions, [targetNode.id]: targetNode.position };
		}}
	>
		<Background />
		<Controls showLock={false} />
	</SvelteFlow>
</div>

<style>
	.virtualization-map {
		height: min(70vh, 900px);
		min-height: 500px;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
		overflow: hidden;
	}
</style>
