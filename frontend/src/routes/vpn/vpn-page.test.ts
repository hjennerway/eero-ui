import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Writable } from 'svelte/store';
import { selectedNetworkId } from '#lib/stores/networks.js';
import Page from './+page.svelte';

const calls = vi.hoisted(() => ({
	get: vi.fn(),
	connection: vi.fn(),
	history: vi.fn(),
	saveConnection: vi.fn(),
	control: vi.fn(),
	tunnels: vi.fn(),
	devices: vi.fn()
}));
vi.mock('$api/client', () => ({ api: { vpn: calls, devices: { list: calls.devices } } }));
vi.mock('#lib/stores/networks.js', async () => {
	const { writable } = await import('svelte/store');
	return { selectedNetworkId: writable<string | null>('net-1') };
});
const testNetworkId = selectedNetworkId as Writable<string | null>;
const devices = [
	{ id: 'device-9', ip: '192.168.4.9', display_name: 'Laptop', connected: true },
	{ id: null, ip: '192.168.4.20', connected: true }
];

const snapshot = {
	host: '192.168.4.10',
	interface: 'tun0',
	control_available: true,
	provider: 'NordVPN',
	status: 'connected',
	reachable: true,
	connected_clients: 3,
	connected_client_ips: ['192.168.4.9', '192.168.4.20', '192.168.4.100'],
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
		testNetworkId.set('net-1');
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
		calls.devices.mockReset().mockResolvedValue(devices);
		calls.tunnels.mockReset().mockResolvedValue([
			{ name: 'nordvpn', control_mode: 'openvpn' },
			{ name: 'wg_vpn', control_mode: 'network' }
		]);
	});
	it('links matched IPs to their device page and leaves unknown or unidentified IPs as text', async () => {
		render(Page);
		await flush();
		expect(screen.getByRole('link', { name: '192.168.4.9' })).toHaveAttribute(
			'href',
			'/devices/device-9'
		);
		expect(screen.queryByRole('link', { name: '192.168.4.20' })).not.toBeInTheDocument();
		expect(screen.queryByRole('link', { name: '192.168.4.100' })).not.toBeInTheDocument();
		expect(screen.getByText('192.168.4.20')).toBeInTheDocument();
		expect(screen.getByText('192.168.4.100')).toBeInTheDocument();
		expect(calls.devices).toHaveBeenCalledWith({ connectedOnly: true, networkId: 'net-1' });
	});
	it('matches equivalent IPv6 addresses', async () => {
		calls.get.mockResolvedValue({ ...snapshot, connected_client_ips: ['fd00:4::2'] });
		calls.devices.mockResolvedValue([
			{ id: 'device-v6', ip: 'FD00:4:0:0:0:0:0:2', connected: true }
		]);
		render(Page);
		await flush();
		expect(screen.getByRole('link', { name: 'fd00:4::2' })).toHaveAttribute(
			'href',
			'/devices/device-v6'
		);
	});
	it('avoids linking an IP shared by multiple devices or retained by an offline device', async () => {
		calls.devices.mockResolvedValue([
			devices[0],
			{ id: 'duplicate-9', ip: '192.168.4.9', connected: true },
			{ id: 'offline-100', ip: '192.168.4.100', connected: false }
		]);
		render(Page);
		await flush();
		expect(screen.queryByRole('link', { name: '192.168.4.9' })).not.toBeInTheDocument();
		expect(screen.queryByRole('link', { name: '192.168.4.100' })).not.toBeInTheDocument();
	});
	it('keeps VPN data visible if device lookup fails and retries on refresh', async () => {
		calls.devices.mockRejectedValueOnce(new Error('Eero unavailable'));
		render(Page);
		await flush();
		expect(screen.getByText('12.50')).toBeInTheDocument();
		expect(screen.getByText('192.168.4.9')).toBeInTheDocument();
		expect(screen.queryByRole('link', { name: '192.168.4.9' })).not.toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		await flush();
		expect(screen.getByRole('link', { name: '192.168.4.9' })).toHaveAttribute(
			'href',
			'/devices/device-9'
		);
		calls.devices.mockRejectedValueOnce(new Error('Eero unavailable'));
		await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
		await flush();
		expect(screen.queryByRole('link', { name: '192.168.4.9' })).not.toBeInTheDocument();
		expect(screen.getByText('192.168.4.9')).toBeInTheDocument();
	});
	it('updates a link when the device at an IP changes', async () => {
		render(Page);
		await flush();
		calls.devices.mockResolvedValue([{ id: 'replacement-9', ip: '192.168.4.9', connected: true }]);
		await vi.advanceTimersByTimeAsync(30000);
		await flush();
		expect(screen.getByRole('link', { name: '192.168.4.9' })).toHaveAttribute(
			'href',
			'/devices/replacement-9'
		);
	});
	it('waits for a selected network before looking up devices', async () => {
		testNetworkId.set(null);
		render(Page);
		await flush();
		expect(calls.devices).not.toHaveBeenCalled();
		expect(screen.getByText('192.168.4.9')).toBeInTheDocument();
		testNetworkId.set('net-1');
		await flush();
		expect(screen.getByRole('link', { name: '192.168.4.9' })).toBeInTheDocument();
	});
	it('clears links on network changes and ignores results from the previous network', async () => {
		let finish!: (value: typeof devices) => void;
		calls.devices.mockReturnValueOnce(new Promise((resolve) => (finish = resolve)));
		render(Page);
		await flush();
		calls.devices.mockResolvedValue([{ id: 'net-2-device-9', ip: '192.168.4.9', connected: true }]);
		testNetworkId.set('net-2');
		await flush();
		expect(calls.devices).toHaveBeenLastCalledWith({ connectedOnly: true, networkId: 'net-2' });
		finish(devices);
		await flush();
		expect(screen.getByRole('link', { name: '192.168.4.9' })).toHaveAttribute(
			'href',
			'/devices/net-2-device-9'
		);
		testNetworkId.set(null);
		await flush();
		expect(screen.queryByRole('link', { name: '192.168.4.9' })).not.toBeInTheDocument();
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
		const clientList = screen.getByRole('list', { name: 'Connected client IP addresses' });
		expect(
			within(clientList)
				.getAllByRole('listitem')
				.map((item) => item.textContent)
		).toEqual(snapshot.connected_client_ips);
		calls.get.mockResolvedValue({
			...snapshot,
			connected_clients: 1,
			connected_client_ips: ['192.168.4.50']
		});
		await vi.advanceTimersByTimeAsync(30000);
		await flush();
		expect(calls.get).toHaveBeenCalledTimes(2);
		expect(screen.getByText('192.168.4.50')).toBeInTheDocument();
		expect(screen.queryByText('192.168.4.9')).not.toBeInTheDocument();
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
		expect(screen.queryByText('192.168.4.9')).not.toBeInTheDocument();
		expect(screen.getByText('Unknown')).toBeInTheDocument();
		expect(screen.getByText('OpenWrt unavailable')).toBeInTheDocument();
	});
	it.each([
		{ ...snapshot, connected_client_ips: null },
		{ ...snapshot, stale: true }
	])('shows unavailable client addresses when data is missing or stale', async (reading) => {
		calls.get.mockResolvedValue(reading);
		render(Page);
		await flush();
		expect(screen.queryByText('192.168.4.9')).not.toBeInTheDocument();
		expect(
			screen.getByText('Client IP addresses are unavailable. Waiting for the next reading.')
		).toBeInTheDocument();
	});
	it('shows an empty state when no clients are active', async () => {
		calls.get.mockResolvedValue({
			...snapshot,
			connected_clients: 0,
			connected_client_ips: []
		});
		render(Page);
		await flush();
		expect(screen.getByText('No active clients through the tunnel.')).toBeInTheDocument();
	});
	it('saves the password through the connection form and clears the field', async () => {
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		await flush();
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
			connected_client_ips: [],
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
		await flush();
		await fireEvent.change(screen.getByLabelText('Tunnel type'), { target: { value: 'network' } });
		expect(screen.getByLabelText('Tunnel name')).toHaveValue('wg_vpn');
		await fireEvent.click(screen.getByRole('button', { name: 'Test and save' }));
		await flush();
		expect(calls.saveConnection).toHaveBeenCalledWith(
			expect.objectContaining({ control_mode: 'network', control_target: 'wg_vpn', password: null })
		);
	});
	it('lists tunnels and preserves an existing selection when there are several', async () => {
		calls.tunnels.mockResolvedValue([
			{ name: 'backup', control_mode: 'openvpn' },
			{ name: 'nordvpn', control_mode: 'openvpn' },
			{ name: 'wg_vpn', control_mode: 'network' }
		]);
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		await flush();
		expect(screen.getByRole('combobox', { name: 'Tunnel name' })).toHaveValue('nordvpn');
		expect(screen.getByRole('option', { name: 'backup' })).toBeInTheDocument();
		expect(screen.queryByRole('option', { name: 'wg_vpn' })).not.toBeInTheDocument();
		await fireEvent.change(screen.getByLabelText('Tunnel name'), { target: { value: 'backup' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Test and save' }));
		await flush();
		expect(calls.saveConnection).toHaveBeenCalledWith(
			expect.objectContaining({ control_target: 'backup' })
		);
	});
	it.each(['openvpn', 'network'])(
		'automatically selects the only %s tunnel and its type',
		async (mode) => {
			calls.connection.mockResolvedValue({ ...connection, control_target: '' });
			calls.tunnels.mockResolvedValue([{ name: 'only_vpn', control_mode: mode }]);
			render(Page);
			await flush();
			await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
			await flush();
			expect(screen.getByLabelText('Tunnel type')).toHaveValue(mode);
			expect(screen.getByLabelText('Tunnel name')).toHaveValue('only_vpn');
			await fireEvent.click(screen.getByRole('button', { name: 'Test and save' }));
			await flush();
			expect(calls.saveConnection).toHaveBeenCalledWith(
				expect.objectContaining({ control_mode: mode, control_target: 'only_vpn' })
			);
		}
	);
	it('leaves multiple tunnels for selection and allows monitoring only', async () => {
		calls.connection.mockResolvedValue({ ...connection, control_target: '' });
		calls.tunnels.mockResolvedValue([
			{ name: 'backup', control_mode: 'openvpn' },
			{ name: 'nordvpn', control_mode: 'openvpn' }
		]);
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		await flush();
		expect(screen.getByLabelText('Tunnel name')).toHaveValue('');
		await fireEvent.change(screen.getByLabelText('Tunnel name'), { target: { value: 'nordvpn' } });
		await fireEvent.change(screen.getByLabelText('Tunnel name'), { target: { value: '' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Test and save' }));
		await flush();
		expect(calls.saveConnection).toHaveBeenCalledWith(
			expect.objectContaining({ control_target: '' })
		);
	});
	it('reports when no tunnels are found without selecting one', async () => {
		calls.connection.mockResolvedValue({ ...connection, control_target: '' });
		calls.tunnels.mockResolvedValue([]);
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		await flush();
		expect(screen.getByText('No tunnels found for this type.')).toBeInTheDocument();
		expect(screen.getByLabelText('Tunnel name')).toHaveValue('');
	});
	it('retries discovery using edited credentials after a failure', async () => {
		calls.tunnels.mockRejectedValueOnce(new Error('SSH login failed.'));
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		await flush();
		expect(screen.getByText('SSH login failed.')).toBeInTheDocument();
		await fireEvent.input(screen.getByLabelText('SSH password'), {
			target: { value: 'new-password' }
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Find tunnels' }));
		await flush();
		expect(calls.tunnels).toHaveBeenLastCalledWith(
			expect.objectContaining({ password: 'new-password' })
		);
		expect(screen.getByRole('combobox', { name: 'Tunnel name' })).toBeInTheDocument();
		expect(screen.queryByText('SSH login failed.')).not.toBeInTheDocument();
		expect(calls.saveConnection).not.toHaveBeenCalled();
	});
	it('ignores discovery results if the connection details change during the request', async () => {
		let finish!: (value: { name: string; control_mode: string }[]) => void;
		calls.tunnels.mockReturnValueOnce(
			new Promise((resolve) => {
				finish = resolve;
			})
		);
		render(Page);
		await flush();
		await fireEvent.click(screen.getByRole('button', { name: 'Connection' }));
		await flush();
		expect(screen.getByRole('button', { name: 'Test and save' })).toBeDisabled();
		await fireEvent.input(screen.getByLabelText('OpenWrt address'), {
			target: { value: '192.168.4.11' }
		});
		finish([{ name: 'old_vpn', control_mode: 'openvpn' }]);
		await flush();
		expect(screen.queryByRole('option', { name: 'old_vpn' })).not.toBeInTheDocument();
		expect(screen.getByLabelText('Tunnel name')).toHaveValue('nordvpn');
	});
});
