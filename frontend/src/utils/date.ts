/** Format a UTC datetime string as Asia/Shanghai (UTC+8) in "YYYY-MM-DD HH:mm" format. */
export function formatDateTime(iso?: string | null): string {
  if (!iso) return ''
  const suffix = iso.endsWith('Z') || iso.includes('+') ? '' : 'Z'
  const d = new Date(iso + suffix)
  const cn = new Date(d.getTime() + 8 * 3600 * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${cn.getUTCFullYear()}-${pad(cn.getUTCMonth() + 1)}-${pad(cn.getUTCDate())} ${pad(cn.getUTCHours())}:${pad(cn.getUTCMinutes())}`
}

/** Format as date only (YYYY-MM-DD) in Asia/Shanghai timezone. */
export function formatDate(iso?: string | null): string {
  if (!iso) return ''
  const suffix = iso.endsWith('Z') || iso.includes('+') ? '' : 'Z'
  const d = new Date(iso + suffix)
  const cn = new Date(d.getTime() + 8 * 3600 * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${cn.getUTCFullYear()}-${pad(cn.getUTCMonth() + 1)}-${pad(cn.getUTCDate())}`
}

/** Format time portion only (HH:mm) in Asia/Shanghai timezone. */
export function formatTimeOnly(iso?: string | null): string {
  if (!iso) return ''
  const suffix = iso.endsWith('Z') || iso.includes('+') ? '' : 'Z'
  const d = new Date(iso + suffix)
  const cn = new Date(d.getTime() + 8 * 3600 * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(cn.getUTCHours())}:${pad(cn.getUTCMinutes())}`
}
