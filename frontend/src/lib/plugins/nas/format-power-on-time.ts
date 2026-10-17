/** Accumulated runtime, using approximate 365-day years and 30-day months. */
export function formatPowerOnTime(hours: number | null | undefined): string {
	if (hours == null || !Number.isSafeInteger(hours) || hours < 0) return 'Unavailable';
	let days = Math.floor(hours / 24);
	const years = Math.floor(days / 365);
	days %= 365;
	// Keep months below a year; the final month can include the year's extra days.
	const months = Math.min(11, Math.floor(days / 30));
	days -= months * 30;
	const parts: [number, string][] = [
		[years, 'year'],
		[months, 'month'],
		[days, 'day'],
		[hours % 24, 'hour']
	];
	const first = parts.findIndex(([value]) => value > 0);
	return parts
		.slice(first < 0 ? 3 : first)
		.map(([value, unit]) => `${value.toLocaleString()} ${unit}${value === 1 ? '' : 's'}`)
		.join(', ');
}
