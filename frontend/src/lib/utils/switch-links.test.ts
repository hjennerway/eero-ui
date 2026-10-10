import { describe, expect, it } from 'vitest';
import {
	normalizeMac,
	resolveSwitchLinks,
	withKnownSwitchDevices,
	type TopologyEero
} from './switch-links';
import type { DeviceSummary, SwitchPort, SwitchSnapshot } from '$api/types';
import {
	transformToTopology,
	type LayoutType,
	type NodeDetailLevel
} from '#lib/stores/topology.js';

const gateway = {
	id: 'gw',
	location: 'Gateway',
	model: 'eero',
	mac_address: '00:00:00:00:00:01',
	is_gateway: true,
	wired: true,
	status: 'green'
} as TopologyEero;
const leaf = {
	...gateway,
	id: 'leaf',
	location: 'Office',
	is_gateway: false,
	mac_address: '00:00:00:00:00:02',
	ethernet_addresses: ['00:00:00:00:00:03']
};
const client = {
	id: 'pc',
	mac: '02:00:00:00:00:04',
	display_name: 'PC',
	connected: true,
	wireless: false,
	connected_to_eero: 'Gateway'
} as DeviceSummary;
const wifi = {
	...client,
	id: 'wifi',
	mac: '02:00:00:00:00:05',
	display_name: 'Phone',
	wireless: true,
	connected_to_eero: 'Office'
};
const behindEero = {
	...client,
	id: 'behind',
	mac: '02:00:00:00:00:06',
	display_name: 'Behind eero',
	connected_to_eero: 'Office'
};
const port = (id: number, macs: string[], chassis?: string): SwitchPort => ({
	id,
	name: `g${id}`,
	kind: 'physical',
	status: 'up',
	speed_mbps: 1000,
	macs,
	neighbors: chassis ? [{ chassis_mac: chassis, name: 'eero', port: '1' }] : []
});
const sw: SwitchSnapshot = {
	id: 'main',
	name: 'Switch',
	host: '192.168.1.10',
	status: 'online',
	model: 'GS110TP',
	mac: '00:00:00:00:00:10',
	observed_at: '2026-10-10T12:00:00Z',
	warnings: [],
	ports: [
		port(7, [gateway.mac_address], gateway.mac_address),
		port(1, [leaf.mac_address, wifi.mac!, behindEero.mac!], leaf.ethernet_addresses[0]),
		port(4, [client.mac!])
	]
};

const unmanaged: SwitchSnapshot = {
	...sw,
	id: 'main--desk',
	name: 'Desk switch',
	host: '',
	mac: null,
	kind: 'unmanaged',
	status: 'unknown',
	observed_at: null,
	ports: [],
	upstream_switch_id: 'main',
	upstream_port_id: 1,
	eero_ids: ['leaf'],
	known_devices: [{ mac: behindEero.mac!, ip: '192.168.1.165', name: 'Known camera' }]
};

describe('known unmanaged switches', () => {
	it('uses supplied placement even when upstream telemetry is unavailable', () => {
		const snapshots = [{ ...sw, status: 'unreachable' as const, ports: [] }, unmanaged];
		const clients = withKnownSwitchDevices([wifi], snapshots);
		expect(clients.find((d) => d.mac === behindEero.mac)).toMatchObject({
			connected: false,
			topology_only: true
		});
		const links = resolveSwitchLinks([gateway, leaf], clients, snapshots);
		expect(links.get('switch-main--desk')).toMatchObject({
			parent: 'switch-main',
			evidence: 'manual'
		});
		expect(links.get('eero-leaf')).toMatchObject({
			parent: 'switch-main--desk',
			evidence: 'manual'
		});
		expect(links.get(`device-${behindEero.mac}`)?.parent).toBe('switch-main--desk');
		expect(links.has(`device-${wifi.mac}`)).toBe(false);
	});
	it('deduplicates real clients and derives static-client activity from the correct port', () => {
		expect(withKnownSwitchDevices([behindEero], [sw, unmanaged])).toEqual([behindEero]);
		expect(withKnownSwitchDevices([], [sw, unmanaged])[0].connected).toBe(true);
		const wrongPort = { ...sw, ports: [port(1, []), port(4, [behindEero.mac!])] };
		expect(withKnownSwitchDevices([], [wrongPort, unmanaged])[0].connected).toBe(false);
	});
	for (const layout of ['hierarchy', 'horizontal', 'radial', 'force'] as LayoutType[]) {
		it(`includes a known intermediate switch with no invented ports or speeds in ${layout}`, () => {
			const snapshots = [sw, unmanaged];
			const graph = transformToTopology(
				[gateway, leaf],
				withKnownSwitchDevices([wifi], snapshots),
				layout,
				'minimal',
				snapshots
			);
			const node = graph.nodes.find((n) => n.id === 'switch-main--desk')!;
			expect(node.data).toMatchObject({ unmanaged: true, status: 'unknown', evidence: 'manual' });
			expect(node.data.ports).toBeUndefined();
			expect(graph.edges.find((e) => e.target === 'eero-leaf')).toMatchObject({
				source: node.id,
				label: 'User supplied'
			});
			expect(graph.nodes.find((n) => n.id === `device-${behindEero.mac}`)?.parentId).toBe(node.id);
			expect(graph.nodes.find((n) => n.id === `device-${wifi.mac}`)?.parentId).toBe('eero-leaf');
		});
	}
});

