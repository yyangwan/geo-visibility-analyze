import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ErrorState from './ErrorState.vue'

describe('ErrorState', () => {
  it('renders default message', () => {
    const wrapper = mount(ErrorState)
    expect(wrapper.text()).toContain('加载失败')
    expect(wrapper.text()).toContain('数据加载出错，请稍后重试')
  })

  it('renders custom message', () => {
    const wrapper = mount(ErrorState, {
      props: { message: '网络连接失败' },
    })
    expect(wrapper.text()).toContain('网络连接失败')
  })

  it('shows retry button with default label', () => {
    const wrapper = mount(ErrorState)
    const btn = wrapper.find('button')
    expect(btn.text()).toBe('重试')
  })

  it('shows retry button with custom label', () => {
    const wrapper = mount(ErrorState, {
      props: { retryLabel: '重新加载' },
    })
    const btn = wrapper.find('button')
    expect(btn.text()).toBe('重新加载')
  })

  it('emits retry when button clicked', async () => {
    const wrapper = mount(ErrorState)
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })
})
