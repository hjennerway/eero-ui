<script lang="ts">
	import { onDestroy } from 'svelte';
	import { api } from '$api/client';
	import type { ManagedSwitchConfig, SwitchSettings } from '$api/types';
	import Button from '$components/common/Button.svelte';
	import Modal from '$components/common/Modal.svelte';
	import { topologyStore } from '$stores/topology';

	type SwitchDraft = ManagedSwitchConfig & { community: string };

	let open = $state(false);
	let loading = $state(false);
	let saving = $state(false);
	let error = $state('');
	let saved = $state(false);
	let settings = $state<SwitchSettings | null>(null);
	let switches = $state<SwitchDraft[]>([]);
	let loadRequest = 0;
	let active = true;

	const eeros = $derived(
		new Map($topologyStore.eeros.map((eero) => [eero.id, eero.location || eero.model || eero.id]))
	);
	const existing = $derived(new Map(settings?.switches.map((entry) => [entry.id, entry])));
	const changed = $derived(
		switches.some((entry) => entry.community) ||
			JSON.stringify(switches.map(({ community: _community, ...entry }) => entry)) !==
				JSON.stringify(settings?.switches)
	);

	onDestroy(() => {
		active = false;
	});

	function close() {
		if (saving) return;
		loadRequest += 1;
		open = false;
		settings = null;
		switches = [];
	}

	async function load() {
		const request = ++loadRequest;
		open = true;
		loading = true;
		saved = false;
		error = '';
		try {
			const result = await api.switches.getSettings();
			if (!active || request !== loadRequest) return;
			settings = result;
			switches = result.switches.map((entry) => ({
				...structuredClone(entry),
				community: ''
			}));
		} catch (cause) {
			if (active && request === loadRequest) {
				error = cause instanceof Error ? cause.message : 'Could not load switch settings.';
			}
		} finally {
			if (active && request === loadRequest) loading = false;
		}
	}

	async function save(event: SubmitEvent) {
		event.preventDefault();
		if (!settings || !changed || saving) return;
		const form = event.currentTarget as HTMLFormElement;
		const invalid = form.querySelector('input:invalid');
		if (invalid) {
			for (let parent = invalid.parentElement; parent; parent = parent.parentElement) {
				if (parent instanceof HTMLDetailsElement) parent.open = true;
			}
			form.reportValidity();
			return;
		}
		saving = true;
		error = '';
		try {
			await api.switches.saveSettings(settings.network_id, {
				revision: settings.revision,
				switches: switches.map((entry) => ({ ...entry, community: entry.community || null }))
			});
			if (!active) return;
			saving = false;
			close();
			saved = true;
			void topologyStore.loadTopology();
		} catch (cause) {
			if (active)
				error = cause instanceof Error ? cause.message : 'Could not save switch settings.';
		} finally {
			if (active) saving = false;
		}
	}

	function newId(prefix: string) {
		return prefix + '-' + crypto.getRandomValues(new Uint32Array(2)).join('-');
	}
</script>

