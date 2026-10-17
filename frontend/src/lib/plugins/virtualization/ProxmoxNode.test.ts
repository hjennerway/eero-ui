import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import type { VirtualizationNode } from './types';
import ProxmoxNode from './ProxmoxNode.svelte';

const node: VirtualizationNode = {
	id: 'pve-1',
	name: 'pve-1',
	version: '9.1.1',
	status: 'online',
	issues: [],
	guests: [
		{
			id: 'lxc:101',
			vmid: 101,
			kind: 'lxc',
			name: 'services',
			status: 'running',
			template: false,
			os_name: 'Debian 12',
			os_version: '12',
			os_source: 'docker',
			ips: ['192.168.4.101'],
			macs: [],
			eero_device_ids: ['dev-1'],
			match_status: 'matched',
			issues: [],
			docker: {
				status: 'available',
				engine_id: 'engine-1',
				version: '26.0.1',
				collected_at: null,
				message: null,
				containers: [
					{ id: '1', name: 'web', image: 'nginx', state: 'running', health: null, project: null },
					{
						id: '2',
						name: 'old-service',
						image: 'alpine',
						state: 'exited',
						health: null,
						project: null
					}
				]
			}
		}
	]
};

describe('virtualization node', () => {
	it('renders nested node, guest, Docker containers and eero link', () => {
		render(ProxmoxNode, { data: { node, showStopped: true } });
		expect(screen.getByRole('region', { name: 'Proxmox node pve-1' })).toBeInTheDocument();
		expect(screen.getByRole('article', { name: 'LXC services' })).toContainElement(
			screen.getByText('Docker')
		);
		expect(screen.getByText('old-service')).toBeInTheDocument();
		expect(screen.getByText('Stopped')).toBeInTheDocument();
		expect(screen.getByRole('link', { name: /eero device/ })).toHaveAttribute(
			'href',
			'/devices/dev-1'
		);
	});
	it('filters stopped Docker containers and collapses the node', async () => {
		render(ProxmoxNode, { data: { node, showStopped: false } });
		expect(screen.queryByText('old-service')).not.toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Collapse pve-1' }));
		expect(screen.queryByText('Docker')).not.toBeInTheDocument();
		expect(screen.getByText('1 guest')).toBeInTheDocument();
	});
	it('labels saved container states when Docker is unavailable', () => {
		const stale = structuredClone(node);
		stale.guests[0].docker.status = 'stale';
		stale.guests[0].docker.message = 'Showing last reported Docker states.';
		render(ProxmoxNode, { data: { node: stale, showStopped: true } });
		expect(screen.getByText('Showing last reported Docker states.')).toBeInTheDocument();
		expect(screen.getByText('web')).toBeInTheDocument();
	});
});
