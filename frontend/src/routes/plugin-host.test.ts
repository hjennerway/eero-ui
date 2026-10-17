import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import { api } from '$api/client';
import { resetSessionCaches } from '#lib/session.js';
import { plugins } from '#lib/plugins/registry.js';
import Host from './[plugin]/+page.svelte';
import { load } from './[plugin]/+page';

afterEach(() => {
	cleanup();
	resetSessionCaches();
	vi.restoreAllMocks();
});

describe('plugin page host', () => {
	it('preserves the existing integration URLs and rejects unknown pages', () => {
		expect(plugins.map((plugin) => plugin.id)).toContain('virtualization');
		expect(load({ params: { plugin: 'nas' } })).toEqual({ pluginId: 'nas' });
		expect(load({ params: { plugin: 'vpn' } })).toEqual({ pluginId: 'vpn' });
		expect(load({ params: { plugin: 'virtualization' } })).toEqual({ pluginId: 'virtualization' });
		let missing: unknown;
		try {
			load({ params: { plugin: 'missing' } });
		} catch (error) {
			missing = error;
		}
		expect(missing).toMatchObject({ status: 404 });
	});

	it('does not load a disabled plugin page', async () => {
		vi.spyOn(api.plugins, 'list').mockResolvedValue([]);
		const page = vi.spyOn(
			plugins.find((plugin) => plugin.id === 'nas')!,
			'load'
		);
		render(Host, { data: { pluginId: 'nas' } });
		expect(await screen.findByRole('alert')).toHaveTextContent('disabled');
		expect(page).not.toHaveBeenCalled();
	});

	it.each([
		['nas', 'NAS'],
		['virtualization', 'Virtualization']
	])(
		'lazy loads the enabled %s plugin into the shared page host',
		async (id, label) => {
			vi.spyOn(api.plugins, 'list').mockResolvedValue([{ id }]);
			render(Host, { data: { pluginId: id } });
			// The first dynamic import can compile the diagram bundle during a full suite run.
			expect(
				await screen.findByRole('heading', { name: label }, { timeout: 5000 })
			).toBeInTheDocument();
		},
		10000
	);
});
