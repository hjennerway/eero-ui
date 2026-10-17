export interface VirtualizationConnection {
	configured: boolean;
	url: string;
	token_id: string;
	has_token: boolean;
	has_ca_certificate: boolean;
	docker_enabled: boolean;
	docker_port: number;
	has_docker_certificates: boolean;
}

export interface VirtualizationConnectionUpdate {
	url: string;
	token_id: string;
	token_secret: string | null;
	ca_certificate: string | null;
	docker_enabled: boolean;
	docker_port: number;
	docker_ca_certificate: string | null;
	docker_client_certificate: string | null;
	docker_client_key: string | null;
}

export interface DockerInventory {
	status: 'disabled' | 'unknown' | 'available' | 'unavailable' | 'not_detected' | 'stale';
	engine_id: string | null;
	version: string | null;
	collected_at: string | null;
	message: string | null;
	containers: {
		id: string;
		name: string;
		image: string;
		state: string;
		health: string | null;
		project: string | null;
	}[];
}

export interface VirtualizationGuest {
	id: string;
	vmid: number;
	kind: 'qemu' | 'lxc';
	name: string;
	status: string;
	template: boolean;
	os_name: string | null;
	os_version: string | null;
	os_source: 'configured' | 'guest_agent' | 'docker';
	ips: string[];
	macs: string[];
	eero_device_ids: string[];
	match_status: 'matched' | 'unmatched' | 'ambiguous';
	issues: string[];
	docker: DockerInventory;
}

export interface VirtualizationNode {
	id: string;
	name: string;
	version: string | null;
	status: string;
	guests: VirtualizationGuest[];
	issues: string[];
}

export interface VirtualizationSnapshot {
	configured: boolean;
	refreshing: boolean;
	collected_at: string | null;
	attempted_at: string | null;
	stale: boolean;
	error: string | null;
	nodes: VirtualizationNode[];
	background_interval_seconds: number;
	visible_interval_seconds: number;
}
