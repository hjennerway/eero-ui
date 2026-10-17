import type { DashboardPlugin } from '../types';

export default {
	id: 'vpn',
	label: 'VPN',
	icon: 'lock',
	load: () => import('./Page.svelte'),
	mqttInfo: () => import('./MQTTInfo.svelte')
} satisfies DashboardPlugin;
