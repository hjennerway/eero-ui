/** Network topology data, client grouping, and deterministic layout. */
import { writable, derived } from 'svelte/store';
import type { Node, Edge } from '@xyflow/svelte';
import { api } from '$api/client';
import type {
	EeroSummary,
	DeviceSummary,
	SwitchSnapshot,
	SwitchPort,
	DiscoveryResponse
} from '$api/types';
import { withDiscoveredDevices } from '#lib/utils/discovery.js';
import {
	normalizeMac,
	resolveSwitchLinks,
	withKnownSwitchDevices,
	type TopologyEero,
	type TopologyDevice
} from '#lib/utils/switch-links.js';

export type NodeType = 'gateway' | 'eero' | 'switch' | 'device' | 'clientGroup';
export type EdgeQuality = 'excellent' | 'good' | 'fair' | 'poor';
export type LayoutType = 'hierarchy' | 'radial' | 'horizontal' | 'force';
export type NodeDetailLevel = 'minimal' | 'standard' | 'detailed';

export interface TopologyNodeData extends Record<string, unknown> {
	type: NodeType;
	id: string;
	label: string;
	status: 'online' | 'offline' | 'unreachable' | 'unknown';
	unmanaged?: boolean;
	topologyOnly?: boolean;
	ports?: SwitchPort[];
	observedAt?: string;
	warnings?: string[];
	portName?: string;
	evidence?: 'lldp' | 'mac' | 'manual' | 'arp';
	meshQuality?: number;
	deviceCount?: number;
	model?: string;
	isGateway?: boolean;
	wired?: boolean;
	ipAddress?: string;
	firmwareVersion?: string;
	signal?: number;
	connectionType?: 'wired' | 'wireless' | 'unknown';
	ip?: string;
	mac?: string;
	manufacturer?: string;
	isBlocked?: boolean;
	isPaused?: boolean;
	profileName?: string;
	eeroLabel?: string;
	detailLevel?: NodeDetailLevel;
	layoutType?: LayoutType;
}

export interface TopologyEdgeData extends Record<string, unknown> {
	quality: EdgeQuality;
	type: 'mesh' | 'client' | 'switch';
	connectionType?: 'wired' | 'wireless' | 'unknown';
	/** Distance from the client's left handle to the shared branch above its group. */
	branchOffset?: number;
	previousRowOffset?: number;
	horizontal?: boolean;
}

export interface TopologyState {
	eeros: TopologyEero[];
	devices: TopologyDevice[];
	switches: SwitchSnapshot[];
	switchWarning: string | null;
	discovery: DiscoveryResponse | null;
	loading: boolean;
	error: string | null;
}

export interface LayoutOptions {
	showDevices: boolean;
	showOfflineDevices: boolean;
	layoutType: LayoutType;
	detailLevel: NodeDetailLevel;
}

const initialState: TopologyState = {
	eeros: [],
	devices: [],
	switches: [],
	switchWarning: null,
	discovery: null,
	loading: false,
	error: null
};

const selectedNodeId = writable<string | null>(null);

export const layoutOptionsStore = writable<LayoutOptions>({
	showDevices: true,
	showOfflineDevices: false,
	layoutType: 'hierarchy',
	detailLevel: 'minimal'
});