describe('switch evidence', () => {
	it('normalizes identities without guessing a device from its manufacturer', () => {
		expect(normalizeMac('02-AA-bb-00-00-01')).toBe('02aabb000001');
		expect(normalizeMac('01:00:5e:00:00:01')).toBeUndefined();
		expect(normalizeMac('not a mac')).toBeUndefined();
	});
	it('uses Ethernet aliases and keeps wireless/behind-eero clients with their eero', () => {
		const links = resolveSwitchLinks([gateway, leaf], [client, wifi, behindEero], [sw]);
		expect(links.get('switch-main')).toMatchObject({
			parent: 'eero-gw',
			evidence: 'lldp',
			port: { name: 'g7' }
		});
		expect(links.get('eero-leaf')).toMatchObject({
			parent: 'switch-main',
			evidence: 'lldp',
			port: { name: 'g1' }
		});
		expect(links.get(`device-${client.mac}`)).toMatchObject({
			parent: 'switch-main',
			evidence: 'mac',
			port: { name: 'g4' }
		});
		expect(links.has(`device-${wifi.mac}`)).toBe(false);
		expect(links.has(`device-${behindEero.mac}`)).toBe(false);
	});
	it('does not invent a parent from ambiguous, offline or uplink observations', () => {
		const duplicate = {
			...sw,
			id: 'second',
			ports: sw.ports.map((p) => ({ ...p, neighbors: [] }))
		};
		expect(
			resolveSwitchLinks([gateway, leaf], [client], [sw, duplicate]).has(`device-${client.mac}`)
		).toBe(false);
		expect(
			resolveSwitchLinks([gateway], [{ ...client, connected: false }], [sw]).has(
				`device-${client.mac}`
			)
		).toBe(false);
		expect(resolveSwitchLinks([gateway], [client], [{ ...sw, status: 'unreachable' }]).size).toBe(
			0
		);
		const upstream = { ...sw, ports: [port(7, [gateway.mac_address, client.mac!])] };
		expect(resolveSwitchLinks([gateway], [client], [upstream]).has(`device-${client.mac}`)).toBe(
			false
		);
	});
	it('leaves ambiguous gateway ports unassigned and prefers LLDP to indirect MACs', () => {
		const ambiguous = { ...sw, ports: [...sw.ports, port(8, [gateway.mac_address])] };
		expect(resolveSwitchLinks([gateway], [client], [ambiguous]).has('switch-main')).toBe(false);
		const indirect = { ...sw, id: 'second', ports: sw.ports.map((p) => ({ ...p, neighbors: [] })) };
		expect(resolveSwitchLinks([gateway, leaf], [], [sw, indirect]).get('eero-leaf')?.parent).toBe(
			'switch-main'
		);
	});
});

