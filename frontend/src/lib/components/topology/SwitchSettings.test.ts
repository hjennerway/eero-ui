import { expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/svelte';
import { http, HttpResponse } from 'msw';
import { server } from '../../../../tests/mocks/server';
import type { SwitchSettingsUpdate } from '$api/types';
import { topologyStore } from '$stores/topology';
import SwitchSettings from './SwitchSettings.svelte';

it('edits existing wiring with an empty community field and refreshes topology after saving', async () => {
	let body: SwitchSettingsUpdate | undefined;
	let networkId: string | null = null;
	const refresh = vi.spyOn(topologyStore, 'loadTopology').mockResolvedValue();
	const settings = {
		network_id: 'network-123',
		revision: '0'.repeat(64),
		switches: [
			{
				id: 'office',
				name: 'Office switch',
				host: '192.168.1.10',
				port: 161,
				unmanaged_switches: [
					{ id: 'desk', name: 'Desk switch', port: 108, eero_ids: ['eero-2'], devices: [] }
				]
			}
		]
	};
	server.use(
		http.get('/api/switches/settings', () => HttpResponse.json(settings)),
		http.put('/api/switches/settings', async ({ request }) => {
			body = (await request.json()) as SwitchSettingsUpdate;
			networkId = new URL(request.url).searchParams.get('network_id');
			return HttpResponse.json(settings);
		})
	);

	try {
		render(SwitchSettings);
		await fireEvent.click(screen.getByRole('button', { name: 'Switch settings' }));
		const dialog = screen.getByRole('dialog');
		await fireEvent.click(await within(dialog).findByText('Office switch'));
		await fireEvent.click(within(dialog).getByText('Desk switch'));
		expect(within(dialog).getByLabelText('Read-only SNMP community')).toHaveValue('');
		expect(within(dialog).getByLabelText('eero-2')).toBeChecked();

		await fireEvent.click(within(dialog).getByRole('button', { name: 'Add known device' }));
		await fireEvent.input(within(dialog).getByLabelText('Device name'), {
			target: { value: 'NAS' }
		});
		await fireEvent.input(within(dialog).getByLabelText('Device IPv4 address'), {
			target: { value: '192.168.1.20' }
		});
		await fireEvent.input(within(dialog).getByLabelText('Device MAC address'), {
			target: { value: '02:00:00:00:00:01' }
		});
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Save switches' }));

		await screen.findByText('Switch settings saved.');
		expect(networkId).toBe('network-123');
		expect(body).toEqual({
			revision: settings.revision,
			switches: [
				{
					...settings.switches[0],
					community: null,
					unmanaged_switches: [
						{
							...settings.switches[0].unmanaged_switches[0],
							devices: [{ name: 'NAS', ip: '192.168.1.20', mac: '02:00:00:00:00:01' }]
						}
					]
				}
			]
		});
		expect(refresh).toHaveBeenCalledOnce();
	} finally {
		refresh.mockRestore();
	}
});