<div class="settings-actions">
	<Button icon="settings" onclick={load}>Switch settings</Button>
	{#if saved}<span class="text-muted" role="status">Switch settings saved.</span>{/if}
</div>

<Modal {open} title="Switch settings" size="lg" onClose={close}>
	{#if loading}
		<p role="status">Loading switch settings…</p>
	{:else if settings}
		<form novalidate onsubmit={save}>
			<p class="text-muted">Manage the switches and known wired connections on this network.</p>
			<fieldset class="switch-list" disabled={saving}>
				{#each switches as entry (entry.id)}
					{@const original = existing.get(entry.id)}
					{@const ports = $topologyStore.switches.find((item) => item.id === entry.id)?.ports ?? []}
					{@const needsCommunity =
						!original || entry.host !== original.host || entry.port !== original.port}
					<details open={!original}>
						<summary>{entry.name || 'New managed switch'}</summary>
						<div class="editor">
							<label>
								Name
								<input class="input" bind:value={entry.name} maxlength="100" required />
							</label>
							<div class="fields">
								<label>
									Private IPv4 address
									<input
										class="input"
										bind:value={entry.host}
										placeholder="192.168.1.10"
										maxlength="15"
										required
									/>
								</label>
								<label>
									SNMP port
									<input
										class="input"
										type="number"
										bind:value={entry.port}
										min="1"
										max="65535"
										required
									/>
								</label>
							</div>
							<label>
								Read-only SNMP community
								<input
									class="input"
									type="password"
									autocomplete="new-password"
									bind:value={entry.community}
									maxlength="128"
									placeholder={needsCommunity ? undefined : 'Leave blank to keep current community'}
									required={needsCommunity}
								/>
							</label>
							{#each entry.unmanaged_switches as unmanaged (unmanaged.id)}
								<details
									open={!original?.unmanaged_switches.some((item) => item.id === unmanaged.id)}
								>
									<summary>{unmanaged.name || 'New unmanaged switch'}</summary>
									<div class="editor">
										<label>
											Name
											<input class="input" bind:value={unmanaged.name} maxlength="100" required />
										</label>
										<label>
											Upstream interface ID (ifIndex)
											<input
												class="input"
												type="number"
												bind:value={unmanaged.port}
												list={'ports-' + unmanaged.id}
												min="1"
												required
											/>
											<datalist id={'ports-' + unmanaged.id}>
												{#each ports as port (port.id)}
													<option value={port.id}>{port.name}</option>
												{/each}
											</datalist>
										</label>
										{#if eeros.size || unmanaged.eero_ids.length}
											<fieldset class="eero-options">
												<legend>Connected eeros</legend>
												{#each new Set([...eeros.keys(), ...unmanaged.eero_ids]) as id (id)}
													<label class="checkbox">
														<input
															type="checkbox"
															value={id}
															bind:group={unmanaged.eero_ids}
															disabled={!unmanaged.eero_ids.includes(id) &&
																unmanaged.eero_ids.length >= 8}
														/>
														{eeros.get(id) ?? id}
													</label>
												{/each}
											</fieldset>
										{/if}
										{#each unmanaged.devices as device, index (device)}
											<fieldset class="device">
												<legend>Known device {index + 1}</legend>
												<label>
													Device name
													<input class="input" bind:value={device.name} maxlength="100" required />
												</label>
												<div class="fields">
													<label>
														Device IPv4 address
														<input class="input" bind:value={device.ip} maxlength="15" required />
													</label>
													<label>
														Device MAC address
														<input
															class="input"
															bind:value={device.mac}
															placeholder="02:00:00:00:00:01"
															pattern={'[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}'}
															maxlength="17"
															required
														/>
													</label>
												</div>
												<Button
													size="sm"
													variant="ghost"
													onclick={() => unmanaged.devices.splice(index, 1)}
												>
													Remove device
												</Button>
											</fieldset>
										{/each}
										<div class="row">
											<Button
												size="sm"
												disabled={unmanaged.devices.length >= 32}
												onclick={() => unmanaged.devices.push({ name: '', ip: '', mac: '' })}
											>
												Add known device
											</Button>
											<Button
												size="sm"
												variant="ghost"
												onclick={() =>
													(entry.unmanaged_switches = entry.unmanaged_switches.filter(
														(item) => item.id !== unmanaged.id
													))}
											>
												Remove unmanaged switch
											</Button>
										</div>
									</div>
								</details>
							{/each}
							<div class="row">
								<Button
									size="sm"
									disabled={entry.unmanaged_switches.length >= 8}
									onclick={() =>
										entry.unmanaged_switches.push({
											id: newId('unmanaged'),
											name: '',
											port:
												ports.find(
													(port) => !entry.unmanaged_switches.some((item) => item.port === port.id)
												)?.id ?? 1,
											eero_ids: [],
											devices: []
										})}
								>
									Add unmanaged switch
								</Button>
								<Button
									size="sm"
									variant="ghost"
									onclick={() => (switches = switches.filter((item) => item.id !== entry.id))}
								>
									Remove managed switch
								</Button>
							</div>
						</div>
					</details>
				{:else}
					<p class="text-muted">No switches configured.</p>
				{/each}
				<div>
					<Button
						disabled={switches.length >= 4}
						onclick={() =>
							switches.push({
								id: newId('switch'),
								name: '',
								host: '',
								port: 161,
								community: '',
								unmanaged_switches: []
							})}
					>
						Add managed switch
					</Button>
				</div>
			</fieldset>
			{#if error}<p class="text-danger" role="alert">{error}</p>{/if}
			<div class="row footer">
				<Button onclick={close} disabled={saving}>Cancel</Button>
				<Button type="submit" variant="primary" loading={saving} disabled={!changed}>
					Save switches
				</Button>
			</div>
		</form>
	{:else}
		<p class="text-danger" role="alert">{error}</p>
		<div><Button onclick={load}>Retry</Button></div>
	{/if}
</Modal>

<style>
	.settings-actions,
	.row {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	form,
	.switch-list,
	.editor,
	.device {
		display: grid;
		gap: var(--space-3);
		min-width: 0;
	}

	fieldset {
		border: 0;
		margin: 0;
		padding: 0;
		min-width: 0;
	}

	.switch-list {
		max-height: min(52dvh, 32rem);
		overflow-y: auto;
		padding: var(--space-1);
	}

	details,
	.device {
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		background: var(--color-bg-secondary);
	}

	summary {
		cursor: pointer;
		font-weight: 600;
		overflow-wrap: anywhere;
		padding: var(--space-3);
	}

	.editor,
	.device {
		padding: var(--space-3);
	}

	.fields {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 9rem), 1fr));
		gap: var(--space-3);
	}

	label {
		display: grid;
		gap: var(--space-1);
		font-size: 0.875rem;
	}

	.checkbox {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-1) 0;
	}

	legend {
		font-size: 0.875rem;
		font-weight: 600;
	}

	.footer {
		justify-content: flex-end;
	}
</style>
