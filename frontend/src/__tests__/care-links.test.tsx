/// <reference types="vitest/globals" />
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import CareSection from '../components/CareSection'
import { useStore } from '../store'

const originalFetch = globalThis.fetch

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const ITEM = {
  id: 'i-1',
  user_id: 'web_user_001',
  kind: 'event',
  title: '项目答辩',
  detail: '',
  due_at: 1790000000,
  repeat: 'none',
  status: 'active',
  source: 'auto',
  created_at: 0,
  updated_at: 0,
  last_fired_at: 0,
}

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {}
  useStore.setState({ userId: 'web_user_001' })
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    if (url.includes('/care/settings'))
      return json({
        enabled: true,
        remind_events: true,
        initiate_chat: true,
        province: '',
        city: '',
        quiet_start: '23:00',
        quiet_end: '08:00',
        talk_mode: 'auto',
        talk_score: 0.5,
        budget_today: 2,
        rain_reminder: true,
      })
    if (url.includes('/care/related'))
      return json({
        related: [
          { type: 'memory', id: 'm-9', label: '用户的妈妈喜欢养花', kind: 'fact', via: 'person:妈妈', score: 0.61 },
        ],
      })
    if (url.includes('/care/graph'))
      return json({
        edges: [
          {
            src_type: 'item', src_id: 'i-2', rel: 'after', dst_type: 'item', dst_id: 'i-1',
            weight: 0.8, label: '答辩出结果', kind: 'event', offset_days: 7, source: 'llm',
          },
        ],
        hubs: {},
      })
    if (url.includes('/care/items')) return json({ items: [ITEM] })
    return json({})
  }) as typeof fetch
})

afterAll(() => {
  globalThis.fetch = originalFetch
})

afterEach(() => {
  cleanup()
})

describe('关心事项上的"这件事还连着"', () => {
  it('把关联事实和先后链一起显示在事项下面', async () => {
    render(<CareSection />)
    await waitFor(() => expect(screen.getByText('项目答辩')).toBeTruthy())
    expect(screen.getByText('这件事还连着')).toBeTruthy()
    expect(await screen.findByText(/关于妈妈 · 用户的妈妈喜欢养花/)).toBeTruthy()
    expect(await screen.findByText(/7 天后 答辩出结果 有下文/)).toBeTruthy()
  })
})
