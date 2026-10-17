<script lang="ts">
	let { topic: _topic }: { topic: string } = $props();
</script>

<p class="text-muted">
	Configured NAS devices publish every 5 minutes. Each volume includes total size, available size,
	used space, percentage used, status and health, with six sensors per volume when auto discovery is
	enabled.
</p>
<p class="text-muted">
	NAS network receive and transmit throughput publish every 30 seconds in Mbit/s using the existing
	SNMP connection. Receive is traffic to the NAS; transmit is traffic from the NAS, including LAN
	transfers. The first reading needs two samples.
</p>
<p class="text-muted">
	Each disk has seven sensors: health, SMART health, status, temperature, capacity, power-on hours
	and SSD life remaining. Power-on hours and SSD life require a saved QTS connection; unsupported
	readings remain unknown. Find these sensors under the NAS device in Home Assistant.
</p>
