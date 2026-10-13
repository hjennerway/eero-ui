import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import Page from './+page.svelte';

const calls = vi.hoisted(() => ({
	get: vi.fn(),
	connection: vi.fn(),
	history: vi.fn(),
	saveConnection: vi.fn(),
	control: vi.fn()
}));
vi.mock('$api/client', () => ({ api: { vpn: calls } }));

const snapshot = {
	host: '192.168.4.10',
	interface: 'tun0',
	control_available: true,
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
	control_mode: 'openvpn',
	control_target: 'nordvpn',
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
		calls.history.mockReset().mockResolvedValue({
			download_mbps: [],
			upload_mbps: [],
			connected_clients: [],
			connection_status: []
		});
		calls.saveConnection.mockReset().mockResolvedValue(connection);
		calls.control.mockReset().mockResolvedValue(snapshot);
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
			control_mode: 'openvpn',
			control_target: 'nordvpn',
			password: 'fixture-password'
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		expect(screen.getByLabelText('SSH password')).toHaveValue('');
	});
	it('requests disconnect and displays the refreshed tunnel status', async () => {
		render(Page);
		await flush();
		const disconnected = {
			...snapshot,
			status: 'disconnected',
			connected_clients: 0,
			download_mbps: 0,
			upload_mbps: 0
		};
		calls.control.mockResolvedValueOnce(disconnected);
		calls.get.mockResolvedValue(disconnected);
		await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
		await flush();
		expect(calls.control).toHaveBeenCalledWith('disconnect');
		expect(screen.getByText('Disconnected')).toBeInTheDocument();
		expect(
			screen.getByText('Disconnect requested. Status updates every 30 seconds.')
		).toBeInTheDocument();
	});
	it('keeps Connect pending and blocks duplicate actions without claiming connected', async () => {
		calls.get.mockResolvedValue({ ...snapshot, status: 'disconnected' });
		let finish!: (value: typeof snapshot) => void;
		calls.control.mockReturnValueOnce(
			new Promise((resolve) => {
				finish = resolve;
			})
		);
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
		await flush();
		expect(screen.getByRole('button', { name: 'Connect' })).toBeDisabled();
		expect(screen.getByRole('button', { name: 'Disconnect' })).toBeDisabled();
		expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled();
		expect(screen.getByText('Disconnected')).toBeInTheDocument();
		await vi.advanceTimersByTimeAsync(30000);
		expect(calls.get).toHaveBeenCalledTimes(1);
		finish(snapshot);
		await flush();
		expect(calls.control).toHaveBeenCalledTimes(1);
	});
	it('reports a failed action without showing a success message', async () => {
		calls.control.mockRejectedValueOnce(new Error('SSH host key changed.'));
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
		await flush();
		expect(screen.getByText('SSH host key changed.')).toBeInTheDocument();
		expect(screen.queryByText(/Disconnect requested/)).not.toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Disconnect' })).toBeEnabled();
	});
	it('requires a saved tunnel name for controls while preserving monitoring', async () => {
		calls.connection.mockResolvedValue({ ...connection, control_target: '' });
		render(Page);
		await flush();
		expect(screen.getByText('12.50')).toBeInTheDocument();
		expect(screen.getByRole('button', { name: 'Connect' })).toBeDisabled();
		expect(screen.getByRole('button', { name: 'Disconnect' })).toBeDisabled();
	});
	it('saves the selected network tunnel controls', async () => {
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		await fireEvent.change(screen.getByLabelText('Tunnel type'), { target: { value: 'network' } });
		await fireEvent.input(screen.getByLabelText('Tunnel name'), { target: { value: 'wg_vpn' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Test and save' }));
		await flush();
		expect(calls.saveConnection).toHaveBeenCalledWith(
			expect.objectContaining({ control_mode: 'network', control_target: 'wg_vpn', password: null })
		);
	});
});
