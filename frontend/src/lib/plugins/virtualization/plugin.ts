import type { DashboardPlugin } from '../types';

export default {
	id: 'virtualization',
	label: 'Virtualization',
	icon: 'server',
	load: () => import('./Page.svelte')
} satisfies DashboardPlugin;
