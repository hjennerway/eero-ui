<script lang="ts">
	import Icon from '$components/common/Icon.svelte';
	import type { VirtualizationNode } from './types';

	interface Props {
		data: { node: VirtualizationNode; showStopped: boolean };
	}
	let { data }: Props = $props();
	let collapsed = $state(false);
	const stateLabels: Record<string, string> = {
		running: 'Running',
		online: 'Online',
		offline: 'Offline',
		stopped: 'Stopped',
		exited: 'Stopped',
		created: 'Created',
		paused: 'Paused',
		restarting: 'Restarting',
		removing: 'Removing',
		dead: 'Failed',
		unknown: 'Unknown'
	};
	const stateLabel = (state: string) => stateLabels[state] ?? state;
	const guests = $derived(data.node.guests);
</script>

<section class="proxmox-node" aria-label={`Proxmox node ${data.node.name}`}>
	<header class="node-handle">
		<div class="node-heading">
			<Icon name="server" size={20} />
			<div>
				<h2>{data.node.name}</h2>
				<p>Proxmox {data.node.version ?? '· version unavailable'}</p>
			</div>
		</div>
		<div class="node-actions">
			<span class:up={data.node.status === 'online'} class="state"
				>{stateLabel(data.node.status)}</span
			>
			<button
				type="button"
				class="nodrag collapse-button"
				aria-expanded={!collapsed}
				aria-label={`${collapsed ? 'Expand' : 'Collapse'} ${data.node.name}`}
				onclick={() => (collapsed = !collapsed)}
			>
				<Icon name={collapsed ? 'chevron-down' : 'chevron-up'} size={16} />
			</button>
		</div>
	</header>
	{#if data.node.issues.length}
		<p class="source-note">{data.node.issues.join(' ')}</p>
	{/if}
	{#if !collapsed}
		<div class="guest-grid">
			{#each guests as guest (guest.id)}
				<article
					class="guest-box"
					aria-label={`${guest.kind === 'qemu' ? 'VM' : 'LXC'} ${guest.name}`}
				>
					<div class="guest-heading">
						<h3>{guest.name}</h3>
						<span class:up={guest.status === 'running'} class="state"
							>{guest.template ? 'Template' : stateLabel(guest.status)}</span
						>
					</div>
					<p class="guest-kind">{guest.kind === 'qemu' ? 'VM' : 'LXC'} · {guest.vmid}</p>
					<p class="os">
						{guest.os_name ??
							'OS unavailable'}{#if guest.os_version && !guest.os_name?.includes(guest.os_version)}
							· {guest.os_version}{/if}
					</p>
					<p class="source-note">
						{guest.os_source === 'configured'
							? 'Configured OS · version unavailable'
							: `Reported by ${guest.os_source === 'guest_agent' ? 'guest agent' : 'Docker'}`}
					</p>
					{#if guest.ips.length}<p class="addresses">{guest.ips.join(' · ')}</p>{/if}
					{#if guest.match_status === 'ambiguous'}
						<p class="source-note">Ambiguous MAC match</p>
					{:else if guest.eero_device_ids.length}
						<div class="device-links nodrag nopan">
							{#each guest.eero_device_ids as deviceId, index (deviceId)}
								<a href={`/devices/${encodeURIComponent(deviceId)}`}
									>eero device{guest.eero_device_ids.length > 1 ? ` ${index + 1}` : ''}
									<span aria-hidden="true">↗</span></a
								>
							{/each}
						</div>
					{:else}
						<p class="source-note">No eero device match</p>
					{/if}
					{#if guest.issues.length}
						<details class="guest-issues nodrag nopan">
							<summary>Some details unavailable</summary>
							<ul>
								{#each guest.issues as issue}<li>{issue}</li>{/each}
							</ul>
						</details>
					{/if}
					{#if guest.docker.status === 'available' || guest.docker.status === 'stale'}
						<details open class="docker-box nodrag nopan">
							<summary
								><strong>Docker</strong>{#if guest.docker.version}<span>{guest.docker.version}</span
									>{/if}</summary
							>
							{#if guest.docker.status === 'stale'}
								<p class="docker-note">{guest.docker.message}</p>
							{/if}
							{#if guest.docker.collected_at && guest.docker.status === 'stale'}
								<p class="source-note">
									Last seen {new Date(guest.docker.collected_at).toLocaleString()}
								</p>
							{/if}
							<ul class="container-list">
								{#each guest.docker.containers.filter((container) => data.showStopped || container.state === 'running') as container (container.id)}
									<li>
										<span class="container-name">{container.name}</span>
										<span class:up={container.state === 'running'} class="state"
											>{stateLabel(container.state)}{#if container.health}
												· {container.health}{/if}</span
										>
									</li>
								{:else}
									<li class="source-note">
										{guest.docker.containers.length ? 'No running containers' : 'No containers'}
									</li>
								{/each}
							</ul>
						</details>
					{:else if guest.docker.status !== 'disabled'}
						<p class="source-note docker-missing">
							{guest.docker.message ?? 'Docker inventory unavailable'}
						</p>
					{/if}
				</article>
			{:else}
				<p class="empty-node">No matching guests</p>
			{/each}
		</div>
	{:else}
		<p class="collapsed-count">{guests.length} guest{guests.length === 1 ? '' : 's'}</p>
	{/if}
</section>

<style>
	.proxmox-node {
		--node-orange: #f5a15a;
		width: 100%;
		border: 2px solid var(--node-orange);
		border-radius: var(--radius-lg);
		background: color-mix(in srgb, var(--node-orange) 8%, var(--color-bg-primary));
		color: var(--color-text-primary);
		padding: var(--space-4);
	}
	:global([data-theme='light']) .proxmox-node {
		--node-orange: #a94708;
	}
	header,
	.node-heading,
	.node-actions,
	.guest-heading {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}
	header,
	.guest-heading {
		justify-content: space-between;
	}
	.node-heading {
		min-width: 0;
		color: var(--node-orange);
	}
	h2 {
		font-size: 1.1rem;
		overflow-wrap: anywhere;
		margin: 0;
	}
	h3 {
		font-size: 0.9rem;
		margin: 0;
		overflow-wrap: anywhere;
	}
	p {
		margin: var(--space-1) 0 0;
		font-size: 0.8rem;
	}
	header p {
		color: var(--color-text-secondary);
	}
	.collapse-button {
		display: grid;
		place-items: center;
		border: 1px solid var(--color-border);
		background: var(--color-bg-secondary);
		color: var(--color-text-primary);
		border-radius: var(--radius-sm);
		min-width: 32px;
		min-height: 32px;
	}
	.guest-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
		align-items: start;
		gap: var(--space-3);
		margin-top: var(--space-4);
	}
	.guest-box {
		background: var(--color-bg-tertiary);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		padding: var(--space-3);
		min-width: 0;
	}
	.guest-heading {
		align-items: flex-start;
		gap: var(--space-2);
	}
	.guest-kind,
	.source-note {
		color: var(--color-text-secondary);
		font-size: 0.75rem;
		overflow-wrap: anywhere;
	}
	.os {
		margin-top: var(--space-2);
		overflow-wrap: anywhere;
	}
	.addresses {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		overflow-wrap: anywhere;
	}
	.state {
		font-size: 0.7rem;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-sm);
		padding: 2px 5px;
		color: var(--color-text-secondary);
		flex-shrink: 0;
	}
	.state.up {
		color: var(--color-success);
	}
	.device-links {
		display: flex;
		gap: var(--space-2);
		flex-wrap: wrap;
		margin-top: var(--space-2);
		font-size: 0.75rem;
	}
	.guest-issues {
		margin-top: var(--space-2);
		font-size: 0.75rem;
		color: var(--color-text-secondary);
	}
	.guest-issues ul {
		padding-left: var(--space-4);
	}
	.docker-box {
		background: color-mix(in srgb, var(--color-info) 10%, var(--color-bg-primary));
		border: 1px solid var(--color-info);
		border-radius: var(--radius-sm);
		padding: var(--space-3);
		margin-top: var(--space-3);
	}
	.docker-box summary {
		color: var(--color-info);
		font-size: 0.85rem;
	}
	.docker-box summary span {
		margin-left: var(--space-2);
		font-size: 0.7rem;
	}
	.docker-note {
		font-size: 0.75rem;
	}
	.container-list {
		list-style: none;
		padding: 0;
		margin: var(--space-2) 0 0;
	}
	.container-list li {
		display: flex;
		gap: var(--space-2);
		align-items: flex-start;
		justify-content: space-between;
		padding: var(--space-2) 0;
		border-top: 1px solid var(--color-border-muted);
		font-size: 0.75rem;
	}
	.container-name {
		overflow-wrap: anywhere;
		min-width: 0;
	}
	.docker-missing,
	.collapsed-count {
		margin-top: var(--space-3);
	}
	@media (max-width: 480px) {
		.guest-grid {
			grid-template-columns: 1fr;
		}
		header {
			flex-wrap: wrap;
		}
	}
	@media (pointer: coarse) {
		.collapse-button {
			min-width: 44px;
			min-height: 44px;
		}
	}
</style>
