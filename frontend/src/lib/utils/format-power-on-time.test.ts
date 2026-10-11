import { describe, expect, it } from 'vitest';
import { formatPowerOnTime } from './format-power-on-time';

describe('power-on duration', () => {
	it.each([
		[0, '0 hours'],
		[1, '1 hour'],
		[23, '23 hours'],
		[24, '1 day, 0 hours'],
		[25, '1 day, 1 hour'],
		[720, '1 month, 0 days, 0 hours'],
		[8736, '11 months, 34 days, 0 hours'],
		[8760, '1 year, 0 months, 0 days, 0 hours'],
		[9505, '1 year, 1 month, 1 day, 1 hour'],
		[51448, '5 years, 10 months, 18 days, 16 hours']
	])('formats %s hours as %s', (hours, expected) => {
		expect(formatPowerOnTime(hours)).toBe(expected);
	});

	it.each([null, undefined, -1, NaN, Infinity, 1.5])(
		'rejects unavailable or invalid hours: %s',
		(hours) => {
			expect(formatPowerOnTime(hours)).toBe('Unavailable');
		}
	);
});
