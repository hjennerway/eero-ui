import { fetchWithHandling } from '$api/client';

export const vpnApi = {
	get: () => fetchWithHandling<import('./types').VPNSnapshot>('/vpn', { retries: 0 }),
	control: (action: 'connect' | 'disconnect') =>
		fetchWithHandling<import('./types').VPNSnapshot>('/vpn/tunnel', {
			method: 'POST',
			body: { action },
			retries: 0,
			timeout: 60000
		}),
	connection: () =>
		fetchWithHandling<import('./types').VPNConnection>('/vpn/connection', { retries: 0 }),
	tunnels: (connection: import('./types').VPNConnectionUpdate) =>
		fetchWithHandling<import('./types').VPNTunnel[]>('/vpn/tunnels', {
			method: 'POST',
			body: connection,
			retries: 0,
			timeout: 60000
		}),
	saveConnection: (connection: import('./types').VPNConnectionUpdate) =>
		fetchWithHandling<import('./types').VPNConnection>('/vpn/connection', {
			method: 'PUT',
			body: connection,
			retries: 0,
			timeout: 60000
		}),
	history: (range: '1h' | '6h' | '24h' = '1h') =>
		fetchWithHandling<import('./types').VPNHistory>('/vpn/history', {
			params: { range },
			retries: 0
		})
};
