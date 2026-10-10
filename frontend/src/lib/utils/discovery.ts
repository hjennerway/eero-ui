import type { DiscoveryResponse, SwitchSnapshot } from '$api/types';
import { normalizeMac, type TopologyDevice, type TopologyEero } from './switch-links';

/** ARP proves presence on this LAN, never a particular port, eero or connection type. */
export function withDiscoveredDevices(
	devices: TopologyDevice[],
	eeros: TopologyEero[],
	switches: SwitchSnapshot[],
	discovery: DiscoveryResponse
): TopologyDevice[] {
	const result = devices.filter((d) => d.discovery_source !== 'arp');
	const identities = new Set([
		...result.map((d) => normalizeMac(d.mac)),
		...eeros.flatMap((e) => [e.mac_address, ...(e.ethernet_addresses ?? [])].map(normalizeMac)),
		...switches.map((s) => normalizeMac(s.mac))
	]);
	const knownIps = new Set([
		...result.filter((d) => d.connected).map((d) => d.ip),
		...eeros.map((e) => e.ip_address),
		...switches.map((s) => s.host)
	]);
	for (const found of [...discovery.devices].sort(
		(a, b) => Number(b.connected) - Number(a.connected) || b.last_seen.localeCompare(a.last_seen)
	)) {
		const mac = normalizeMac(found.mac);
		if (!mac || identities.has(mac) || knownIps.has(found.ip)) continue;
		identities.add(mac);
		result.push({
			id: found.mac,
			mac: found.mac,
			ip: found.ip,
			display_name: `Unknown device ${found.ip}`,
			connected: found.connected && discovery.status === 'ok',
			wireless: false,
			topology_only: true,
			discovery_source: 'arp',
			last_seen: found.last_seen
		} as TopologyDevice);
	}
	return result;
}
