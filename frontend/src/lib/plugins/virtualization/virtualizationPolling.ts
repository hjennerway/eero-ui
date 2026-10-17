/** Poll only while this page is visible; its backend also polls with no browser open. */
export function startVisibleRefresh(refresh: () => void, interval = 60000): () => void {
	let timer: ReturnType<typeof setInterval> | undefined;
	const visibilityChanged = () => {
		if (timer !== undefined) clearInterval(timer);
		timer = undefined;
		if (document.visibilityState === 'visible') {
			refresh();
			timer = setInterval(refresh, interval);
		}
	};
	document.addEventListener('visibilitychange', visibilityChanged);
	visibilityChanged();
	return () => {
		if (timer !== undefined) clearInterval(timer);
		document.removeEventListener('visibilitychange', visibilityChanged);
	};
}
