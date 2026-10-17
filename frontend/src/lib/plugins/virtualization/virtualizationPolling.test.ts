import { afterEach, describe, expect, it, vi } from 'vitest';
import { startVisibleRefresh } from './virtualizationPolling';

describe('virtualization visible refresh', () => {
	const descriptor = Object.getOwnPropertyDescriptor(document, 'visibilityState');
	let stop = () => {};
	afterEach(() => {
		stop();
		vi.useRealTimers();
		if (descriptor) Object.defineProperty(document, 'visibilityState', descriptor);
		else Reflect.deleteProperty(document, 'visibilityState');
	});
	function visibility(state: DocumentVisibilityState) {
		Object.defineProperty(document, 'visibilityState', { configurable: true, value: state });
		document.dispatchEvent(new Event('visibilitychange'));
	}
	it('refreshes on opening, every 60 seconds, pauses when hidden, and resumes when visible', () => {
		vi.useFakeTimers();
		visibility('visible');
		const refresh = vi.fn();
		stop = startVisibleRefresh(refresh);
		expect(refresh).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(59999);
		expect(refresh).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(1);
		expect(refresh).toHaveBeenCalledTimes(2);
		visibility('hidden');
		vi.advanceTimersByTime(600000);
		expect(refresh).toHaveBeenCalledTimes(2);
		visibility('visible');
		expect(refresh).toHaveBeenCalledTimes(3);
		vi.advanceTimersByTime(60000);
		expect(refresh).toHaveBeenCalledTimes(4);
		stop();
		visibility('hidden');
		visibility('visible');
		vi.advanceTimersByTime(60000);
		expect(refresh).toHaveBeenCalledTimes(4);
	});
	it('does not poll if opened in a hidden tab', () => {
		vi.useFakeTimers();
		visibility('hidden');
		const refresh = vi.fn();
		stop = startVisibleRefresh(refresh);
		vi.advanceTimersByTime(600000);
		expect(refresh).not.toHaveBeenCalled();
	});
});
