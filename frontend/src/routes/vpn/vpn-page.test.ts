import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import Page from './+page.svelte';

const calls = vi.hoisted(() => ({
	get: vi.fn(),
	connection: vi.fn(),
	history: vi.fn(),
	saveConnection: vi.fn()
}));
vi.mock('$api/client', () => ({ api: { vpn: calls } }));

const snapshot = {
	host: '192.168.4.10',
	interface: 'tun0',
	provider: 'NordVPN',
	status: 'connected',
	reachable: true,
	connected_clients: 3,
	download_mbps: 12.5,
	upload_mbps: 2.25,
	collected_at: '2026-10-13T12:00:00Z',
	stale: false,
	error: null,
	warnings: [],
	history_error: null,
	interval_seconds: 30
};
const connection = {
	enabled: true,
	host: '192.168.4.10',
	port: 22,
	username: 'root',
	interface: 'tun0',
	password_set: true,
	host_key: 'SHA256:fixture'
};
const descriptor = Object.getOwnPropertyDescriptor(document, 'visibilityState');
function visible(value: DocumentVisibilityState) {
	Object.defineProperty(document, 'visibilityState', { configurable: true, value });
	document.dispatchEvent(new Event('visibilitychange'));
}
async function flush() {
	for (let i = 0; i < 10; i++) await tick();
}

describe('VPN page', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		visible('visible');
		calls.get.mockReset().mockResolvedValue(snapshot);
		calls.connection.mockReset().mockResolvedValue(connection);
		calls.history
			.mockReset()
			.mockResolvedValue({
				download_mbps: [],
				upload_mbps: [],
				connected_clients: [],
				connection_status: []
			});
		calls.saveConnection.mockReset().mockResolvedValue(connection);
	});
	afterEach(() => {
		cleanup();
		vi.useRealTimers();
		if (descriptor) Object.defineProperty(document, 'visibilityState', descriptor);
		else Reflect.deleteProperty(document, 'visibilityState');
	});
	it('shows measurements and refreshes every 30 seconds with cleanup', async () => {
		const { unmount } = render(Page);
		await flush();
		expect(screen.getByText('12.50')).toBeInTheDocument();
		expect(screen.getByText('2.25')).toBeInTheDocument();
		expect(screen.getByText('3')).toBeInTheDocument();
		await vi.advanceTimersByTimeAsync(30000);
		await flush();
		expect(calls.get).toHaveBeenCalledTimes(2);
		visible('hidden');
		await vi.advanceTimersByTimeAsync(60000);
		expect(calls.get).toHaveBeenCalledTimes(2);
		visible('visible');
		await flush();
		expect(calls.get).toHaveBeenCalledTimes(3);
		unmount();
		await vi.advanceTimersByTimeAsync(60000);
		expect(calls.get).toHaveBeenCalledTimes(3);
	});
	it('hides old measurements when a refresh fails', async () => {
		render(Page);
		await flush();
		calls.get.mockRejectedValueOnce(new Error('OpenWrt unavailable'));
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		await flush();
		expect(screen.queryByText('12.50')).not.toBeInTheDocument();
		expect(screen.getByText('Unknown')).toBeInTheDocument();
		expect(screen.getByText('OpenWrt unavailable')).toBeInTheDocument();
	});
	it('saves the password through the connection form and clears the field', async () => {
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		const password = screen.getByLabelText('SSH password');
		expect(password).toHaveValue('');
		await fireEvent.input(password, { target: { value: 'fixture-password' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Test and save' }));
		await flush();
		expect(calls.saveConnection).toHaveBeenCalledWith({
			enabled: true,
			host: '192.168.4.10',
			port: 22,
			username: 'root',
			interface: 'tun0',
			password: 'fixture-password'
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		expect(screen.getByLabelText('SSH password')).toHaveValue('');
	});
});
