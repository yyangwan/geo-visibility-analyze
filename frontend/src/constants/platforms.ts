/** Shared platform label mapping — single source of truth */
export const PLATFORM_LABELS: Record<string, string> = {
  deepseek: 'DeepSeek',
  qwen: '通义千问',
  doubao: '豆包',
  kimi: 'Kimi',
  wenxin: '文心一言',
  hunyuan: '腾讯元宝',
} as const

/** Platform key list (for iteration order) */
export const PLATFORM_KEYS = Object.keys(PLATFORM_LABELS) as (keyof typeof PLATFORM_LABELS)[]
