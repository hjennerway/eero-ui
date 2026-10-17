import { onSessionReset } from '#lib/session.js';
import type { NASSnapshot } from '#lib/plugins/nas/types.js';

export const NAS_REFRESH_MS = 5 * 60 * 1000;
const cache = new Map<string, { snapshots: NASSnapshot[]; savedAt: number }>();

export function cachedNAS(networkId: string): NASSnapshot[] {
	const entry = cache.get(networkId);
	if (!entry) return [];
	return entry.snapshots.map((snapshot) => ({
		...snapshot,
		stale: snapshot.stale || Date.now() - entry.savedAt >= NAS_REFRESH_MS
	}));
}

export function cacheNAS(networkId: string, snapshots: NASSnapshot[]): void {
	cache.set(networkId, { snapshots, savedAt: Date.now() });
}

export function clearNASCache(): void {
	cache.clear();
}

onSessionReset(clearNASCache);
