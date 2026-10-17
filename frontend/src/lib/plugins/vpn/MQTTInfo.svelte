<script lang="ts">
	let { topic }: { topic: string } = $props();
</script>

<p class="text-muted">
	VPN connection status, active clients and download/upload throughput publish every 30 seconds.
	Home Assistant also gets Connect and Disconnect buttons. Set the tunnel type and name in VPN
	Connection to enable them.
</p>
{#if topic}
	<p class="text-muted">VPN readings: <code>{topic}/state</code></p>
	<p class="text-muted">
		Publish <code>connect</code> or <code>disconnect</code> to
		<code>{topic}/command</code>
		without retaining the message. Results: <code>{topic}/result</code>.
	</p>
{/if}
