<script lang="ts">
	import Button from '$components/common/Button.svelte';
	import type { VirtualizationConnection, VirtualizationConnectionUpdate } from './types';
	let {
		connection,
		saving,
		error,
		onsave
	}: {
		connection: VirtualizationConnection;
		saving: boolean;
		error: string | null;
		onsave: (update: VirtualizationConnectionUpdate) => void;
	} = $props();
	let url = $state('');
	let tokenId = $state('');
	let tokenSecret = $state('');
	let pveCa = $state('');
	let dockerEnabled = $state(false);
	let dockerPort = $state(2376);
	let dockerCa = $state('');
	let dockerCert = $state('');
	let dockerKey = $state('');
	$effect(() => {
		url = connection.url;
		tokenId = connection.token_id;
		dockerEnabled = connection.docker_enabled;
		dockerPort = connection.docker_port;
		tokenSecret = pveCa = dockerCa = dockerCert = dockerKey = '';
	});
	const value = (text: string) => text.trim() || null;
	function submit(event: SubmitEvent) {
		event.preventDefault();
		onsave({
			url,
			token_id: tokenId,
			token_secret: value(tokenSecret),
			ca_certificate: value(pveCa),
			docker_enabled: dockerEnabled,
			docker_port: dockerPort,
			docker_ca_certificate: value(dockerCa),
			docker_client_certificate: value(dockerCert),
			docker_client_key: value(dockerKey)
		});
	}
</script>

<form class="connection-form" onsubmit={submit}>
	<h2>{connection.configured ? 'Connection settings' : 'Connect your Proxmox cluster'}</h2>
	<p class="text-muted">
		Connect one node to discover the cluster. Use a dedicated monitoring API token.
	</p>
	{#if error}<p role="alert" class="form-error">{error}</p>{/if}
	<div class="form-grid">
		<label for="pve-url"
			>Proxmox address<input
				id="pve-url"
				class="input"
				type="url"
				bind:value={url}
				placeholder="https://192.168.4.10:8006"
				required
			/></label
		>
		<label for="pve-token-id"
			>API token ID<input
				id="pve-token-id"
				class="input"
				bind:value={tokenId}
				placeholder="monitoring@pve!eero-ui"
				required
				autocomplete="off"
			/></label
		>
		<label for="pve-token-secret"
			>API token secret<input
				id="pve-token-secret"
				class="input"
				type="password"
				bind:value={tokenSecret}
				required={!connection.has_token}
				autocomplete="new-password"
				placeholder={connection.has_token ? 'Leave blank to keep saved token' : 'Token secret'}
			/></label
		>
		<label for="pve-ca"
			>Proxmox CA certificate (PEM)<textarea
				id="pve-ca"
				class="input"
				bind:value={pveCa}
				rows="3"
				placeholder={connection.has_ca_certificate
					? 'Leave blank to keep saved certificate'
					: 'Paste the cluster CA certificate, or leave blank for a publicly trusted certificate'}
			></textarea></label
		>
	</div>
	<label class="docker-toggle"
		><input type="checkbox" bind:checked={dockerEnabled} /> Discover Docker hosts using authenticated
		HTTPS</label
	>
	{#if dockerEnabled}
		<p class="text-muted">
			Enable the TLS listener on each existing Docker engine. Guest LAN addresses are discovered
			automatically. Certificates must cover those addresses.
		</p>
		<div class="form-grid">
			<label for="docker-port"
				>Docker HTTPS port<input
					id="docker-port"
					class="input"
					type="number"
					min="1"
					max="65535"
					bind:value={dockerPort}
					required
				/></label
			>
			<label for="docker-ca"
				>Docker CA certificate (PEM)<textarea
					id="docker-ca"
					class="input"
					rows="3"
					bind:value={dockerCa}
					required={!connection.has_docker_certificates}
					placeholder={connection.has_docker_certificates
						? 'Leave blank to keep saved certificate'
						: 'CA certificate'}></textarea></label
			>
			<label for="docker-cert"
				>Docker client certificate (PEM)<textarea
					id="docker-cert"
					class="input"
					rows="3"
					bind:value={dockerCert}
					required={!connection.has_docker_certificates}
					placeholder={connection.has_docker_certificates
						? 'Leave blank to keep saved certificate'
						: 'Client certificate'}></textarea></label
			>
			<label for="docker-key"
				>Docker client private key (PEM)<textarea
					id="docker-key"
					class="input"
					rows="3"
					bind:value={dockerKey}
					required={!connection.has_docker_certificates}
					autocomplete="off"
					spellcheck={false}
					placeholder={connection.has_docker_certificates
						? 'Leave blank to keep saved key'
						: 'Client private key'}></textarea></label
			>
		</div>
	{/if}
	<p class="text-muted">
		Saved credentials stay on the server. Docker client credentials grant access to the engine; keep
		them protected.
	</p>
	<Button type="submit" variant="primary" loading={saving} icon="check">Save and discover</Button>
</form>

<style>
	.connection-form {
		background: var(--color-bg-secondary);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
		padding: var(--space-5);
	}
	h2 {
		font-size: 1.1rem;
		margin-bottom: var(--space-2);
	}
	p {
		font-size: 0.85rem;
		margin: var(--space-2) 0 var(--space-4);
	}
	.form-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-4);
	}
	label {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		font-size: 0.85rem;
	}
	input,
	textarea {
		width: 100%;
		min-width: 0;
	}
	textarea {
		font-family: var(--font-mono);
		font-size: 0.8rem;
	}
	.docker-toggle {
		flex-direction: row;
		align-items: center;
		margin: var(--space-4) 0;
	}
	.docker-toggle input {
		width: auto;
	}
	.form-error {
		color: var(--color-danger);
	}
	@media (max-width: 640px) {
		.form-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