describe('connected branch layout', () => {
	for (const layout of ['hierarchy', 'horizontal'] as const) {
		for (const detail of ['minimal', 'standard', 'detailed'] as NodeDetailLevel[]) {
			it(`keeps gateway client paths outside downstream switch links in ${layout}/${detail}`, () => {
				const gatewayWifi = { ...wifi, connected_to_eero: 'Gateway' };
				const graph = transformToTopology([gateway, leaf], [client, gatewayWifi], layout, detail, [
					sw
				]);
				const byId = new Map(graph.nodes.map((n) => [n.id, n]));
				const source = byId.get('switch-main')!;
				const target = byId.get('eero-leaf')!;
				const root = byId.get('eero-gw')!;
				// The gateway stem runs down from its centre (or right from its
				// centre in horizontal mode). It must not cut the switch's bus.
				const axis = layout === 'hierarchy' ? 'x' : 'y';
				const centre = layout === 'hierarchy' ? 90 : 40;
				const stem = root.position[axis] + centre;
				const span = [source.position[axis] + centre, target.position[axis] + centre];
				expect(stem < Math.min(...span) || stem > Math.max(...span)).toBe(true);
			});
		}
		it(`keeps switch subtrees together despite interleaved location names in ${layout}`, () => {
			const a = { ...leaf, id: 'a', location: 'A Office' };
			const z = {
				...leaf,
				id: 'z',
				location: 'Z Garage',
				mac_address: '00:00:00:00:00:12',
				ethernet_addresses: []
			};
			const m = {
				...leaf,
				id: 'm',
				location: 'M Hall',
				mac_address: '00:00:00:00:00:14',
				ethernet_addresses: []
			};
			const first = { ...sw, ports: [...sw.ports, port(2, [z.mac_address], z.mac_address)] };
			const second = {
				...sw,
				id: 'second',
				name: 'Second switch',
				mac: '00:00:00:00:00:20',
				ports: [
					port(7, [gateway.mac_address], gateway.mac_address),
					port(1, [m.mac_address], m.mac_address)
				]
			};
			const clients = [a, z, m, gateway].map((e, i) => ({
				...wifi,
				mac: `02:00:00:00:01:0${i}`,
				connected_to_eero: e.location
			}));
			const graph = transformToTopology([gateway, a, m, z], clients, layout, 'minimal', [
				first,
				second
			]);
			const axis = layout === 'hierarchy' ? 'x' : 'y';
			const byId = new Map(graph.nodes.map((n) => [n.id, n]));
			const firstSpan = ['switch-main', 'eero-a', 'eero-z'].map(
				(id) => byId.get(id)!.position[axis]
			);
			for (const id of ['switch-second', 'eero-m']) {
				const value = byId.get(id)!.position[axis];
				expect(value < Math.min(...firstSpan) || value > Math.max(...firstSpan)).toBe(true);
			}
			expect(
				transformToTopology([z, m, a, gateway], [...clients].reverse(), layout, 'minimal', [
					second,
					first
				])
			).toEqual(graph);
		});
	}
});

for (const layout of ['hierarchy', 'horizontal', 'radial', 'force'] as LayoutType[]) {
	for (const detail of ['minimal', 'standard', 'detailed'] as NodeDetailLevel[]) {
		it(`includes switches without duplicates or overlaps in ${layout}/${detail}`, () => {
			const clients = [client, wifi, behindEero, { ...client, id: 'switch-device', mac: sw.mac! }];
			const graph = transformToTopology([gateway, leaf], clients, layout, detail, [sw]);
			const byId = new Map(graph.nodes.map((n) => [n.id, n]));
			expect(byId.size).toBe(graph.nodes.length);
			expect(graph.nodes.filter((n) => n.type === 'switch')).toHaveLength(1);
			expect(graph.nodes.filter((n) => n.type === 'device')).toHaveLength(3);
			expect(byId.get(`device-${client.mac}`)?.parentId).toBe('switch-main');
			expect(byId.get(`device-${wifi.mac}`)?.parentId).toBe('eero-leaf');
			expect(graph.edges.find((e) => e.target === 'eero-leaf')?.source).toBe('switch-main');
			for (const edge of graph.edges) {
				expect(byId.has(edge.source)).toBe(true);
				expect(byId.has(edge.target)).toBe(true);
			}
			const boxes = graph.nodes
				.filter((n) => n.type !== 'device')
				.map((n) => ({
					id: n.id,
					x: n.position.x + (byId.get(n.parentId ?? '')?.position.x ?? 0),
					y: n.position.y + (byId.get(n.parentId ?? '')?.position.y ?? 0),
					w: n.width ?? 180,
					h: n.height ?? (n.type === 'switch' ? 120 : detail === 'minimal' ? 80 : 150)
				}));
			for (let i = 0; i < boxes.length; i++)
				for (const b of boxes.slice(i + 1)) {
					const a = boxes[i];
					expect(
						a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y,
						`${a.id} overlaps ${b.id}`
					).toBe(false);
				}
			expect(
				transformToTopology([leaf, gateway], [...clients].reverse(), layout, detail, [sw])
			).toEqual(graph);
		});
	}
}