function createTopologyStore() {
	const { subscribe, set, update } = writable<TopologyState>(initialState);
	let request = 0;
	return {
		subscribe,
		async loadTopology(): Promise<void> {
			const currentRequest = ++request;
			update((s) => ({ ...s, loading: true, error: null }));
			try {
				const [eeros, devices, switchResult, discovery] = await Promise.all([
					api.eeros.list(true),
					api.devices.list({ refresh: true }),
					api.switches
						.list()
						.then((switches) => ({ switches, warning: null as string | null }))
						.catch(() => ({
							switches: [],
							warning: 'Switch data is unavailable. Eero connections are still shown.'
						})),
					loadDiscovery()
				]);
				const enriched: TopologyEero[] = [...eeros];
				if (switchResult.switches.length || discovery.status !== 'disabled') {
					// Physical Ethernet addresses can differ from the eero's base MAC.
					for (let i = 0; i < eeros.length; i += 4) {
						if (currentRequest !== request) return;
						await Promise.all(
							eeros.slice(i, i + 4).map(async (eero, offset) => {
								try {
									const detail = await api.eeros.get(eero.id);
									enriched[i + offset] = { ...eero, ethernet_addresses: detail.ethernet_addresses };
								} catch {
									/* Base MAC matching still works when details are unavailable. */
								}
							})
						);
					}
				}
				if (currentRequest !== request) return;
				update((s) => ({
					...s,
					eeros: enriched,
					devices: withDiscoveredDevices(
						withKnownSwitchDevices(devices, switchResult.switches),
						enriched,
						switchResult.switches,
						discovery
					),
					switches: switchResult.switches,
					switchWarning: switchResult.warning,
					discovery,
					loading: false
				}));
			} catch (error) {
				if (currentRequest !== request) return;
				update((s) => ({
					...s,
					loading: false,
					error: error instanceof Error ? error.message : 'Failed to load topology'
				}));
			}
		},
		async refreshDiscovery(): Promise<void> {
			const currentRequest = request;
			const discovery = await loadDiscovery();
			if (currentRequest !== request) return;
			update((s) => ({
				...s,
				discovery,
				devices: withDiscoveredDevices(s.devices, s.eeros, s.switches, discovery)
			}));
		},
		selectNode(nodeId: string | null): void {
			selectedNodeId.set(nodeId);
		},
		clear(): void {
			request++;
			selectedNodeId.set(null);
			set(initialState);
		}
	};
}

async function loadDiscovery(): Promise<DiscoveryResponse> {
	try {
		return await api.discovery.get();
	} catch {
		return {
			status: 'error',
			subnet: null,
			interval_seconds: 3600,
			last_scan: null,
			next_scan: null,
			warning: 'LAN discovery is unavailable. Eero and switch connections are still shown.',
			devices: []
		};
	}
}

export const topologyStore = createTopologyStore();

// Filter before laying out: hidden clients must not leave empty groups or large gaps.
export const filteredTopology = derived(
	[topologyStore, layoutOptionsStore],
	([$topology, $options]) => ({
		...transformToTopology(
			$topology.eeros,
			$options.showDevices
				? $topology.devices.filter((d) => $options.showOfflineDevices || d.connected)
				: [],
			$options.layoutType,
			$options.detailLevel,
			$topology.switches
		),
		loading: $topology.loading,
		error: $topology.error,
		switchWarning: $topology.switchWarning,
		discovery: $topology.discovery
	})
);

export const selectedNode = derived(
	[selectedNodeId, filteredTopology],
	([$id, $graph]) => $graph.nodes.find((n) => n.id === $id) ?? null
);
export const isTopologyLoading = derived(topologyStore, ($store) => $store.loading);

// These dimensions match the node components. Each group has a dedicated left-hand
// connection gutter, so branches never pass through another client's card.
const ROUTER_WIDTH = 180;
const GROUP_WIDTH = 208;
const GROUP_GAP = 24;
const GROUP_HEADER = 64;
const ROW_GAP = 18;
const CLUSTER_GAP = 80;
export const CLIENT_HEIGHT: Record<NodeDetailLevel, number> = {
	minimal: 74,
	standard: 104,
	detailed: 132
};

interface ClientGroup {
	connectionType: 'wired' | 'wireless' | 'unknown';
	devices: TopologyDevice[];
}
interface Cluster {
	eero?: EeroSummary;
	branch?: Branch;
	groups: ClientGroup[];
	width: number;
	height: number;
	x: number;
	y: number;
	groupY: number;
}

interface Branch {
	id: string;
	label: string;
	eero?: EeroSummary;
	switch?: SwitchSnapshot;
}

function deviceLabel(device: DeviceSummary): string {
	return (
		device.display_name || device.nickname || device.hostname || device.mac || 'Unknown Device'
	);
}

