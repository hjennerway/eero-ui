import type { DeviceSummary, EeroSummary, SwitchSnapshot, SwitchPort } from '$api/types';

export type TopologyEero = EeroSummary & { ethernet_addresses?: string[] | null };
export interface SwitchLink {
	parent: string;
	switchId: string;
	port: SwitchPort;
	evidence: 'lldp' | 'mac';
}

/** Locally administered addresses are valid identities, but never vendor evidence. */
export function normalizeMac(mac?: string | null): string | undefined {
	const compact = mac?.trim().replace(/[:-]/g, '').toLowerCase();
	if (!compact || !/^[0-9a-f]{12}$/.test(compact) || /^0{12}$/.test(compact)) return;
	if (parseInt(compact.slice(0, 2), 16) & 1) return;
	return compact;
}

export function resolveSwitchLinks(
	eeros: TopologyEero[],
	devices: DeviceSummary[],
	switches: SwitchSnapshot[]
): Map<string, SwitchLink> {
	const links = new Map<string, SwitchLink>();
	const gateway = eeros.find((e) => e.is_gateway);
	const macs = (e: TopologyEero) =>
		new Set([e.mac_address, ...(e.ethernet_addresses ?? [])].map(normalizeMac).filter(Boolean));
	const matches = (values: Set<string | undefined>, value?: string | null) => {
		const mac = normalizeMac(value);
		return mac !== undefined && values.has(mac);
	};
	const gatewayMacs = gateway ? macs(gateway) : new Set<string>();
	const online = switches.filter((s) => s.status === 'online');
	const upstream = new Map<string, number>();
	const occupied = new Map<string, Set<number>>();
	const switchMacs = new Set(online.map((s) => normalizeMac(s.mac)).filter(Boolean));
	const infrastructureMacs = new Set([...eeros.flatMap((e) => [...macs(e)]), ...switchMacs]);
	const evidenceFor = (port: SwitchPort, identities: Set<string | undefined>) =>
		port.neighbors.some((n) => matches(identities, n.chassis_mac))
			? ('lldp' as const)
			: port.macs.some((m) => matches(identities, m))
				? ('mac' as const)
				: undefined;

	for (const sw of online) {
		const ports = sw.ports.filter((p) => p.status === 'up');
		const gatewayPorts = ports.filter((p) => evidenceFor(p, gatewayMacs));
		// A gateway seen on multiple ports is ambiguous (e.g. VLANs or a loop).
		if (gateway && gatewayPorts.length === 1) {
			const port = gatewayPorts[0];
			upstream.set(sw.id, port.id);
			const peers = online.filter(
				(other) =>
					other.id !== sw.id &&
					port.neighbors.some(
						(n) =>
							normalizeMac(n.chassis_mac) !== undefined &&
							normalizeMac(n.chassis_mac) === normalizeMac(other.mac)
					)
			);
			if (peers.length <= 1)
				links.set(`switch-${sw.id}`, {
					parent: peers.length ? `switch-${peers[0].id}` : `eero-${gateway.id}`,
					switchId: sw.id,
					port,
					evidence: peers.length ? 'lldp' : evidenceFor(port, gatewayMacs)!
				});
		}
		occupied.set(
			sw.id,
			new Set(
				ports
					.filter(
						(p) =>
							p.macs.some((m) => matches(infrastructureMacs, m)) ||
							p.neighbors.some((n) => matches(infrastructureMacs, n.chassis_mac))
					)
					.map((p) => p.id)
			)
		);
	}

	const candidates = (identities: Set<string | undefined>, client: boolean): SwitchLink[] => {
		const found: SwitchLink[] = [];
		for (const sw of online)
			for (const port of sw.ports) {
				if (port.status !== 'up' || port.id === upstream.get(sw.id)) continue;
				const evidence = evidenceFor(port, identities);
				if (!evidence) continue;
				// MAC learning describes reachability, including every client behind an
				// eero/uplink. Keep those clients attached to their eero in the diagram.
				if (
					evidence === 'mac' &&
					(!upstream.has(sw.id) || (client && occupied.get(sw.id)?.has(port.id)))
				)
					continue;
				found.push({ parent: `switch-${sw.id}`, switchId: sw.id, port, evidence });
			}
		const direct = found.filter((c) => c.evidence === 'lldp');
		return direct.length ? direct : found;
	};
	for (const eero of eeros) {
		if (eero.is_gateway || !eero.wired || eero.status !== 'green') continue;
		const found = candidates(macs(eero), false);
		if (found.length === 1) links.set(`eero-${eero.id}`, found[0]);
	}
	for (const device of devices) {
		const mac = normalizeMac(device.mac);
		if (device.wireless || !device.connected || !mac || infrastructureMacs.has(mac)) continue;
		const found = candidates(new Set([mac]), true);
		if (found.length === 1) links.set(`device-${device.mac || device.id}`, found[0]);
	}
	// Conflicting neighbour reports must never introduce a graph cycle.
	for (const id of [...links.keys()]) {
		const seen = new Set([id]);
		let current = links.get(id)?.parent;
		while (current) {
			if (seen.has(current)) {
				links.delete(id);
				break;
			}
			seen.add(current);
			current = links.get(current)?.parent;
		}
	}
	return links;
}
