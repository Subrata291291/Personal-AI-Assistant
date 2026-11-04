export function resolveAssistantGmailConnection(
  connections,
  defaultConnectionId,
) {
  const connected = connections.filter(
    (connection) =>
      connection.provider === 'google' &&
      connection.status === 'connected',
  )

  if (connected.length === 1) return connected[0]
  if (!defaultConnectionId) return null

  return connected.find(
    (connection) => connection.id === defaultConnectionId,
  ) || null
}