/** Prefer identity/location; a shared model name cannot identify a particular eero. */
function connectedEero(device: DeviceSummary, eeros: EeroSummary[]): EeroSummary | undefined {
	const name = device.connected_to_eero?.trim().toLowerCase();
	if (!name) return;
	const byId = eeros.find((e) => e.id.toLowerCase() === name);
	if (byId) return byId;
	const byLocation = eeros.filter((e) => e.location?.trim().toLowerCase() === name);
	if (byLocation.length === 1) return byLocation[0];
	const byModel = eeros.filter((e) => e.model.toLowerCase() === name);
	if (byLocation.length === 0 && byModel.length === 1) return byModel[0];
}

function getMeshQuality(bars: number | null | undefined): EdgeQuality {
	if (bars === null || bars === undefined) return 'fair';
	if (bars >= 4) return 'excellent';
	if (bars >= 3) return 'good';
	if (bars >= 2) return 'fair';
	return 'poor';
}

const QUALITY_COLOR: Record<EdgeQuality, string> = {
	excellent: 'success',
	good: 'accent',
	fair: 'warning',
	poor: 'danger'
};

function getQualityColor(quality: EdgeQuality): string {
	return `var(--color-${QUALITY_COLOR[quality]})`;
}

export function transformToTopology(
	eeros: TopologyEero[],
	devices: TopologyDevice[],
	layoutType: LayoutType = 'hierarchy',
	detailLevel: NodeDetailLevel = 'minimal',
	switches: SwitchSnapshot[] = []
): { nodes: Node<TopologyNodeData>[]; edges: Edge<TopologyEdgeData>[] } {
	const nodes: Node<TopologyNodeData>[] = [];
	const edges: Edge<TopologyEdgeData>[] = [];
	const gateway = eeros.find((e) => e.is_gateway);
	const switchLinks = resolveSwitchLinks(eeros, devices, switches);
	const branchList: Branch[] = [
		...eeros.map((eero) => ({ id: `eero-${eero.id}`, label: eero.location || eero.model, eero })),
		...switches.map((sw) => ({ id: `switch-${sw.id}`, label: sw.name, switch: sw }))
	];
	const branches = new Map(branchList.map((b) => [b.id, b]));
	const switchMacs = new Set(switches.map((sw) => normalizeMac(sw.mac)).filter(Boolean));
	const orderedEeros = [...eeros].sort(
		(a, b) =>
			Number(b.is_gateway) - Number(a.is_gateway) ||
			(a.location || a.model).localeCompare(b.location || b.model) ||
			a.id.localeCompare(b.id)
	);
	const byEero = new Map<string | undefined, TopologyDevice[]>();
	for (const device of devices) {
		if (switchMacs.has(normalizeMac(device.mac))) continue;
		const eero = connectedEero(device, orderedEeros);
		const id =
			device.discovery_source === 'arp'
				? gateway
					? `eero-${gateway.id}`
					: undefined
				: (switchLinks.get(`device-${device.mac || device.id}`)?.parent ??
					(eero ? `eero-${eero.id}` : undefined));
		byEero.set(id, [...(byEero.get(id) ?? []), device]);
	}
	const clientHeight = CLIENT_HEIGHT[detailLevel];
	const groupHeight = (count: number) =>
		GROUP_HEADER + count * (clientHeight + ROW_GAP) - ROW_GAP + 16;
	const sortedBranches = [
		...orderedEeros.map((e) => branches.get(`eero-${e.id}`)!),
		...branchList
			.filter((b) => b.switch)
			.sort((a, b) => a.label.localeCompare(b.label) || a.id.localeCompare(b.id))
	];
	// Keep each connected subtree contiguous. Alphabetical columns with the
	// gateway's clients in the middle put their long stem across switch links.
	const children = new Map<string | undefined, Branch[]>();
	for (const branch of sortedBranches) {
		const parent =
			switchLinks.get(branch.id)?.parent ??
			(branch.eero && branch.eero !== gateway && gateway ? `eero-${gateway.id}` : undefined);
		children.set(parent, [...(children.get(parent) ?? []), branch]);
	}
	const orderedBranches: Branch[] = [];
	const visited = new Set<string>();
	const visit = (branch: Branch) => {
		if (visited.has(branch.id)) return;
		visited.add(branch.id);
		orderedBranches.push(branch);
		for (const child of children.get(branch.id) ?? []) visit(child);
	};
	for (const root of children.get(undefined) ?? []) visit(root);
	for (const branch of sortedBranches) visit(branch);
	const clusters: Cluster[] = [
		...orderedBranches,
		...(byEero.has(undefined) ? [undefined] : [])
	].map((branch) => {
		const eero = branch?.eero;
		const clients = byEero.get(branch?.id) ?? [];
		const groups: ClientGroup[] = (['wired', 'wireless', 'unknown'] as const).flatMap(
			(connectionType) => {
				const groupDevices = clients
					.filter(
						(d) =>
							(d.discovery_source === 'arp' ? 'unknown' : d.wireless ? 'wireless' : 'wired') ===
							connectionType
					)
					.sort(
						(a, b) =>
							Number(b.connected) - Number(a.connected) ||
							deviceLabel(a).localeCompare(deviceLabel(b))
					);
				return groupDevices.length ? [{ connectionType, devices: groupDevices }] : [];
			}
		);
		return {
			eero,
			branch,
			groups,
			width: Math.max(ROUTER_WIDTH, groups.length * (GROUP_WIDTH + GROUP_GAP) - GROUP_GAP),
			height: 200 + Math.max(0, ...groups.map((g) => groupHeight(g.devices.length))),
			x: 0,
			y: 0,
			groupY: 200
		};
	});
	const gatewayCluster = clusters.find((c) => c.eero === gateway && gateway !== undefined);
	const leaves = clusters.filter((c) => c !== gatewayCluster);
	const depth = (id: string, seen = new Set<string>()): number => {
		if (seen.has(id) || id === `eero-${gateway?.id}`) return 0;
		seen.add(id);
		const parent = switchLinks.get(id)?.parent;
		return parent ? depth(parent, seen) + 1 : 1;
	};
	const maxDepth = Math.max(1, ...branchList.map((b) => depth(b.id)));

	if (layoutType === 'hierarchy' || layoutType === 'horizontal') {
		const eeroY = detailLevel === 'minimal' ? 160 : 220;
		const clientsY = (detailLevel === 'minimal' ? 280 : 420) + (maxDepth - 1) * eeroY;
		// Put a parent's own clients before its descendants, outside the span of
		// their links. Apply the same ordering to columns and horizontal rows.
		const columns = clusters.filter((c) => c !== gatewayCluster || c.groups.length);
		let cursor = 0;
		for (const cluster of columns) {
			if (layoutType === 'horizontal') {
				cluster.x =
					cluster === gatewayCluster
						? 0
						: 300 * (cluster.branch ? depth(cluster.branch.id) : maxDepth);
				cluster.y = cursor;
				cluster.groupY = 0;
				cursor += Math.max(160, cluster.height - 200) + CLUSTER_GAP;
			} else {
				cluster.x = cursor + (cluster.width - ROUTER_WIDTH) / 2;
				cluster.y =
					cluster === gatewayCluster
						? 0
						: eeroY * (cluster.branch ? depth(cluster.branch.id) : maxDepth);
				cluster.groupY = clientsY - cluster.y;
				cursor += cluster.width + CLUSTER_GAP;
			}
		}
		if (gatewayCluster && !gatewayCluster.groups.length) {
			gatewayCluster.x =
				layoutType === 'horizontal' ? 0 : Math.max(0, (cursor - CLUSTER_GAP - ROUTER_WIDTH) / 2);
			gatewayCluster.y =
				layoutType === 'horizontal' ? Math.max(0, (cursor - CLUSTER_GAP) / 2 - 40) : 0;
		}
	} else {
		// Size the ring from whole client clusters, rather than just router cards.
		const diameter =
			Math.max(200, ...clusters.map((c) => Math.hypot(c.width, c.height))) + CLUSTER_GAP;
		const radius = Math.max(
			diameter,
			diameter / (2 * Math.sin(Math.PI / Math.max(leaves.length, 2)))
		);
		leaves.forEach((cluster, index) => {
			const angle =
				(index * 2 * Math.PI) / leaves.length - Math.PI / 2 + (layoutType === 'force' ? 0.2 : 0);
			const distance = radius + (layoutType === 'force' ? (index % 2) * 60 : 0);
			cluster.x = Math.cos(angle) * distance;
			cluster.y = Math.sin(angle) * distance;
		});
	}

	for (const cluster of clusters) {
		const { eero, groups, branch } = cluster;
		const eeroId = branch?.id;
		const eeroLabel = branch?.label ?? 'Unassigned clients';
		const sw = branch?.switch;
		if (sw && eeroId)
			nodes.push({
				id: eeroId,
				type: 'switch',
				position: { x: cluster.x, y: cluster.y },
				data: {
					type: 'switch',
					id: sw.id,
					label: sw.name,
					status: sw.status,
					unmanaged: sw.kind === 'unmanaged',
					model: sw.model ?? undefined,
					ipAddress: sw.host,
					mac: sw.mac ?? undefined,
					ports: sw.kind === 'unmanaged' ? undefined : sw.ports,
					observedAt: sw.observed_at ?? undefined,
					warnings: sw.warnings,
					detailLevel,
					layoutType
				}
			});
		if (eero && eeroId) {
			nodes.push({
				id: eeroId,
				type: eero.is_gateway ? 'gateway' : 'eero',
				position: { x: cluster.x, y: cluster.y },
				zIndex: 0,
				data: {
					type: eero.is_gateway ? 'gateway' : 'eero',
					id: eero.id,
					label: eeroLabel,
					status: eero.status === 'green' ? 'online' : 'offline',
					meshQuality: eero.is_gateway ? 5 : (eero.mesh_quality_bars ?? 0),
					deviceCount: eero.connected_clients_count,
					model: eero.model,
					isGateway: eero.is_gateway,
					wired: eero.wired,
					ipAddress: eero.ip_address || undefined,
					firmwareVersion: eero.firmware_version || undefined,
					detailLevel,
					layoutType
				}
			});
			if (gateway && eero !== gateway && !switchLinks.has(eeroId)) {
				const quality = getMeshQuality(eero.mesh_quality_bars);
				edges.push({
					id: `mesh-${gateway.id}-${eero.id}`,
					source: `eero-${gateway.id}`,
					target: eeroId,
					type: 'smoothstep',
					zIndex: 0,
					style: `stroke: ${getQualityColor(quality)}; stroke-width: 2px;`,
					data: { quality, type: 'mesh' }
				});
			}
		}
		const infrastructureLink = eeroId ? switchLinks.get(eeroId) : undefined;
		if (infrastructureLink && eeroId) {
			const node = nodes.find((n) => n.id === eeroId)!;
			const knownSourcePort = branches.get(infrastructureLink.parent)?.switch?.kind !== 'unmanaged';
			node.data.portName = knownSourcePort ? infrastructureLink.port.name : undefined;
			node.data.evidence = infrastructureLink.evidence;
			const speed = knownSourcePort ? infrastructureLink.port.speed_mbps : null;
			edges.push({
				id: `switch-link-${eeroId}`,
				source: infrastructureLink.parent,
				target: eeroId,
				type: 'smoothstep',
				label: `${knownSourcePort ? `${infrastructureLink.port.name}${speed ? ` · ${speed >= 1000 ? `${speed / 1000} Gbps` : `${speed} Mbps`}` : ''} · ` : ''}${infrastructureLink.evidence === 'manual' ? 'User supplied' : infrastructureLink.evidence === 'lldp' ? 'LLDP' : 'Inferred'}`,
				style: `stroke: var(--color-${infrastructureLink.evidence === 'manual' ? 'text-muted' : 'success'}); stroke-width: 2px;${infrastructureLink.evidence !== 'lldp' ? ' stroke-dasharray: 5 4;' : ''}`,
				data: { quality: 'good', type: 'switch', connectionType: 'wired' }
			});
		}
		groups.forEach((group, groupIndex) => {
			const groupX =
				layoutType === 'horizontal'
					? Math.max(260, 300 * maxDepth + 260 - cluster.x) + groupIndex * (GROUP_WIDTH + GROUP_GAP)
					: (ROUTER_WIDTH - cluster.width) / 2 + groupIndex * (GROUP_WIDTH + GROUP_GAP);
			const origin = branch ? { x: 0, y: 0 } : { x: cluster.x, y: cluster.y };
			const groupId = `clients-${eero?.id ?? branch?.id ?? 'unassigned'}-${group.connectionType}`;
			nodes.push({
				id: groupId,
				type: 'clientGroup',
				parentId: eeroId,
				position: { x: origin.x + groupX, y: origin.y + cluster.groupY },
				width: GROUP_WIDTH,
				height: groupHeight(group.devices.length),
				zIndex: -1,
				selectable: false,
				draggable: false,
				focusable: false,
				data: {
					type: 'clientGroup',
					id: groupId,
					label:
						group.connectionType === 'unknown'
							? 'ARP discovered'
							: sw
								? sw.kind === 'unmanaged'
									? 'Known wired clients'
									: 'Wired paths via switch'
								: group.connectionType === 'wired'
									? 'Wired clients'
									: 'Wireless clients',
					status: 'online',
					connectionType: group.connectionType,
					eeroLabel: group.connectionType === 'unknown' ? 'Physical connection unknown' : eeroLabel,
					deviceCount: group.devices.length
				}
			});
			group.devices.forEach((device, index) => {
				const deviceId = device.mac || device.id || `${groupId}-${index}`;
				const switchLink = switchLinks.get(`device-${deviceId}`);
				nodes.push({
					id: `device-${deviceId}`,
					type: 'device',
					parentId: eeroId,
					position: {
						x: origin.x + groupX + 32,
						y: origin.y + cluster.groupY + GROUP_HEADER + index * (clientHeight + ROW_GAP)
					},
					draggable: false,
					zIndex: 2,
					data: {
						type: 'device',
						id: deviceId,
						label: deviceLabel(device),
						status: device.connected ? 'online' : device.topology_only ? 'unknown' : 'offline',
						observedAt: device.last_seen,
						topologyOnly: device.topology_only,
						warnings:
							device.discovery_source === 'arp'
								? [
										'Not reported by eero. Responded to a LAN ARP scan; the dotted gateway link is a placeholder, not confirmed wiring. Status reflects the last scan.'
									]
								: device.topology_only
									? [
											'Not reported by eero. Activity is inferred from MAC learning on the upstream port.'
										]
									: undefined,
						signal: device.signal_strength ?? undefined,
						connectionType: group.connectionType,
						eeroLabel:
							device.discovery_source === 'arp'
								? 'Physical connection unknown'
								: branch
									? eeroLabel
									: 'Unknown eero',
						portName: switchLink?.evidence === 'manual' ? undefined : switchLink?.port.name,
						evidence: device.discovery_source === 'arp' ? 'arp' : switchLink?.evidence,
						ip: device.ip || undefined,
						mac: device.mac || undefined,
						manufacturer: device.manufacturer || undefined,
						isBlocked: device.blocked,
						isPaused: device.paused,
						profileName: device.profile_name || undefined,
						detailLevel,
						layoutType
					}
				});
				if (!eeroId) return;
				const color =
					device.discovery_source === 'arp' || switchLink?.evidence === 'manual'
						? 'text-muted'
						: group.connectionType === 'wired'
							? 'success'
							: 'accent';
				edges.push({
					id: `client-${eero?.id ?? eeroId}-${deviceId}`,
					source: eeroId,
					target: `device-${deviceId}`,
					type: 'client',
					zIndex: 1,
					style: `stroke: var(--color-${device.connected ? color : 'text-muted'}); stroke-width: 1.5px;${device.discovery_source === 'arp' ? ' stroke-dasharray: 1 5; stroke-linecap: round;' : group.connectionType === 'wireless' || !device.connected || switchLink?.evidence === 'mac' || switchLink?.evidence === 'manual' ? ' stroke-dasharray: 5 4;' : ''}`,
					data: {
						quality: 'good',
						type: 'client',
						connectionType: group.connectionType,
						branchOffset: GROUP_HEADER + index * (clientHeight + ROW_GAP) + clientHeight / 2 + 24,
						previousRowOffset: index > 0 ? clientHeight + ROW_GAP : undefined,
						horizontal: layoutType === 'horizontal'
					}
				});
			});
		});
	}
	return { nodes, edges };
}
