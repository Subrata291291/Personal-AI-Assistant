export function requireArrayResponse(value, resource) {
  if (!Array.isArray(value)) {
    throw new Error(`Invalid ${resource} response.`)
  }

  return value
}

export function requireConnectionsResponse(value) {
  if (
    !value ||
    typeof value !== 'object' ||
    !Array.isArray(value.connections) ||
    (
      value.calendar_capabilities !== undefined &&
      (
        !value.calendar_capabilities ||
        typeof value.calendar_capabilities !== 'object' ||
        Array.isArray(value.calendar_capabilities)
      )
    )
  ) {
    throw new Error('Invalid connected accounts response.')
  }

  return value
}
