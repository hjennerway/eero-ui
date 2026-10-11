import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { api } from '$api/client';
import QTSConnectionModal from './QTSConnectionModal.svelte';

const stored = {
	enabled: true,
	username: 'monitor',
	password_set: true,
	port: 443,
	https: true,
	verify_tls: true
};
const props = {
	nasId: 'qnap',
	networkId: 'home',
	name: 'NAS',
	host: '192.168.4.2',
	onClose: vi.fn(),
	onSaved: vi.fn()
};

beforeEach(() => {
	vi.spyOn(api.nas, 'getQTS').mockResolvedValue(stored);
	vi.spyOn(api.nas, 'updateQTS').mockResolvedValue(stored);
	vi.spyOn(api.nas, 'testQTS').mockResolvedValue({
		success: true,
		message: 'Connected. Power-on time is available for 6 drives.'
	});
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
	vi.clearAllMocks();
});

describe('QTS connection', () => {
	it('keeps an existing password out of the form and preserves it on save', async () => {
		render(QTSConnectionModal, props);
		await screen.findByDisplayValue('monitor');
		expect(screen.getByLabelText('QTS password')).toHaveValue('');
		await fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
		await waitFor(() => expect(props.onSaved).toHaveBeenCalled());
		expect(api.nas.updateQTS).toHaveBeenCalledWith('qnap', 'home', {
			enabled: true,
			username: 'monitor',
			password: null,
			port: 443,
			https: true,
			verify_tls: true
		});
	});

	it('tests new credentials without saving, then saves entered connection details', async () => {
		vi.mocked(api.nas.getQTS).mockResolvedValue({
			...stored,
			enabled: false,
			username: '',
			password_set: false
		});
		render(QTSConnectionModal, props);
		await screen.findByLabelText('QTS username');
		expect(screen.getByRole('button', { name: 'Save connection' })).toBeDisabled();
		await fireEvent.input(screen.getByLabelText('QTS username'), {
			target: { value: 'new-monitor' }
		});
		await fireEvent.input(screen.getByLabelText('QTS password'), {
			target: { value: 'test-secret' }
		});
		await fireEvent.click(screen.getByLabelText('Use HTTPS'));
		expect(screen.getByLabelText('QTS web port')).toHaveValue(8080);
		await fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
		await screen.findByText(/Connected. Power-on time is available for 6 drives/);
		expect(api.nas.updateQTS).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
		await waitFor(() => expect(props.onSaved).toHaveBeenCalled());
		expect(api.nas.updateQTS).toHaveBeenCalledWith('qnap', 'home', {
			enabled: true,
			username: 'new-monitor',
			password: 'test-secret',
			port: 8080,
			https: false,
			verify_tls: true
		});
		expect(screen.getByLabelText('QTS password')).toHaveValue('');
	});

	it('removes the saved connection explicitly', async () => {
		render(QTSConnectionModal, props);
		await screen.findByRole('button', { name: 'Remove connection' });
		await fireEvent.click(screen.getByRole('button', { name: 'Remove connection' }));
		await waitFor(() => expect(props.onSaved).toHaveBeenCalled());
		expect(api.nas.updateQTS).toHaveBeenCalledWith(
			'qnap',
			'home',
			expect.objectContaining({ enabled: false })
		);
	});

	it('shows a failed connection test and lets the user correct the form', async () => {
		vi.mocked(api.nas.testQTS).mockResolvedValue({
			success: false,
			message: 'Check QTS credentials.'
		});
		render(QTSConnectionModal, props);
		await screen.findByDisplayValue('monitor');
		await fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
		expect(await screen.findByRole('alert')).toHaveTextContent('Check QTS credentials.');
		expect(screen.getByRole('button', { name: 'Save connection' })).toBeEnabled();
		expect(props.onSaved).not.toHaveBeenCalled();
	});

	it('retains entries after a save error and supports retrying a failed settings read', async () => {
		vi.mocked(api.nas.getQTS).mockRejectedValueOnce(new Error('Could not load connection.'));
		vi.mocked(api.nas.updateQTS).mockRejectedValueOnce(new Error('Could not save connection.'));
		render(QTSConnectionModal, props);
		await screen.findByRole('button', { name: 'Retry' });
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		await screen.findByDisplayValue('monitor');
		await fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
		expect(await screen.findByRole('alert')).toHaveTextContent('Could not save connection.');
		expect(screen.getByLabelText('QTS username')).toHaveValue('monitor');
	});
});
