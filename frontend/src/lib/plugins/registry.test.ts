import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { api } from '$api/client';
import { resetSessionCaches } from '#lib/session.js';
import { enabledPlugins, loadPlugins, pluginNavItems, pluginState } from './registry';
import { cacheNAS, cachedNAS } from './nas/cache';

afterEach(() => {
	resetSessionCaches();
	vi.restoreAllMocks();
});

describe('plugin availability', () => {
	it('only renders links for plugins enabled by the server', async () => {
		vi.spyOn(api.plugins, 'list').mockResolvedValue([{ id: 'vpn' }, { id: 'unknown' }]);
		await loadPlugins();
		expect(get(pluginNavItems)).toEqual([{ path: '/vpn', label: 'VPN', icon: 'lock' }]);
		expect(get(enabledPlugins).map((p) => p.id)).toEqual(['vpn']);
	});

	it('supports a server with no plugins', async () => {
		vi.spyOn(api.plugins, 'list').mockResolvedValue([]);
		await loadPlugins();
		expect(get(pluginState).loaded).toBe(true);
		expect(get(pluginNavItems)).toEqual([]);
	});

	it('ignores discovery completing after logout and clears plugin caches', async () => {
		let resolve!: (value: { id: string }[]) => void;
		vi.spyOn(api.plugins, 'list').mockReturnValue(
			new Promise((done) => {
				resolve = done;
			})
		);
		cacheNAS('home', [
			{
				id: 'qnap',
				name: 'QNAP',
				host: '192.168.1.2',
				status: 'online',
				model: null,
				observed_at: null,
				disks: [],
				volumes: [],
				pools: [],
				raids: [],
				warnings: []
			}
		]);
		expect(cachedNAS('home')).toHaveLength(1);
		const pending = loadPlugins();
		resetSessionCaches();
		resolve([{ id: 'nas' }]);
		await pending;
		expect(get(pluginState).loaded).toBe(false);
		expect(get(pluginNavItems)).toEqual([]);
		expect(cachedNAS('home')).toEqual([]);
	});

	it('makes discovery failures retryable', async () => {
		vi.spyOn(api.plugins, 'list')
			.mockRejectedValueOnce(new Error('offline'))
			.mockResolvedValueOnce([{ id: 'nas' }]);
		await loadPlugins();
		expect(get(pluginState).error).toContain('Could not load');
		await loadPlugins();
		expect(get(pluginState).error).toBeNull();
		expect(get(pluginNavItems)[0].path).toBe('/nas');
	});
});
