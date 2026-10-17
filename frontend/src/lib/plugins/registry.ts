import { derived, writable } from 'svelte/store';
import { api } from '$api/client';
import type { DashboardPlugin } from './types';
import { onSessionReset } from '#lib/session.js';

// Load the small manifests eagerly; each manifest loads its page on demand.
const bundledManifests = import.meta.glob<DashboardPlugin>('./*/plugin.ts', {
	eager: true,
	import: 'default'
});
export const plugins = Object.values(bundledManifests);
const state = writable<{ loaded: boolean; ids: string[]; error: string | null }>({
	loaded: false,
	ids: [],
	error: null
});
let generation = 0;
let pending: Promise<void> | null = null;

export const pluginState = { subscribe: state.subscribe };
export const enabledPlugins = derived(state, ($state) =>
	plugins.filter((plugin) => $state.ids.includes(plugin.id))
);
export const pluginNavItems = derived(enabledPlugins, ($plugins) =>
	$plugins.map((plugin) => ({ path: `/${plugin.id}`, label: plugin.label, icon: plugin.icon }))
);

export function loadPlugins(): Promise<void> {
	if (pending) return pending;
	const request = generation;
	pending = api.plugins
		.list()
		.then((available) => {
			if (request === generation)
				state.set({ loaded: true, ids: available.map((p) => p.id), error: null });
		})
		.catch(() => {
			if (request === generation)
				state.set({ loaded: true, ids: [], error: 'Could not load plugins. Try again.' });
		})
		.finally(() => {
			if (request === generation) pending = null;
		});
	return pending;
}

onSessionReset(() => {
	generation += 1;
	pending = null;
	state.set({ loaded: false, ids: [], error: null });
});
