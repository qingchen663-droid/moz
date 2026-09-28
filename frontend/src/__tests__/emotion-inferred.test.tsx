/// <reference types="vitest/globals" />
import { afterEach, beforeAll, afterAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import MemoryViewerModal from '../components/MemoryViewerModal'
import { useStore } from '../store'

const originalFetch = globalThis.fetch
const confirms: string[] = []

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

let erased: string[] = []

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {}
  vi.spyOn(window, 'confirm').mockImplementation((msg) => {
    confirms.push(String(msg))
    return true
  })
  useStore.setState({ userId: 'web_user_001' })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    if (url.includes('/emotion/state')) {
      if ((init?.method || 'GET') === 'DELETE') {
        erased.push(url)
        return json({ ok: true, deleted: 1 })
      }
      return json({
        baseline: {
          tone_default: '平和稳重，少说多听',
          baseline_emotion: '平静',
          trigger_topics: ['妈妈的身体和睡眠'],
          landmines: ['打趣她妈妈养花'],
          comfort_style: '别急着给办法',
        },
        baseline_line: '平时用平和稳重的语气接她',
        baseline_age_seconds: 42,
        revision: 1,
        plans: [
          {
            topic: '家人',
            data: {
              say: '你妈最近睡眠好点了吗',
              avoid: ['打趣她妈妈养花的事'],
              tone: '轻一点',
              followup: '这周还加班吗',
            },
            expires_at: Date.now() / 1000 + 600,
            expired: false,
          },
          {
            topic: '过期主题',
            data: { say: '这句不该再出现', avoid: [], tone: '', followup: '' },
            expires_at: Date.now() / 1000 - 60,
            expired: true,
          },
        ],
        stats: { baseline_present: true, plans: 2, plans_live: 1, daily_baseline: 1 },
        agreement: { samples: 3, rate: 0.667 },
      })
    }
    if (url.includes('/detail')) return json({ layers: { core: [], important: [], regular: [] } })
    if (url.includes('/profile')) return json({ profile: null })
    return json({ summaries: [] })
  }) as typeof fetch
})

afterAll(() => {
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

afterEach(() => {
  cleanup()
  confirms.length = 0
  erased = []
})

describe('「她猜的」这一页', () => {
  it('把相处总结和临时对策摊开给用户核对，过期的不出现', async () => {
    render(<MemoryViewerModal onClose={() => {}} />)
    fireEvent.click(await screen.findByText('她猜的'))
    expect(await screen.findByText('先提：你妈最近睡眠好点了吗')).toBeTruthy()
    expect(screen.getByText('避开：打趣她妈妈养花的事')).toBeTruthy()
    expect(screen.getByText('语气轻一点')).toBeTruthy()
    expect(screen.getByText(/分钟内有效/)).toBeTruthy()
    expect(screen.getByText('易被妈妈的身体和睡眠牵着')).toBeTruthy()
    // 过期那条不许混进来
    expect(screen.queryByText('先提：这句不该再出现')).toBeNull()
  })

  it('撤掉对策先问一句，且明确说这是猜的', async () => {
    render(<MemoryViewerModal onClose={() => {}} />)
    fireEvent.click(await screen.findByText('她猜的'))
    fireEvent.click(await screen.findByTitle('这次猜得不对，划掉'))
    expect(confirms.length).toBe(1)
    expect(confirms[0]).toContain('猜')
    await waitFor(() => expect(erased.length).toBe(1))
    expect(erased[0]).toContain('kind=plan')
    expect(await screen.findByText(/对策撤掉了/)).toBeTruthy()
  })
})
