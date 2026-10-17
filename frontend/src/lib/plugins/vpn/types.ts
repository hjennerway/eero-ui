export interface VPNConnection {
	enabled: boolean;
	host: string;
	port: number;
	username: string;
	interface: string;
	control_mode: 'openvpn' | 'network';
	control_target: string;
	password_set: boolean;
	host_key: string | null;
}

export interface VPNConnectionUpdate {
	enabled: boolean;
	host: string;
	port: number;
	username: string;
	interface: string;
	control_mode: 'openvpn' | 'network';
	control_target: string;
	password: string | null;
}

export interface VPNTunnel {
	name: string;
	control_mode: 'openvpn' | 'network';
}

export interface VPNSnapshot {
	host: string;
	interface: string;
	provider: string;
	status: 'connected' | 'disconnected' | 'unknown' | 'unreachable' | 'disabled';
	reachable: boolean;
	connected_clients: number | null;
	connected_client_ips: string[] | null;
	download_mbps: number | null;
	upload_mbps: number | null;
	rx_bytes: number | null;
	tx_bytes: number | null;
	collected_at: string | null;
	last_success_at: string | null;
	stale: boolean;
	error: string | null;
	warnings: string[];
	history_error: string | null;
	interval_seconds: number;
	control_available: boolean;
}

export interface VPNHistory {
	download_mbps: Array<{ x: number; y: number }>;
	upload_mbps: Array<{ x: number; y: number }>;
	connected_clients: Array<{ x: number; y: number }>;
	connection_status: Array<{ x: number; y: number }>;
}
