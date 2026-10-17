import { afterEach, describe, expect, it, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../../../tests/mocks/server';
import { nasApi } from './nas/api';
import { vpnApi } from './vpn/api';
import { virtualizationApi } from './virtualization/api';

afterEach(() => vi.restoreAllMocks());

describe('plugin API clients', () => {
	it('serves every plugin method through the authenticated shared client', async () => {
		const connection = {
			enabled: false,
			username: '',
			password_set: false,
			port: 443,
			https: true,
			verify_tls: true
		};
		server.use(
			http.get('/api/nas/:nasId/qts', () => HttpResponse.json(connection)),
			http.put('/api/nas/:nasId/qts', () => HttpResponse.json(connection)),
			http.post('/api/nas/:nasId/qts/test', () =>
				HttpResponse.json({ success: true, message: 'Connected' })
			)
		);
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const qts = {
			enabled: false,
			username: '',
			password: null,
			port: 443,
			https: true,
			verify_tls: true
		};
		const vpn = {
			enabled: false,
			host: '192.168.1.2',
			port: 22,
			username: 'root',
			interface: 'tun0',
			control_mode: 'openvpn' as const,
			control_target: '',
			password: null
		};
		await Promise.all([
			nasApi.list('network-123'),
			nasApi.getQTS('qnap', 'network-123'),
			nasApi.updateQTS('qnap', 'network-123', qts),
			nasApi.testQTS('qnap', 'network-123', qts),
			vpnApi.get(),
			vpnApi.connection(),
			vpnApi.tunnels(vpn),
			vpnApi.saveConnection(vpn),
			vpnApi.control('connect'),
			vpnApi.history(),
			virtualizationApi.get('network-123'),
			virtualizationApi.connection('network-123'),
			virtualizationApi.refresh('network-123'),
			virtualizationApi.saveConnection('network-123', {
				url: 'https://pve.test:8006',
				token_id: 'monitor@pve!inventory',
				token_secret: null,
				ca_certificate: null,
				docker_enabled: false,
				docker_port: 2376,
				docker_ca_certificate: null,
				docker_client_certificate: null,
				docker_client_key: null
			})
		]);
		expect(
			warn.mock.calls.filter(([message]) =>
				String(message).includes('without a matching request handler')
			)
		).toEqual([]);
	});

	it('sends CSRF protection and never retries a failed tunnel command', async () => {
		let attempts = 0;
		server.use(
			http.post('/api/vpn/tunnel', ({ request }) => {
				attempts++;
				expect(request.headers.get('X-Requested-With')).toBe('eero-ui');
				return HttpResponse.json({ detail: 'Connection lost' }, { status: 503 });
			})
		);
		await expect(vpnApi.control('disconnect')).rejects.toMatchObject({ status: 503 });
		expect(attempts).toBe(1);
	});
});
