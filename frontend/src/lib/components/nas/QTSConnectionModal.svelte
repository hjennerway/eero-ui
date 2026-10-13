<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$api/client';
	import type { QTSConnectionStatus, QTSConnectionUpdate } from '$api/types';
	import Modal from '$components/common/Modal.svelte';
	import Button from '$components/common/Button.svelte';

	let {
		nasId,
		networkId,
		name,
		host,
		onClose,
		onSaved
	}: {
		nasId: string;
		networkId: string;
		name: string;
		host: string;
		onClose: () => void;
		onSaved: () => void;
	} = $props();

	let status: QTSConnectionStatus | null = $state(null);
	let username = $state('');
	let password = $state('');
	let port: number | undefined = $state(443);
	let https = $state(true);
	let verifyTLS = $state(true);
	let loading = $state(true);
	let busy = $state<'save' | 'test' | 'disconnect' | null>(null);
	let error = $state('');
	let testMessage = $state('');
	let active = true;
	let form = $state<HTMLFormElement>();
	const canConnect = $derived.by(() =>
		Boolean(username.trim() && (password || status?.password_set) && port)
	);

	async function load() {
		loading = true;
		error = '';
		try {
			const result = await api.nas.getQTS(nasId, networkId);
			if (!active) return;
			status = result;
			username = result.username;
			port = result.port;
			https = result.https;
			verifyTLS = result.verify_tls;
		} catch (cause) {
			if (active) error = cause instanceof Error ? cause.message : 'Could not load QTS connection.';
		} finally {
			if (active) loading = false;
		}
	}

	function connection(enabled = true): QTSConnectionUpdate {
		return {
			enabled,
			username: username.trim(),
			password: password || null,
			port: port ?? 443,
			https,
			verify_tls: verifyTLS
		};
	}

	async function run(action: 'save' | 'test' | 'disconnect') {
		if (busy || (action !== 'disconnect' && !form?.reportValidity())) return;
		busy = action;
		error = '';
		testMessage = '';
		try {
			if (action === 'test') {
				const result = await api.nas.testQTS(nasId, networkId, connection());
				if (!active) return;
				if (result.success) testMessage = `${result.message} Save to use this connection.`;
				else error = result.message;
			} else {
				await api.nas.updateQTS(nasId, networkId, connection(action !== 'disconnect'));
				if (!active) return;
				password = '';
				onSaved();
			}
		} catch (cause) {
			if (active)
				error = cause instanceof Error ? cause.message : 'Could not update QTS connection.';
		} finally {
			if (active) busy = null;
		}
	}

	function toggleHTTPS() {
		if (https && port === 8080) port = 443;
		else if (!https && port === 443) port = 8080;
	}

	onMount(() => {
		void load();
		return () => {
			active = false;
			password = '';
		};
	});
</script>

<Modal
	open
	title="QTS connection"
	size="lg"
	onClose={() => {
		if (!busy) onClose();
	}}
>
	<div class="connection-content">
		<p class="text-muted">{name} · {host}</p>
		<p>Connect to QTS to read drive power-on time and supported SSD life readings.</p>
		{#if error}<p class="form-error" role="alert">{error}</p>{/if}
		{#if testMessage}<p class="test-success" role="status">{testMessage}</p>{/if}
		{#if loading}<p role="status">Loading connection…</p>
		{:else if !status}<Button onclick={() => void load()}>Retry</Button>
		{:else}
			<form
				bind:this={form}
				onsubmit={(event) => {
					event.preventDefault();
					void run('save');
				}}
				oninput={() => {
					testMessage = '';
					error = '';
				}}
			>
				<fieldset disabled={busy !== null}>
					<div class="credentials">
						<div class="field">
							<label for="qts-username">QTS username</label>
							<input
								id="qts-username"
								class="input"
								bind:value={username}
								required
								maxlength="256"
								autocomplete="username"
							/>
						</div>
						<div class="field">
							<label for="qts-password">QTS password</label>
							<input
								id="qts-password"
								class="input"
								type="password"
								bind:value={password}
								required={!status.password_set}
								maxlength="4096"
								autocomplete="new-password"
								placeholder={status.password_set ? 'Leave blank to keep saved password' : ''}
								aria-describedby="qts-password-help"
							/>
						</div>
					</div>
					<p id="qts-password-help" class="text-muted">
						{status.password_set ? 'A password is saved. ' : ''}Login details are stored on the
						dashboard server. The saved password is never sent back to this page.
					</p>
					<div class="field port">
						<label for="qts-port">QTS web port</label>
						<input
							id="qts-port"
							class="input"
							type="number"
							bind:value={port}
							min="1"
							max="65535"
							step="1"
							required
						/>
					</div>
					<label class="toggle"
						><input type="checkbox" bind:checked={https} onchange={toggleHTTPS} />Use HTTPS</label
					>
					{#if https}
						<label class="toggle"
							><input type="checkbox" bind:checked={verifyTLS} />Verify NAS certificate</label
						>
						{#if !verifyTLS}<p class="text-muted">
								Certificate verification is disabled. Use this only for a trusted NAS with a
								self-signed certificate.
							</p>{/if}
					{:else}<p class="text-muted">
							HTTP sends login details without encryption. Use it only on a trusted local network.
						</p>{/if}
					<p class="text-muted">
						Use a QTS account with permission to read disk health. Two-step login is not supported.
					</p>
					<div class="actions">
						<Button
							type="button"
							disabled={!canConnect || busy !== null}
							loading={busy === 'test'}
							onclick={() => void run('test')}>Test connection</Button
						>
						<Button
							type="submit"
							variant="primary"
							disabled={!canConnect || busy !== null}
							loading={busy === 'save'}>Save connection</Button
						>
					</div>
					{#if status.enabled}
						<div class="disconnect">
							<Button
								variant="danger"
								disabled={busy !== null}
								loading={busy === 'disconnect'}
								onclick={() => void run('disconnect')}>Remove connection</Button
							>
							<p class="text-muted">
								Removes the saved QTS login. Disk health and storage monitoring continue.
							</p>
						</div>
					{/if}
				</fieldset>
			</form>
		{/if}
	</div>
</Modal>

<style>
	.connection-content {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		max-height: 75dvh;
		overflow-y: auto;
	}
	p {
		margin: 0;
		font-size: var(--text-sm);
		line-height: 1.5;
	}
	fieldset {
		border: 0;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
		min-width: 0;
	}
	.credentials {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-3);
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}
	.port {
		max-width: 150px;
	}
	.input {
		width: 100%;
		box-sizing: border-box;
	}
	label {
		font-size: var(--text-sm);
	}
	.toggle {
		display: flex;
		align-items: center;
		gap: var(--space-2);
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: var(--space-3);
		flex-wrap: wrap;
	}
	.disconnect {
		border-top: 1px solid var(--color-border);
		padding-top: var(--space-4);
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--space-2);
	}
	.form-error {
		color: var(--color-danger);
	}
	.test-success {
		color: var(--color-success);
	}
	@media (max-width: 500px) {
		.credentials {
			grid-template-columns: 1fr;
		}
	}
</style>
