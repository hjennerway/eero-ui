import { error } from '@sveltejs/kit';
import { plugins } from '#lib/plugins/registry.js';

export function load({ params }: { params: { plugin: string } }) {
	if (!plugins.some((plugin) => plugin.id === params.plugin)) error(404, 'Page not found');
	return { pluginId: params.plugin };
}
