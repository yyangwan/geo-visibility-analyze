import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import LoadingSkeleton from './LoadingSkeleton.vue'

describe('LoadingSkeleton', () => {
  it('renders card variant by default', () => {
    const wrapper = mount(LoadingSkeleton)
    expect(wrapper.find('.skeleton-card').exists()).toBe(true)
  })

  it('renders 4 skeleton cards for card variant with count=4', () => {
    const wrapper = mount(LoadingSkeleton, {
      props: { variant: 'card', count: 4 },
    })
    expect(wrapper.findAll('.skeleton-card')).toHaveLength(4)
  })

  it('renders table variant', () => {
    const wrapper = mount(LoadingSkeleton, {
      props: { variant: 'table', count: 5 },
    })
    expect(wrapper.findAll('.skeleton-row')).toHaveLength(5)
  })

  it('renders chart variant', () => {
    const wrapper = mount(LoadingSkeleton, {
      props: { variant: 'chart' },
    })
    expect(wrapper.find('.skeleton-pulse').exists()).toBe(true)
  })

  it('renders list variant', () => {
    const wrapper = mount(LoadingSkeleton, {
      props: { variant: 'list', count: 3 },
    })
    expect(wrapper.findAll('.skeleton-list-item')).toHaveLength(3)
  })
})
