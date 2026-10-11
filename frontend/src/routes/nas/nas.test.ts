import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';
import { selectedNetwork } from '$stores';
import { api } from '$api/client';
import type { NASSnapshot } from '$api/types';
import NASPage from './+page.svelte';
import { clearNASCache, NAS_REFRESH_MS } from '$stores/nasCache';

vi.mock('$stores', async () => {
	const { writable } = await import('svelte/store');
	return { selectedNetwork: writable<{ id: string } | null>(null) };
});

const network = selectedNetwork as Writable<{ id: string } | null>;
const snapshot: NASSnapshot = {
	id: 'qnap',
	name: 'QNAP TVS-473e',
	host: '192.168.4.2',
	status: 'online',
	model: 'TVS-473e',
	observed_at: '2026-10-10T12:00:00Z',
	warnings: [],
	pools: [],
	raids: [],
	disks: [
		{
			id: '1',
			name: 'Disk 1',
			enclosure_id: 0,
			bay: 1,
			model: 'WD Red',
			status: 'Good',
			smart_status: 'Warning',
			health: 'warning',
			temperature_c: 37,
			capacity_bytes: 4_000_000_000_000,
			power_on_days: 1234,
			ssd_life_remaining_percent: 98
		},
		{
			id: '2',
			name: 'Disk 2',
			enclosure_id: 0,
			bay: 2,
			model: null,
			status: 'Good',
			smart_status: null,
			health: 'unknown',
			temperature_c: null,
			capacity_bytes: null,
			power_on_days: null,
			ssd_life_remaining_percent: null
		}
	],
	volumes: [
		{
			id: '1',
			name: 'Data',
			status: 'Ready',
			health: 'good',
			raid_level: null,
			capacity_bytes: 1000,
			used_bytes: 1000,
			free_bytes: 0,
			used_percent: 100
		}
	]
};

beforeEach(() => {
	clearNASCache();
	network.set({ id: 'network-123' });
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	vi.useRealTimers();
	network.set(null);
});

describe('NAS status', () => {
	it('shows warnings, unknown SMART and a completely full volume without inventing readings', async () => {
		const list = vi.spyOn(api.nas, 'list').mockResolvedValue([snapshot]);
		render(NASPage);
		await screen.findByText('Disk 1');
		expect(list).toHaveBeenCalledWith('network-123', false);
		expect(screen.getByText('1 Warning')).toBeInTheDocument();
		expect(screen.getByText('1 Unknown')).toBeInTheDocument();
		expect(screen.getByText('0 Healthy')).toBeInTheDocument();
		expect(screen.getByText('37 °C')).toBeInTheDocument();
		expect(screen.getByText('100.0% used')).toBeInTheDocument();
		expect(screen.getByLabelText('Data used capacity')).toHaveAttribute('value', '100');
		expect(screen.getByText('0 B')).toBeInTheDocument();
		expect(screen.getByText('1,234 days')).toBeInTheDocument();
		expect(screen.getByText('98%')).toBeInTheDocument();
		expect(screen.getAllByText('Unavailable')).toHaveLength(2);
	});

	it('removes previous health when a refresh reports an unreachable NAS', async () => {
		vi.spyOn(api.nas, 'list')
			.mockResolvedValueOnce([snapshot])
			.mockResolvedValueOnce([
				{
					...snapshot,
					status: 'unreachable',
					observed_at: null,
					disks: [],
					volumes: [],
					warnings: ['Could not read NAS data.']
				}
			]);
		render(NASPage);
		await screen.findByText('Disk 1');
		await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		await screen.findByText('Unreachable');
		expect(screen.queryByText('Disk 1')).not.toBeInTheDocument();
		expect(screen.queryByText('100.0% used')).not.toBeInTheDocument();
		expect(screen.getByText('No current reading')).toBeInTheDocument();
	});

	it('keeps cached readings labelled stale and permits retry after an API error', async () => {
		vi.spyOn(api.nas, 'list')
			.mockResolvedValueOnce([snapshot])
			.mockRejectedValueOnce(new Error('NAS configuration unavailable.'))
			.mockResolvedValueOnce([snapshot]);
		render(NASPage);
		await screen.findByText('Disk 1');
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		await screen.findByRole('alert');
		expect(screen.getByText('Disk 1')).toBeInTheDocument();
		expect(screen.getByText('Cached reading — awaiting refresh')).toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		await screen.findByText('Disk 1');
		expect(screen.queryByRole('alert')).not.toBeInTheDocument();
	});

	it('discards a delayed response from the previously selected network', async () => {
		let resolveOld!: (value: NASSnapshot[]) => void;
		const oldRequest = new Promise<NASSnapshot[]>((resolve) => {
			resolveOld = resolve;
		});
		const list = vi
			.spyOn(api.nas, 'list')
			.mockReturnValueOnce(oldRequest)
			.mockResolvedValueOnce([]);
		render(NASPage);
		await waitFor(() => expect(list).toHaveBeenCalledWith('network-123', false));
		network.set({ id: 'network-456' });
		await screen.findByText('Connect your QNAP');
		resolveOld([snapshot]);
		await waitFor(() => expect(list).toHaveBeenCalledWith('network-456', false));
		expect(screen.queryByText('Disk 1')).not.toBeInTheDocument();
		expect(screen.getByText('network-456')).toBeInTheDocument();
	});

	it('shows cached data immediately when returning to the page during a delayed refresh', async () => {
		const list = vi.spyOn(api.nas, 'list').mockResolvedValueOnce([snapshot]);
		const view = render(NASPage);
		await screen.findByText('Disk 1');
		view.unmount();
		list.mockImplementation(() => new Promise(() => {}));
		render(NASPage);
		await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
		expect(screen.getByText('Disk 1')).toBeInTheDocument();
		expect(screen.getByText('Reading NAS status…')).toBeInTheDocument();
		network.set({ id: 'network-456' });
		await waitFor(() => expect(list).toHaveBeenCalledWith('network-456', false));
		expect(screen.queryByText('Disk 1')).not.toBeInTheDocument();
	});

	it('refreshes every five minutes and forces a new reading for manual refresh', async () => {
		vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
		const list = vi.spyOn(api.nas, 'list').mockResolvedValue([snapshot]);
		render(NASPage);
		await screen.findByText('Disk 1');
		await vi.advanceTimersByTimeAsync(NAS_REFRESH_MS - 1);
		expect(list).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(1);
		await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
		await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		expect(list).toHaveBeenLastCalledWith('network-123', true);
	});

	it('accepts zero days and zero remaining life as real readings', async () => {
		vi.spyOn(api.nas, 'list').mockResolvedValue([
			{
				...snapshot,
				disks: [
					{
						...snapshot.disks[0],
						power_on_days: 0,
						ssd_life_remaining_percent: 0
					}
				]
			}
		]);
		render(NASPage);
		await screen.findByText('0 days');
		expect(screen.getByText('0%')).toBeInTheDocument();
	});
});
