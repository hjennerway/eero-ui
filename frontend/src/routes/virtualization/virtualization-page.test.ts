import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { selectedNetworkId } from '$stores';
import type { Writable } from 'svelte/store';
import Page from './+page.svelte';

const calls = vi.hoisted(() => ({
	get: vi.fn(),
	connection: vi.fn(),
	refresh: vi.fn(),
	saveConnection: vi.fn()
}));
vi.mock('$api/client', () => ({ api: { virtualization: calls } }));
vi.mock('$stores', async () => {
	const { writable } = await import('svelte/store');
	return { selectedNetworkId: writable<string | null>('net-1') };
});
const inventory = {
	configured: true,
	refreshing: false,
	collected_at: '2026-10-13T12:00:00Z',
	attempted_at: null,
	stale: false,
	error: null,
	nodes: [],
	background_interval_seconds: 600,
	visible_interval_seconds: 60
};
const settings = {
	configured: true,
	url: 'https://pve.test:8006',
	token_id: 'monitor@pve!inventory',
	has_token: true,
	has_ca_certificate: true,
	docker_enabled: true,
	docker_port: 2376,
	has_docker_certificates: true
};
const descriptor = Object.getOwnPropertyDescriptor(document, 'visibilityState');
// The module mock above supplies a writable in place of the real derived store.
const testNetworkId = selectedNetworkId as Writable<string | null>;
function visible(state: DocumentVisibilityState) {
	Object.defineProperty(document, 'visibilityState', { configurable: true, value: state });
	document.dispatchEvent(new Event('visibilitychange'));
}
async function flush() {
	for (let i = 0; i < 10; i++) await tick();
}

describe('virtualization page refresh controls', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		testNetworkId.set('net-1');
		visible('visible');
		calls.get.mockReset().mockResolvedValue(inventory);
		calls.connection.mockReset().mockResolvedValue(settings);
		calls.refresh.mockReset().mockResolvedValue(inventory);
	});
	afterEach(() => {
		cleanup();
		vi.useRealTimers();
		if (descriptor) Object.defineProperty(document, 'visibilityState', descriptor);
		else Reflect.deleteProperty(document, 'visibilityState');
	});
	it('wires the 60-second timer, hidden-tab pause, manual force, and unmount cleanup', async () => {
		const { unmount } = render(Page);
		await flush();
		expect(calls.refresh).toHaveBeenLastCalledWith('net-1', false);
		await vi.advanceTimersByTimeAsync(60000);
		await flush();
		expect(calls.refresh).toHaveBeenCalledTimes(2);
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		await flush();
		expect(calls.refresh).toHaveBeenLastCalledWith('net-1', true);
		visible('hidden');
		await vi.advanceTimersByTimeAsync(600000);
		expect(calls.refresh).toHaveBeenCalledTimes(3);
		await unmount();
		visible('visible');
		await vi.advanceTimersByTimeAsync(60000);
		expect(calls.refresh).toHaveBeenCalledTimes(3);
	});
	it('ignores an old networks delayed response after switching networks', async () => {
		let finishOld!: (value: typeof inventory) => void;
		calls.refresh.mockImplementationOnce(
			() =>
				new Promise((resolve) => {
					finishOld = resolve;
				})
		);
		render(Page);
		await flush();
		testNetworkId.set('net-2');
		await flush();
		expect(calls.refresh).toHaveBeenLastCalledWith('net-2', false);
		finishOld({ ...inventory, collected_at: '2030-01-01T12:00:00Z' });
		await flush();
		expect(screen.queryByText(/2030/)).not.toBeInTheDocument();
	});
});
