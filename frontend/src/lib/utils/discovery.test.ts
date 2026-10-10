import { describe, expect, it } from 'vitest';
import type { DiscoveryResponse, SwitchSnapshot } from '$api/types';
import { withDiscoveredDevices } from './discovery';
import { transformToTopology } from '#lib/stores/topology.js';
import type { TopologyDevice, TopologyEero } from './switch-links';

const gateway = {
	id: 'gateway',
	is_gateway: true,
	location: 'Living room',
	model: 'eero',
	status: 'green',
	mac_address: '02:00:00:00:00:01',
	ip_address: '192.168.1.1',
	ethernet_addresses: ['02:00:00:00:00:02']
} as TopologyEero;
const snapshot: DiscoveryResponse = {
	status: 'ok',
	subnet: '192.168.1.0/24',
	interval_seconds: 3600,
	last_scan: '2026-10-10T12:00:00Z',
	next_scan: '2026-10-10T13:00:00Z',
	warning: null,
	devices: [
		{
			ip: '192.168.1.165',
			mac: '02:00:00:00:00:03',
			connected: true,
			last_seen: '2026-10-10T12:00:00Z'
		}
	]
};

describe('ARP discoveries', () => {
	it('connects unknown devices to the gateway with dotted placeholder paths in every layout', () => {
		const devices = withDiscoveredDevices([], [gateway], [], snapshot);
		for (const layout of ['hierarchy', 'horizontal', 'radial', 'force'] as const) {
			const graph = transformToTopology([gateway], devices, layout);
			const node = graph.nodes.find((n) => n.type === 'device')!;
			expect(node.parentId).toBe('eero-gateway');
			expect(node.data.connectionType).toBe('unknown');
			expect(node.data.evidence).toBe('arp');
			expect(node.data.observedAt).toBe(snapshot.last_scan);
			expect(graph.edges.find((e) => e.target === node.id)?.style).toContain(
				'stroke-dasharray: 1 5'
			);
		}
	});
	it('replaces ARP placeholders when DHCP makes a camera appear in eero', () => {
		const unknown = withDiscoveredDevices([], [gateway], [], snapshot);
		const camera = {
			...unknown[0],
			topology_only: false,
			discovery_source: undefined,
			display_name: 'Camera',
			connected_to_eero: 'Office'
		} as TopologyDevice;
		const result = withDiscoveredDevices([camera, ...unknown], [gateway], [], snapshot);
		expect(result).toHaveLength(1);
		expect(result[0]).toBe(camera);
	});
	it('deduplicates MAC formatting and excludes infrastructure and configured cameras', () => {
		const camera = { mac: '02-00-00-00-00-03', topology_only: true } as TopologyDevice;
		const sw = { mac: '02:00:00:00:00:04', host: '192.168.1.10' } as SwitchSnapshot;
		const devices = [1, 2, 3, 4].map((n) => ({
			...snapshot.devices[0],
			ip: `192.168.1.${n}`,
			mac: `02:00:00:00:00:0${n}`
		}));
		expect(withDiscoveredDevices([camera], [gateway], [sw], { ...snapshot, devices })).toEqual([
			camera
		]);
	});
	it('does not infer a switch port for an ARP-only device even when its MAC is learned', () => {
		const port = {
			id: 1,
			name: 'g1',
			kind: 'physical' as const,
			status: 'up' as const,
			speed_mbps: 1000,
			macs: [gateway.mac_address],
			neighbors: []
		};
		const sw = {
			id: 'switch',
			name: 'Switch',
			host: '192.168.1.10',
			status: 'online',
			ports: [port, { ...port, id: 2, name: 'g2', macs: [snapshot.devices[0].mac] }],
			warnings: []
		} as unknown as SwitchSnapshot;
		const devices = withDiscoveredDevices([], [gateway], [sw], snapshot);
		const node = transformToTopology([gateway], devices, 'hierarchy', 'minimal', [sw]).nodes.find(
			(n) => n.type === 'device'
		);
		expect(node?.parentId).toBe('eero-gateway');
	});
	it('never marks failed or stale scan results online and does not duplicate IP aliases', () => {
		const discovery = {
			...snapshot,
			status: 'stale' as const,
			devices: [...snapshot.devices, { ...snapshot.devices[0], ip: '192.168.1.166' }]
		};
		const devices = withDiscoveredDevices([], [gateway], [], discovery);
		expect(devices).toHaveLength(1);
		expect(devices[0].connected).toBe(false);
	});
});
