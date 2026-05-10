import { describe, it, expect } from 'vitest'
import { PLATFORM_LABELS, PLATFORM_KEYS } from './platforms'

describe('Platform constants', () => {
  it('has 5 platforms', () => {
    expect(PLATFORM_KEYS).toHaveLength(5)
  })

  it('includes all required platforms', () => {
    const required = ['deepseek', 'qwen', 'doubao', 'kimi', 'hunyuan']
    required.forEach(p => {
      expect(PLATFORM_LABELS[p]).toBeDefined()
    })
  })

  it('labels are Chinese display names', () => {
    expect(PLATFORM_LABELS.deepseek).toBe('DeepSeek')
    expect(PLATFORM_LABELS.qwen).toBe('通义千问')
    expect(PLATFORM_LABELS.doubao).toBe('豆包')
    expect(PLATFORM_LABELS.kimi).toBe('Kimi')
    expect(PLATFORM_LABELS.hunyuan).toBe('腾讯元宝')
  })

  it('PLATFORM_KEYS matches PLATFORM_LABELS keys', () => {
    expect(PLATFORM_KEYS).toEqual(Object.keys(PLATFORM_LABELS))
  })
})
