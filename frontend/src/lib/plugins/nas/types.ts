export type NASHealth = 'good' | 'warning' | 'critical' | 'unknown';

export interface QTSConnectionUpdate {
	enabled: boolean;
	username: string;
	password: string | null;
	port: number;
	https: boolean;
	verify_tls: boolean;
}

export interface QTSConnectionStatus extends Omit<QTSConnectionUpdate, 'password'> {
	password_set: boolean;
}

export interface QTSTestResult {
	success: boolean;
	message: string;
}

export interface NASDisk {
	id: string;
	name: string;
	enclosure_id: number | null;
	bay: number | null;
	model: string | null;
	status: string | null;
	smart_status: string | null;
	health: NASHealth;
	temperature_c: number | null;
	capacity_bytes: number | null;
	power_on_hours?: number | null;
	power_on_days: number | null;
	ssd_life_remaining_percent: number | null;
}

export interface NASStorage {
	id: string;
	name: string;
	status: string | null;
	health: NASHealth;
	raid_level: string | null;
	capacity_bytes: number | null;
	free_bytes: number | null;
	used_bytes: number | null;
	used_percent: number | null;
}

export interface NASSnapshot {
	id: string;
	name: string;
	host: string;
	status: 'online' | 'unreachable';
	model: string | null;
	observed_at: string | null;
	stale?: boolean;
	disks: NASDisk[];
	volumes: NASStorage[];
	pools: NASStorage[];
	raids: NASStorage[];
	warnings: string[];
}
