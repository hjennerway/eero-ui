import type { DashboardPlugin } from '../types';

export default {
	id: 'nas',
	label: 'NAS',
	icon: 'server',
	load: () => import('./Page.svelte'),
	mqttInfo: () => import('./MQTTInfo.svelte')
} satisfies DashboardPlugin;
