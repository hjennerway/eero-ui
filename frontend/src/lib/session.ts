// Plugins register cache cleanup when their lazy-loaded modules first run.
const resets = new Set<() => void>();

export function onSessionReset(reset: () => void): void {
	resets.add(reset);
}

export function resetSessionCaches(): void {
	for (const reset of resets) reset();
}
