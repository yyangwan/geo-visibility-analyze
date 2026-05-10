import { describe, it, expect } from 'vitest'

// Token system: verify CSS custom properties are defined in style.css
// We parse the raw CSS to check token existence
import fs from 'fs'
import path from 'path'

const stylePath = path.resolve(__dirname, 'style.css')
const css = fs.readFileSync(stylePath, 'utf-8')

describe('Design token system', () => {
  describe('Typography tokens', () => {
    const typoTokens = ['--text-xs', '--text-sm', '--text-base', '--text-md', '--text-lg', '--text-xl', '--text-2xl']
    typoTokens.forEach(token => {
      it(`defines ${token}`, () => {
        expect(css).toContain(token)
      })
    })
  })

  describe('Spacing tokens', () => {
    const spacingTokens = ['--space-1', '--space-2', '--space-3', '--space-4', '--space-5', '--space-6', '--space-7', '--space-8']
    spacingTokens.forEach(token => {
      it(`defines ${token}`, () => {
        expect(css).toContain(token)
      })
    })
  })

  describe('Motion tokens', () => {
    it('defines --duration-fast', () => {
      expect(css).toContain('--duration-fast')
    })
    it('defines --duration-normal', () => {
      expect(css).toContain('--duration-normal')
    })
    it('defines --ease-default', () => {
      expect(css).toContain('--ease-default')
    })
  })

  describe('Status tokens', () => {
    const statusTokens = ['--status-good', '--status-warn', '--status-bad']
    statusTokens.forEach(token => {
      it(`defines ${token}`, () => {
        expect(css).toContain(token)
      })
    })
  })

  describe('Accessibility', () => {
    it('has focus-visible outline', () => {
      expect(css).toContain(':focus-visible')
    })
    it('has skip-to-content styles', () => {
      expect(css).toContain('.skip-to-content')
    })
  })

  it('does not use -apple-system in font stack', () => {
    expect(css).not.toContain('-apple-system')
  })
})
