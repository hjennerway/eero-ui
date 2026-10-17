import { fetchWithHandling } from '$api/client';

export const nasApi = {
	list: (networkId?: string, refresh = false) =>
		fetchWithHandling<import('./types').NASSnapshot[]>('/nas', {
			params: {
				...(networkId ? { network_id: networkId } : {}),
				...(refresh ? { refresh: true } : {})
			},
			retries: 0,
			timeout: 60000
		}),
	getQTS: (nasId: string, networkId: string) =>
		fetchWithHandling<import('./types').QTSConnectionStatus>(
			`/nas/${encodeURIComponent(nasId)}/qts`,
			{
				params: { network_id: networkId },
				retries: 0
			}
		),
	updateQTS: (nasId: string, networkId: string, settings: import('./types').QTSConnectionUpdate) =>
		fetchWithHandling<import('./types').QTSConnectionStatus>(
			`/nas/${encodeURIComponent(nasId)}/qts`,
			{
				method: 'PUT',
				body: settings,
				params: { network_id: networkId },
				retries: 0
			}
		),
	testQTS: (nasId: string, networkId: string, settings: import('./types').QTSConnectionUpdate) =>
		fetchWithHandling<import('./types').QTSTestResult>(
			`/nas/${encodeURIComponent(nasId)}/qts/test`,
			{
				method: 'POST',
				body: settings,
				params: { network_id: networkId },
				retries: 0,
				timeout: 60000
			}
		)
};
