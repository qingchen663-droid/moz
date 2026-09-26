import { describe, it, expect, beforeAll, afterAll, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import App from '../App'
import { useStore } from '../store'

const originalFetch = globalThis.fetch

function mockResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

let multimodalFlag = false

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {}
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    if (url.includes('/health')) return mockResponse({ status: 'ok', auth_required: false })
    if (url.includes('/care/pending')) return mockResponse({ items: [] })
    if (url.includes('/care/settings')) return mockResponse({
      enabled: true, province: '', city: '', quiet_start: '23:00', quiet_end: '08:00',
      talk_mode: 'auto', talk_score: 0.5, rain_reminder: true,
    })
    if (url.includes('/care/items')) return mockResponse({ items: [] })
    if (url.includes('/users')) return mockResponse([])
    if (url.includes('/conversations')) return mockResponse({ conversations: [], current_id: '' })
    if (url.includes('/memory')) return mockResponse({ total: 3, avg_importance: 0.4, consolidated_count: 0 })
    if (url.includes('/config/model-presets')) return mockResponse({ DeepSeek: { model: 'deepseek-chat', base_url: 'x', use_thinking: true } })
    if (url.includes('/config/model')) return mockResponse({
      model: 'deepseek-chat', base_url: 'https://api.deepseek.com/v1',
      multimodal: multimodalFlag, multimodal_declared: false, use_thinking: false,
    })
    if (url.includes('/avatar/')) return mockResponse({}, 404)
    return mockResponse({})
  }) as typeof fetch
})

afterAll(() => {
  globalThis.fetch = originalFetch
})

beforeEach(() => {
  multimodalFlag = false
  cleanup()
  useStore.setState({
    userId: 'web_user_001', avatar: null, conversations: [], currentConvId: null, messages: [],
    memoryStats: { total: 3, avg_importance: 0.4, consolidated_count: 0 },
    modelConfig: null, promptConfig: null, isLoading: false, authRequired: false, isAuthenticated: true,
  })
})

const railLabels = () =>
  Array.from(document.querySelectorAll('.rail-label')).map((e) => e.textContent?.trim())

describe('产品形态回归：单条长期陪伴流', () => {
  it('侧栏只有四个入口，且没有会话/用户管理', async () => {
    render(<App />)
    await waitFor(() => expect(railLabels()).toEqual(['人设', '认知', '设置', '模型']))
    expect(document.body.textContent).not.toContain('开启新对话')
    expect(document.body.textContent).not.toContain('添加用户')
    expect(document.body.textContent).not.toContain('当前用户')
    expect(document.querySelector('.user-section')).toBeNull()
    expect(document.querySelector('.conv-list')).toBeNull()
  })

  it('聊天头部显示长期记忆，不显示会话标题', async () => {
    render(<App />)
    await waitFor(() => expect(document.querySelector('.chat-header-subtitle')?.textContent).toContain('长期记忆'))
    expect(document.querySelector('.chat-header-title')?.textContent).toBe('moz')
    expect(document.body.textContent).not.toContain('新对话')
  })

  it('设置面板分四组，且不含手动录入表单', async () => {
    render(<App />)
    await waitFor(() => expect(document.querySelector('.rail-item')).toBeTruthy())
    fireEvent.click(screen.getByText('设置'))
    await waitFor(() => expect(document.querySelector('.me-panel')).toBeTruthy())
    const groups = Array.from(document.querySelectorAll('.me-group-label')).map((e) => e.textContent?.trim())
    expect(groups).toEqual(['状态', '主动关心', '数据与维护', '历史对话'])
    // 提醒事项必须自动记录，不允许出现手动填表
    expect(document.querySelector('.care-add')).toBeNull()
    expect(Array.from(document.querySelectorAll('button')).some((b) => b.textContent?.trim() === '记下')).toBe(false)
    expect(document.querySelector('.care-empty')?.textContent).toContain('我会自己记下来')
  })

  it('确认框必须带卡片与按钮样式类（曾因样式在孤儿文件里而裸奔）', async () => {
    render(<App />)
    await waitFor(() => expect(document.querySelector('.rail-item')).toBeTruthy())
    fireEvent.click(screen.getByText('设置'))
    await waitFor(() => expect(document.querySelector('.me-panel')).toBeTruthy())
    const clear = Array.from(document.querySelectorAll('.me-row')).find((r) => r.textContent?.includes('清空记忆'))
    expect(clear).toBeTruthy()
    fireEvent.click(clear!)
    await waitFor(() => expect(document.querySelector('.confirm-dialog')).toBeTruthy())
    const card = document.querySelector('.confirm-dialog')!
    expect(card.className).toContain('dialog-content')
    const btns = Array.from(card.querySelectorAll('button')).map((b) => b.className)
    expect(btns.some((c) => c.includes('dialog-btn--danger'))).toBe(true)
    expect(btns.some((c) => c.includes('dialog-btn--secondary'))).toBe(true)
  })
})

describe('图片上传入口', () => {
  it('模型未声明能看图时上传按钮仍在（只是低调态）', async () => {
    render(<App />)
    await waitFor(() => expect(document.querySelector('.chat-input-attach')).toBeTruthy())
    expect(document.querySelector('.chat-input-attach')?.className).toContain('chat-input-attach--muted')
  })
})

describe('主动消息接收', () => {
  it('把待读主动消息落进对话并 ack', async () => {
    let acked: string[] = []
    const base = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.includes('/care/pending')) {
        return mockResponse({ items: [{ id: 'p1', user_id: 'web_user_001', kind: 'event', text: '今天面试加油', created_at: 1, acked_at: null }] })
      }
      if (url.includes('/care/ack')) {
        acked = JSON.parse(String(init?.body ?? '{}')).ids
        return mockResponse({ ok: true, acked: 1, ids: ['p1'] })
      }
      return base(input as RequestInfo)
    }) as typeof fetch
    try {
      await useStore.getState().pullProactive()
      await waitFor(() => expect(acked).toEqual(['p1']))
      const msgs = useStore.getState().messages
      expect(msgs.some((m) => m.role === 'assistant' && m.content === '今天面试加油')).toBe(true)
    } finally {
      globalThis.fetch = base
    }
  })

  it('正在流式回复时不插主动消息', async () => {
    useStore.setState({ isLoading: true })
    const before = useStore.getState().messages.length
    await useStore.getState().pullProactive()
    expect(useStore.getState().messages.length).toBe(before)
  })
})
