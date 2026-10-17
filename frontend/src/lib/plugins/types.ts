import type { Component } from 'svelte';
import type { IconName } from '#lib/icons/paths.js';

export interface DashboardPlugin {
	id: string;
	label: string;
	icon: IconName;
	load: () => Promise<{ default: Component }>;
	mqttInfo?: () => Promise<{ default: Component<{ topic: string }> }>;
}
