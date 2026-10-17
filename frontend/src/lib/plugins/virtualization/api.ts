import { fetchWithHandling } from '$api/client';

export const virtualizationApi = {
	get: (networkId: string) =>
		fetchWithHandling<import('./types').VirtualizationSnapshot>(
			`/networks/${networkId}/virtualization`
		),
	connection: (networkId: string) =>
		fetchWithHandling<import('./types').VirtualizationConnection>(
			`/networks/${networkId}/virtualization/connection`
		),
	saveConnection: (
		networkId: string,
		connection: import('./types').VirtualizationConnectionUpdate
	) =>
		fetchWithHandling<import('./types').VirtualizationConnection>(
			`/networks/${networkId}/virtualization/connection`,
			{
				method: 'PUT',
				body: connection,
				retries: 0
			}
		),
	refresh: (networkId: string, force = true) =>
		fetchWithHandling<import('./types').VirtualizationSnapshot>(
			`/networks/${networkId}/virtualization/refresh`,
			{
				method: 'POST',
				body: { force },
				retries: 0,
				timeout: 65000
			}
		)
};
