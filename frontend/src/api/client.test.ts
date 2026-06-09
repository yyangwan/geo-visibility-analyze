import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  post: vi.fn(),
  use: vi.fn(),
}))

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => ({
      post: mocks.post,
      get: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
      interceptors: {
        request: { use: mocks.use },
        response: { use: mocks.use },
      },
    })),
  },
}))

import { generatePrompts } from './client'

describe('generatePrompts', () => {
  beforeEach(() => {
    mocks.post.mockClear()
  })

  it('sends the current project product category', async () => {
    await generatePrompts(42, 12, '车险')

    expect(mocks.post).toHaveBeenCalledWith('/projects/42/prompts/generate', {
      project_id: 42,
      count: 12,
      product_category: '车险',
    })
  })
})
