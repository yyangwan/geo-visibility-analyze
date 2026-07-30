import { describe, expect, it } from 'vitest'
import { formatDate, formatDateTime, formatTimeOnly } from './date'

describe('date formatting', () => {
  it('treats timezone-less backend values as UTC', () => {
    expect(formatDateTime('2026-07-29T23:50:25')).toBe('2026-07-30 07:50')
  })

  it('respects explicit offsets without appending another timezone', () => {
    expect(formatDateTime('2026-07-29T19:50:25-04:00')).toBe('2026-07-30 07:50')
  })

  it('formats date and time consistently in Asia/Shanghai', () => {
    expect(formatDate('2026-07-29T23:50:25Z')).toBe('2026-07-30')
    expect(formatTimeOnly('2026-07-29T23:50:25Z')).toBe('07:50')
  })
})
