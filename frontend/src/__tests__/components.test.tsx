import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest'
import { render } from '@testing-library/react'
import App from '../App'

/** 假时间推进要包在 act 里，否则 React 的状态更新会报"未包在 act() 里" */
async function actAsync(fn: () => Promise<void> | void) {
  const { act } = await import('react')
  await act(async () => {
    await fn()
  })
}

const originalFetch = globalThis.fetch

function mockResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {}
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url =
      typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    if (url.includes('/health')) {
      return mockResponse({ status: 'ok', auth_required: false, admin_key_configured: false })
    }
    if (url.includes('/users')) {
      return mockResponse([])
    }
    if (url.includes('/conversations')) {
      return mockResponse({ conversations: [], current_id: '' })
    }
    if (url.includes('/memory')) {
      return mockResponse({ total: 0, avg_importance: 0, consolidated_count: 0 })
    }
    if (url.includes('/config/model')) {
      return mockResponse({ model: 'test', base_url: '', multimodal: false })
    }
    return mockResponse({})
  }) as typeof fetch
})

afterAll(() => {
  globalThis.fetch = originalFetch
})

describe('App', () => {
  it('should render without crashing', () => {
    const { container } = render(<App />)
    expect(container).toBeTruthy()
  })
})

describe('等得久时界面要说一句人话', () => {
  it('秒数换算：40 秒说秒，过了 60 秒说分', async () => {
    const { waitedLabel, waitNotice, WAIT_NOTICE_AFTER } = await import('../components/ChatArea')
    expect(WAIT_NOTICE_AFTER).toBe(40)
    expect(waitedLabel(45)).toBe('45 秒')
    expect(waitedLabel(95)).toBe('1 分 35 秒')
    expect(waitNotice(95)).toContain('这句她还在想（已经 1 分 35 秒）')
    // 三种可能都被误读成"坏了"，所以要说清是慢不是错，并给出出口
    expect(waitNotice(95)).toContain('不是她没听懂')
    expect(waitNotice(95)).toContain('停止生成')
    // 3 分钟以上别再让人干等
    expect(waitNotice(200)).toContain('多半是中转卡住了')
    expect(waitNotice(200)).toContain('重发一句')
  })

  it('刚发出去 10 秒别啰嗦，满 40 秒才解释', async () => {
    const { default: ChatArea } = await import('../components/ChatArea')
    const { useStore } = await import('../store')
    vi.useFakeTimers()
    try {
      useStore.setState({ isLoading: true, statusText: '', messages: [], error: null })
      render(<ChatArea />)
      await actAsync(async () => { await vi.advanceTimersByTimeAsync(10_000) })
      expect(document.querySelector('.chat-waiting-note')).toBeFalsy()
      await actAsync(async () => { await vi.advanceTimersByTimeAsync(35_000) })
      const note = document.querySelector('.chat-waiting-note')
      expect(note?.textContent).toContain('这句她还在想')
      expect(note?.textContent).toContain('45 秒')
      // 后端已经报了阶段进度（"上一条还在收尾"那种）就别叠一句
      useStore.setState({ statusText: '上一条还在收尾，等一下' })
      await actAsync(async () => { await vi.advanceTimersByTimeAsync(1_000) })
      expect(document.querySelector('.chat-waiting-note')).toBeFalsy()
    } finally {
      vi.useRealTimers()
      useStore.setState({ isLoading: false, statusText: '' })
    }
  })
})
