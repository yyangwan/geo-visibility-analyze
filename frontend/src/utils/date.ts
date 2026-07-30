const DISPLAY_TIME_ZONE = 'Asia/Shanghai'

function toUtcDate(iso: string): Date {
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso)
  return new Date(hasTimezone ? iso : `${iso}Z`)
}

function formatParts(iso: string) {
  return Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: DISPLAY_TIME_ZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(toUtcDate(iso)).map((part) => [part.type, part.value]),
  )
}

/** Format a UTC datetime string as Asia/Shanghai in "YYYY-MM-DD HH:mm" format. */
export function formatDateTime(iso?: string | null): string {
  if (!iso) return ''
  const parts = formatParts(iso)
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

/** Format as date only (YYYY-MM-DD) in Asia/Shanghai timezone. */
export function formatDate(iso?: string | null): string {
  if (!iso) return ''
  const parts = formatParts(iso)
  return `${parts.year}-${parts.month}-${parts.day}`
}

/** Format time portion only (HH:mm) in Asia/Shanghai timezone. */
export function formatTimeOnly(iso?: string | null): string {
  if (!iso) return ''
  const parts = formatParts(iso)
  return `${parts.hour}:${parts.minute}`
}
