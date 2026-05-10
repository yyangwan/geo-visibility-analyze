import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import EmptyState from './EmptyState.vue'

describe('EmptyState', () => {
  it('renders title', () => {
    const wrapper = mount(EmptyState, {
      props: { title: '暂无数据' },
    })
    expect(wrapper.text()).toContain('暂无数据')
  })

  it('renders description when provided', () => {
    const wrapper = mount(EmptyState, {
      props: { title: '暂无数据', description: '完成首次审计后查看' },
    })
    expect(wrapper.text()).toContain('完成首次审计后查看')
  })

  it('hides description when not provided', () => {
    const wrapper = mount(EmptyState, {
      props: { title: '暂无数据' },
    })
    expect(wrapper.find('p').exists()).toBe(false)
  })

  it('shows action button when actionLabel provided', () => {
    const wrapper = mount(EmptyState, {
      props: { title: '暂无数据', actionLabel: '+ 新建审计' },
    })
    const btn = wrapper.find('button')
    expect(btn.text()).toBe('+ 新建审计')
  })

  it('hides action button when actionLabel not provided', () => {
    const wrapper = mount(EmptyState, {
      props: { title: '暂无数据' },
    })
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('emits action when button clicked', async () => {
    const wrapper = mount(EmptyState, {
      props: { title: '暂无数据', actionLabel: '开始' },
    })
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('action')).toHaveLength(1)
  })

  it('renders icon when provided', () => {
    const wrapper = mount(EmptyState, {
      props: { title: '暂无数据', icon: '🔍' },
    })
    expect(wrapper.text()).toContain('🔍')
  })
})
