<script lang="ts">
	import { onMount } from 'svelte';
	import { enabledPlugins, loadPlugins, pluginState } from '#lib/plugins/registry.js';
	import Button from '$components/common/Button.svelte';
	let { data }: { data: { pluginId: string } } = $props();
	const plugin = $derived($enabledPlugins.find((item) => item.id === data.pluginId));
	const component = $derived(plugin?.load());
	onMount(() => {
		void loadPlugins();
	});
</script>

{#if $pluginState.error}
	<p role="alert">{$pluginState.error}</p>
	<Button onclick={() => loadPlugins()}>Retry</Button>
{:else if !$pluginState.loaded}
	<p role="status">Loading plugin…</p>
{:else if !plugin}
	<p role="alert">This plugin is disabled on this server.</p>
{:else}
	{#key data.pluginId}
		{#await component}
			<p role="status">Loading {plugin.label}…</p>
		{:then module}
			{#if module}<module.default />{/if}
		{:catch}
			<p role="alert">Could not load {plugin.label}. Reload the page to try again.</p>
		{/await}
	{/key}
{/if}
