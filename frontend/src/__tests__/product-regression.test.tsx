import { describe, it, expect, beforeAll, afterAll, beforeEach, vi } from 'vitest'
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

const careSettings = () => ({
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

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {}
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    if (url.includes('/health')) return mockResponse({ status: 'ok', auth_required: false })
    if (url.includes('/care/pending')) return mockResponse({ items: [] })
    if (url.includes('/care/settings')) return mockResponse(careSettings())
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
    await waitFor(() => expect(railLabels()).toEqual(['人设', '记忆', '设置', '模型']))
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
  it('到点提醒和平时搭话是两个独立开关，且不摆内部指标', async () => {
    render(<App />)
    await waitFor(() => expect(document.querySelector('.rail-item')).toBeTruthy())
    fireEvent.click(screen.getByText('设置'))
    await waitFor(() => expect(document.querySelector('.care-section')).toBeTruthy())
    const text = document.querySelector('.care-section')!.textContent!
    expect(text).toContain('到点提醒我')
    expect(text).toContain('没来由地找我说话')
    expect(document.querySelectorAll('.care-switch input').length).toBe(3)
    // talk_score 百分比这种内部数字不该出现在界面上，用户看得懂的是"一天最多几条"
    expect(text).not.toMatch(/自动判断值|%.*慢慢调/)
    expect(text).toMatch(/一天最多 2 条/)
  })
})

describe('一个东西只许有一个名字', () => {
  it('侧栏和弹窗不再出现"认知""人设提示词""AI 情感伴侣"', async () => {
    const base = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.includes('/config/prompt'))
        return mockResponse({ prompt: '你是一个安静的听众', default_prompt: '默认人设', is_custom: false })
      return base(input as RequestInfo)
    }) as typeof fetch
    try {
      render(<App />)
      await waitFor(() => expect(railLabels()).toEqual(['人设', '记忆', '设置', '模型']))
      expect(document.body.textContent).not.toContain('认知')

      fireEvent.click(screen.getByText('人设'))
      await waitFor(() => expect(document.querySelector('.prompt-dialog-preview-text')?.textContent).toContain('安静的听众'))
      const view = document.querySelector('.prompt-dialog')!.textContent!
      expect(view).not.toMatch(/情感伴侣|提示词/)
      expect(view).toContain('人设')

      fireEvent.click(screen.getByText('自定义人设'))
      await waitFor(() => expect(document.querySelector('.prompt-dialog-editor')).toBeTruthy())
      const editor = document.querySelector('.prompt-dialog-editor')!.textContent!
      expect(editor).toContain('编辑人设')
      expect(editor).not.toMatch(/提示词|AI 伴侣/)
    } finally {
      globalThis.fetch = base
    }
  })
})

describe('冷启动：第一眼得知道下一步干什么', () => {
  const welcome = () => document.querySelector('.chat-welcome')?.textContent ?? ''

  it('一条记忆都没有时，给一句能马上照着试的话', async () => {
    const base = globalThis.fetch
    // App 挂载时会自己拉记忆统计，只改 store 会被覆盖回去
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.includes('/memory')) return mockResponse({ total: 0, avg_importance: 0, consolidated_count: 0 })
      if (url.includes('/care/pending')) return mockResponse({ items: [] })
      return base(input as RequestInfo)
    }) as typeof fetch
    try {
      render(<App />)
      await waitFor(() => expect(document.querySelector('.chat-welcome')).toBeTruthy())
      await waitFor(() => expect(welcome()).toContain('想先试一下'))
      expect(welcome()).toContain('记住，我妈生日是 10 月 5 日')
      expect(welcome()).toContain('「记忆」')
      expect(welcome()).not.toMatch(/层级|巩固|talk_score|检索/)
    } finally {
      globalThis.fetch = base
    }
  })

  it('已经记过东西的老用户，不用再被手把手教', async () => {
    render(<App />)
    await waitFor(() => expect(document.querySelector('.chat-welcome')).toBeTruthy())
    expect(welcome()).not.toContain('想先试一下')
  })

  it('统计还没读回来时别先喊"0 条"、也别当成新用户', async () => {
    const base = globalThis.fetch
    // 让 /memory 一直不返回，模拟"刚打开、统计还在路上"
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.includes('/memory')) return new Promise(() => {})
      return base(input as RequestInfo)
    }) as typeof fetch
    try {
      useStore.setState({ messages: [], memoryStats: null })
      render(<App />)
      await waitFor(() => expect(document.querySelector('.chat-welcome')).toBeTruthy())
      expect(document.querySelector('.chat-header-subtitle')?.textContent).toBe('长期记忆')
      expect(welcome()).not.toContain('想先试一下')
    } finally {
      globalThis.fetch = base
    }
  })
})

describe('人设弹窗：读不到要说清楚，没保存要问一句', () => {
  it('后端没给人设时不写"加载中..."，而是给原因和重试', async () => {
    const { default: PromptDialog } = await import('../components/PromptDialog')
    render(<PromptDialog onClose={() => {}} />)
    await waitFor(() => expect(document.querySelector('.prompt-dialog-load')).toBeTruthy())
    const dialog = document.querySelector('.prompt-dialog')!
    expect(dialog.textContent).not.toContain('加载中')
    expect(document.querySelector('.prompt-dialog-load')!.textContent).toContain('再试一次')
    expect(dialog.querySelector<HTMLButtonElement>('.prompt-dialog-actions .dialog-btn--primary')?.disabled).toBe(true)
  })

  it('改了人设没保存就点取消，会先问；说"不丢"就留在编辑里', async () => {
    const base = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.includes('/config/prompt'))
        return mockResponse({ prompt: '你是一个温柔的听众', default_prompt: '默认人设', is_custom: true })
      return base(input as RequestInfo)
    }) as typeof fetch
    const asked: string[] = []
    const confirmSpy = vi.spyOn(window, 'confirm').mockImplementation((msg) => {
      asked.push(String(msg))
      return asked.length > 1 // 第一次拒绝，第二次同意
    })
    try {
      const { default: PromptDialog } = await import('../components/PromptDialog')
      render(<PromptDialog onClose={() => {}} />)
      await waitFor(() => expect(document.querySelector('.prompt-dialog-preview-text')?.textContent).toContain('温柔的听众'))
      fireEvent.click(screen.getByText('修改人设'))
      const ta = document.querySelector('.prompt-dialog-textarea') as HTMLTextAreaElement
      fireEvent.change(ta, { target: { value: '你是一个温柔的听众，但少说教' } })
      fireEvent.click(screen.getByText('取消'))
      expect(asked).toHaveLength(1)
      expect(asked[0]).toContain('还没保存')
      expect(document.querySelector('.prompt-dialog-textarea')).toBeTruthy()
      fireEvent.click(screen.getByText('取消'))
      expect(asked).toHaveLength(2)
      expect(document.querySelector('.prompt-dialog-textarea')).toBeFalsy()
    } finally {
      confirmSpy.mockRestore()
      globalThis.fetch = base
    }
  })
})

describe('弹窗基本规矩：认得出自己是什么，Esc 关得掉', () => {
  type DialogCase = [string, React.ComponentType<any>, Record<string, unknown>]
  const cases = async (): Promise<DialogCase[]> => {
    const [ModelDialog, PromptDialog, ConfirmDialog, Memory, Logs, MePanel] = await Promise.all([
      import('../components/ModelDialog'),
      import('../components/PromptDialog'),
      import('../components/ConfirmDialog'),
      import('../components/MemoryViewerModal'),
      import('../components/LogViewerModal'),
      import('../components/MePanel'),
    ])
    return [
      ['模型', ModelDialog.default, {}],
      ['人设', PromptDialog.default, {}],
      ['确认', ConfirmDialog.default, { title: '确认框', message: 'x', onConfirm: () => {} }],
      ['记忆', Memory.default, {}],
      ['日志', Logs.default, {}],
      ['设置', MePanel.default, {}],
    ]
  }

  it('六个弹窗都有 role/aria-label，且按 Esc 会关', async () => {
    for (const [name, Comp, extra] of await cases()) {
      cleanup()
      const onClose = vi.fn()
      const { container } = render(<Comp onClose={onClose} onCancel={onClose} {...extra} />)
      const box = container.querySelector<HTMLElement>('[role]')
      expect(box, `${name}弹窗没有 role`).toBeTruthy()
      expect(box!.getAttribute('aria-label'), `${name}弹窗没有 aria-label`).toBeTruthy()
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(onClose, `${name}弹窗按 Esc 不关`).toHaveBeenCalled()
    }
  })
})

describe('崩了之后', () => {
  it('要说清"数据不会丢"，按钮必须真的重新载入，不许写"联系开发者"', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = vi.fn()
    // jsdom 的 location.reload 改不动（unforgeable），只能整个换掉
    vi.stubGlobal('location', { ...window.location, reload })
    const Boom = () => {
      throw new Error('渲染期崩溃')
    }
    try {
      const { default: ErrorBoundary } = await import('../components/ErrorBoundary')
      render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>
      )
      const text = document.body.textContent || ''
      expect(text).toContain('重新载入')
      expect(text).toContain('不会')
      expect(text).not.toContain('联系开发者')
      fireEvent.click(screen.getByText('重新载入'))
      expect(reload, '按钮没真的重新载入').toHaveBeenCalledTimes(1)
    } finally {
      vi.unstubAllGlobals()
      errSpy.mockRestore()
      cleanup()
    }
  })
})

describe('后端没开的时候', () => {
  it('第一屏要说清"怎么重新打开"，并且不许甩未捕获的异常', async () => {
    const base = globalThis.fetch
    globalThis.fetch = (async () => {
      throw new TypeError('Failed to fetch')
    }) as typeof fetch
    try {
      render(<App />)
      await waitFor(() => expect(document.querySelector('.chat-error-banner')).toBeTruthy())
      const text = document.querySelector('.chat-error-banner')!.textContent || ''
      expect(text).toContain('moz-app.bat')
      expect(text).not.toMatch(/undefined|\[object/)
      // 等一轮主动消息轮询，确认挂掉的请求都各自兜住了
      await useStore.getState().pullProactive()
      await useStore.getState().loadUsers().catch(() => {})
    } finally {
      globalThis.fetch = base
    }
  })
})

describe('我存的模型', () => {
  it('列得出来、点得回去，界面上不出现密钥', async () => {
    const base = globalThis.fetch
    const posts: string[] = []
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      posts.push(`${init?.method ?? 'GET'} ${url}`)
      if (url.includes('/config/saved-models'))
        return mockResponse({
          max: 12,
          items: [
            {
              name: '中转 deepseek', model: 'deepseek-v4.1-flash', base_url: 'https://x.example/v1',
              use_thinking: true, multimodal: true, has_key: true, saved_at: 1, is_active: false,
            },
            {
              name: '现在这套', model: 'glm-5.1', base_url: 'https://y.example/v1',
              use_thinking: false, multimodal: null, has_key: true, saved_at: 2, is_active: true,
            },
          ],
        })
      return base(input as RequestInfo)
    }) as typeof fetch
    try {
      const { default: ModelDialog } = await import('../components/ModelDialog')
      render(<ModelDialog onClose={() => {}} />)
      await waitFor(() => expect(document.querySelectorAll('.saved-model').length).toBe(2))
      const body = document.querySelector('.model-dialog')!.textContent!
      expect(body).toContain('中转 deepseek')
      expect(body).toContain('使用中')
      expect(body).not.toMatch(/sk-[A-Za-z0-9]|\*{4,}/)
      // 正在用的那套不该再给"用这个"
      expect(document.querySelectorAll('.saved-model-btn').length).toBe(1)
      fireEvent.click(document.querySelector('.saved-model-btn')!)
      await waitFor(() => expect(posts.some((p) => p.includes('saved-models/use'))).toBe(true))
    } finally {
      globalThis.fetch = base
    }
  })
})

describe('图片上传入口', () => {
  it('模型未声明能看图时上传按钮仍在（只是低调态）', async () => {
    render(<App />)
    await waitFor(() => expect(document.querySelector('.chat-input-attach')).toBeTruthy())
    expect(document.querySelector('.chat-input-attach')?.className).toContain('chat-input-attach--muted')
  })

  it('勾了多模态也不承诺看得清：中转真的会丢图', async () => {
    multimodalFlag = true
    render(<App />)
    await waitFor(() => expect(document.querySelector('.chat-input-attach')).toBeTruthy())
    const btn = document.querySelector<HTMLElement>('.chat-input-attach')!
    expect(btn.className).not.toContain('muted')
    expect(btn.title).not.toContain('我会看图内容')
    expect(btn.title).toContain('不一定准')
  })

  it('会话列表接口返回半截响应也不会弄崩应用', async () => {
    const base = globalThis.fetch
    globalThis.fetch = (async () => mockResponse({})) as typeof fetch
    try {
      await useStore.getState().loadConversations()
    } finally {
      globalThis.fetch = base
    }
    expect(useStore.getState().conversations).toEqual([])
    expect(useStore.getState().currentConvId).toBeNull()
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

describe('记忆可读可改：能筛选、能纠错', () => {
  const memory = (id: string, content: string, layer: string) => ({
    id,
    content,
    layer,
    category: 'fact',
    emotion: 'neutral',
    emotion_emoji: '😐',
    tags: ['工作'],
    importance: 0.7,
    access_count: 2,
    is_consolidated: false,
    created_at: 1760000000,
    temporal_data: {},
  })

  async function openViewer(layers: Record<string, unknown[]>) {
    const { default: MemoryViewerModal } = await import('../components/MemoryViewerModal')
    const fetches: string[] = []
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input)
      fetches.push(url)
      if (url.includes('/detail')) return mockResponse({ layers })
      if (url.includes('/profile/')) return mockResponse({ profile: null })
      if (url.includes('/summaries')) return mockResponse({ summaries: [] })
      return mockResponse({})
    }) as typeof fetch
    render(<MemoryViewerModal onClose={() => {}} />)
    await waitFor(() => expect(document.querySelector('.mem-viewer-tabs')).toBeTruthy())
    return fetches
  }

  it('三条分层记忆都有筛选、搜索和纠正入口', async () => {
    await openViewer({
      core: [memory('c1', '妈妈生日是 10 月 5 日', 'core')],
      important: [memory('i1', '正在换工作，面试在下周', 'important')],
      regular: [memory('r1', '喜欢喝美式', 'regular')],
    })
    fireEvent.click(screen.getByText('记忆'))
    expect(document.querySelector('.mem-search')).toBeTruthy()
    expect(document.querySelectorAll('.mem-filter').length).toBe(4)
    expect(document.querySelectorAll('.mem-card').length).toBe(3)
    // 记错了要能当场处理，而不是只能看着
    expect(screen.getAllByText('忘掉').length).toBe(3)
    expect(screen.getAllByText('不对').length).toBe(3)
  })

  it('搜索按内容过滤，筛完没命中时给的是可操作的话', async () => {
    await openViewer({ core: [memory('c1', '妈妈生日是 10 月 5 日', 'core')], important: [], regular: [] })
    fireEvent.click(screen.getByText('记忆'))
    fireEvent.change(document.querySelector('.mem-search')!, { target: { value: '美式' } })
    expect(document.querySelectorAll('.mem-card').length).toBe(0)
    expect(document.querySelector('.mem-empty')?.textContent).toContain('换个词')
  })

  it('一条记忆都没有时，空态要告诉用户怎么让它记住', async () => {
    await openViewer({ core: [], important: [], regular: [] })
    fireEvent.click(screen.getByText('记忆'))
    expect(document.querySelector('.mem-empty')?.textContent).toContain('记住')
  })

  it('记忆卡片不摆内部指标：检索次数、重要性百分比、已巩固', async () => {
    await openViewer({
      core: [
        {
          ...memory('c1', '妈妈生日是 10 月 5 日', 'core'),
          access_count: 12,
          importance: 0.87,
          is_consolidated: true,
        },
      ],
      important: [],
      regular: [],
    })
    fireEvent.click(screen.getByText('记忆'))
    const card = document.querySelector('.mem-card')!
    expect(card.textContent).not.toMatch(/检索|已巩固|重要性|%/)
    expect(card.textContent).toContain('妈妈生日是 10 月 5 日')
  })
})

describe('仓库杂物', () => {
  // tsconfig 没装 @types/node；.tsx 能按文本 glob 到，.css 只能拿到文件名（Vite 会吞掉 ?raw）
  const compFiles = import.meta.glob('../components/*', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>

  it('不留 .bak / .timestamp / 没人 import 的孤儿样式', () => {
    const junk = Object.keys(compFiles).filter((f) => /\.bak$|\.timestamp$|\.new\.css$/.test(f))
    expect(junk).toEqual([])
    const tsx = Object.entries(compFiles).filter(([f]) => f.endsWith('.tsx'))
    expect(tsx.length).toBeGreaterThan(5)
    for (const file of Object.keys(compFiles)) {
      if (!file.endsWith('.css')) continue
      const name = file.split('/').pop()!
      expect(tsx.some(([, src]) => src.includes(name)), `${name} 没人 import，是孤儿样式`).toBe(true)
    }
  })
})

describe('危险操作先问再做', () => {
  it('选完快照文件不立刻覆盖，确认之后才发 import 请求', async () => {
    const posts: string[] = []
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input)
      posts.push(`${init?.method ?? 'GET'} ${url}`)
      if (url.includes('/import/')) return mockResponse({ status: 'ok', memories_imported: 2, care_items_imported: 1 })
      if (url.includes('/care/settings')) return mockResponse(careSettings())
      if (url.includes('/care/items')) return mockResponse({ items: [] })
      return mockResponse({})
    }) as typeof fetch

    const { default: MePanel } = await import('../components/MePanel')
    render(<MePanel onClose={() => {}} />)
    const input = document.querySelector('input[type=file]')!
    const file = new File([JSON.stringify({ conversations: { a: { messages: [] } }, memories: [] })], 'moz_export.json', {
      type: 'application/json',
    })
    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() => expect(document.querySelector('.confirm-dialog')).toBeTruthy())
    expect(posts.filter((p) => p.includes('/import/'))).toHaveLength(0)
    expect(document.querySelector('.dialog-message')?.textContent).toContain('会被替掉')

    fireEvent.click(screen.getByText('覆盖恢复'))
    await waitFor(() => expect(posts.some((p) => p.includes('/import/'))).toBe(true))
  })

  it('不像 moz 快照的文件直接被拦下，不弹覆盖确认', async () => {
    const { default: MePanel } = await import('../components/MePanel')
    render(<MePanel onClose={() => {}} />)
    const input = document.querySelector('input[type=file]')!
    fireEvent.change(input, { target: { files: [new File(['{"nope":1}'], 'other.json')] } })
    await waitFor(() => expect(document.querySelector('.me-notice')?.textContent).toContain('不像 moz 的快照'))
    expect(document.querySelector('.confirm-dialog')).toBeFalsy()
  })
})

describe('后端结构变化不能弄崩应用', () => {
  it('工作话题是结构化对象时，记忆界面照样打得开', async () => {
    const { default: MemoryViewerModal } = await import('../components/MemoryViewerModal')
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/detail'))
        return mockResponse({
          layers: { core: [], important: [], regular: [] },
          working_memory: {
            summary: '在换工作',
            // 真实形状：OpenLoop 对象，不是字符串
            open_topics: [
              { id: 'a1', topic: '下周面试', status: 'waiting', due_at: 0, created_at: 0 },
              '老数据：纯字符串',
            ],
          },
        })
      if (url.includes('/profile/')) return mockResponse({ profile: null })
      return mockResponse({ summaries: [] })
    }) as typeof fetch

    render(<MemoryViewerModal onClose={() => {}} />)
    await waitFor(() => expect(document.querySelector('.mem-working-bar')).toBeTruthy())
    const tags = [...document.querySelectorAll('.mem-topic-tag')].map((e) => e.textContent?.trim())
    expect(tags).toEqual(['下周面试', '老数据：纯字符串'])
  })
})

describe('「moz 记得什么」里的措辞', () => {
  // 三条正文照抄第十轮沙箱里真实存下来的样子
  const row = (id: string, content: string) => ({
    id,
    content,
    layer: 'regular',
    category: 'fact',
    emotion: 'neutral',
    emotion_emoji: '😐',
    tags: [],
    importance: 0.6,
    access_count: 0,
    is_consolidated: false,
    created_at: 1760000000,
    temporal_data: {},
  })

  it('内部分类标记不摊给用户看，来路改成人话', async () => {
    const { default: MemoryViewerModal } = await import('../components/MemoryViewerModal')
    const base = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/detail'))
        return mockResponse({
          layers: {
            core: [],
            important: [],
            regular: [
              row('m1', '[关于用户] 用户的妈妈喜欢养花'),
              row('m2', '[对话摘要] AI回复要点：团子呀，五岁的橘猫'),
              row('m3', '[对话摘要] 用户说：我下周三下午两点要去做项目答辩'),
              row('m4', '[关于用户] 我讨厌吃香菜'),
            ],
          },
        })
      if (url.includes('/profile/')) return mockResponse({ profile: null })
      return mockResponse({ summaries: [] })
    }) as typeof fetch

    try {
      render(<MemoryViewerModal onClose={() => {}} />)
      await waitFor(() => expect(document.querySelector('.mem-viewer-tabs')).toBeTruthy())
      fireEvent.click(screen.getByText('记忆'))
      await waitFor(() => expect(document.querySelectorAll('.mem-card').length).toBe(4))
      const body = document.body.textContent || ''
      expect(body).not.toContain('[关于用户]')
      expect(body).not.toContain('[对话摘要]')
      expect(body).not.toContain('AI回复要点：')
      expect(body).toContain('你的妈妈喜欢养花')   // 第三称的"用户"改成"你"
      expect(body).toContain('你讨厌吃香菜')       // 第一人称的"我"也得改：那页是她的记忆
      expect(body).not.toContain('我讨厌吃香菜')   // 否则读起来像她自己不吃香菜
      expect(body).toContain('她自己的话')
      expect(body).toContain('那次聊天里你说的')
    } finally {
      globalThis.fetch = base
    }
  })
})

describe('档案卡上能把记错的划掉', () => {
  const PROFILE = {
    identity: { name: '小明', occupation: '程序员' },
    preferences: { hobbies: ['编程', '旅行'], food: ['美式'] },
    relationships: { family: [{ relation: '妈妈', description: '喜欢养花' }] },
    emotional_profile: { recent_mood_trend: '还行' },
  }

  async function openProfile() {
    const { default: MemoryViewerModal } = await import('../components/MemoryViewerModal')
    const puts: Array<{ url: string; method?: string; body: unknown }> = []
    const base = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input)
      if (init?.method === 'PUT') {
        puts.push({ url, method: init.method, body: JSON.parse(String(init.body)) })
        return mockResponse({ ok: true, updated_fields: ['preferences'], version: 2 })
      }
      if (url.includes('/profile/'))
        return mockResponse({ user_id: 'web_user_001', profile: PROFILE, prompt_context: '', version: 1 })
      if (url.includes('/detail'))
        return mockResponse({ layers: { core: [], important: [], regular: [] } })
      if (url.includes('/summaries')) return mockResponse({ summaries: [] })
      return base(input as RequestInfo | URL, init)
    }) as typeof fetch
    render(<MemoryViewerModal onClose={() => {}} />)
    await waitFor(() => expect(document.querySelectorAll('.mem-tag-x').length).toBeGreaterThan(0))
    // 划掉有确认框（jsdom 里 window.confirm 默认返回 undefined = 取消），测试里默认答"确定"
    const spy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    return {
      puts,
      cleanupFetch: () => {
        spy.mockRestore()
        globalThis.fetch = base
      },
    }
  }

  it('划掉之前先问一句，取消就一条请求都不发（PUT 是整段替换、没有撤销）', async () => {
    const { puts, cleanupFetch } = await openProfile()
    let asked = ''
    const spy = vi.spyOn(window, 'confirm').mockImplementation((m) => {
      asked = String(m)
      return false
    })
    const hobby = [...document.querySelectorAll('.mem-pref-tag')].filter((e) =>
      e.textContent?.includes('旅行')
    )[0].querySelector('.mem-tag-x')!
    fireEvent.click(hobby)
    expect(puts).toHaveLength(0)
    expect(asked).toContain('旅行')          // 确认框里得看清划掉的是哪一条
    expect(document.body.textContent).toContain('旅行')
    spy.mockRestore()
    cleanupFetch()
  })

  it('后端答了话就别赖它"没应答"：403 的原因要念给用户看', async () => {
    const { default: MemoryViewerModal } = await import('../components/MemoryViewerModal')
    const base = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input)
      if (init?.method === 'PUT')
        return mockResponse({ detail: '用户不在允许列表中' }, 403)
      if (url.includes('/profile/'))
        return mockResponse({ user_id: 'web_user_001', profile: PROFILE, prompt_context: '', version: 1 })
      if (url.includes('/detail')) return mockResponse({ layers: { core: [], important: [], regular: [] } })
      if (url.includes('/summaries')) return mockResponse({ summaries: [] })
      return base(input as RequestInfo | URL, init)
    }) as typeof fetch
    const spy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      render(<MemoryViewerModal onClose={() => {}} />)
      await waitFor(() =>
        expect([...document.querySelectorAll('.mem-pref-tag')].some((e) =>
          e.textContent?.includes('旅行')
        )).toBe(true)
      )
      const hobby = [...document.querySelectorAll('.mem-pref-tag')].find((e) =>
        e.textContent?.includes('旅行')
      )!
      fireEvent.click(hobby.querySelector('.mem-tag-x')!)
      await waitFor(() =>
        expect(document.body.textContent).toContain('没划掉：用户不在允许列表中')
      )
      expect(document.body.textContent).not.toContain('后端没应答')
    } finally {
      spy.mockRestore()
      globalThis.fetch = base
    }
  })

  it('模型把偏好回成嵌套对象时不许把整块档案卡崩掉', async () => {
    // 第十四轮沙箱实测存出来的真实形状：
    // preferences = {"other": {"pet": {"name":"团子","age":5,"type":"猫","breed":"橘猫"}}}
    const { default: MemoryViewerModal } = await import('../components/MemoryViewerModal')
    const base = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/profile/'))
        return mockResponse({
          user_id: 'web_user_001',
          profile: {
            identity: { name: '小明' },
            preferences: { other: { pet: { name: '团子', age: 5, type: '猫', breed: '橘猫' } } },
            relationships: {},
            emotional_profile: {},
          },
          prompt_context: '',
          version: 1,
        })
      if (url.includes('/detail')) return mockResponse({ layers: { core: [], important: [], regular: [] } })
      if (url.includes('/summaries')) return mockResponse({ summaries: [] })
      return base(input as RequestInfo | URL, init)
    }) as typeof fetch
    try {
      render(<MemoryViewerModal onClose={() => {}} />)
      await waitFor(() => expect(document.body.textContent).toContain('喜好偏好'))
      const body = document.body.textContent || ''
      expect(body).not.toContain('[object Object]')
      expect(body).toContain('团子')          // 得让人认得出这是哪条，然后能划掉
      expect(document.querySelectorAll('.mem-tag-x').length).toBeGreaterThan(0)
    } finally {
      globalThis.fetch = base
    }
  })

  it('后端早就有 PUT /api/profile，本轮第一次把它接上', async () => {
    const { puts, cleanupFetch } = await openProfile()
    const hobbies = [...document.querySelectorAll('.mem-pref-tag')].filter((e) =>
      e.textContent?.includes('旅行')
    )
    expect(hobbies.length).toBe(1)
    fireEvent.click(hobbies[0].querySelector('.mem-tag-x')!)
    await waitFor(() => expect(puts.length).toBe(1))
    expect(puts[0].url).toContain('/profile/web_user_001')
    // 后端是整段替换，所以回写的必须是"去掉旅行之后的整个 preferences"
    expect(puts[0].body).toEqual({
      preferences: { hobbies: ['编程'], food: ['美式'] },
    })
    await waitFor(() =>
      expect(document.body.textContent).not.toContain('旅行')
    )
    expect(document.body.textContent).toContain('编程')
    cleanupFetch()
  })

  it('基本信息的姓名也能划掉，并提示可以开口纠正', async () => {
    const { puts, cleanupFetch } = await openProfile()
    expect(document.body.textContent).toContain('记错了点 × 划掉')
    const row = [...document.querySelectorAll('.mem-kv-item')].find((e) =>
      e.textContent?.includes('小明')
    )
    expect(row).toBeTruthy()
    fireEvent.click(row!.querySelector('.mem-tag-x')!)
    await waitFor(() => expect(puts.length).toBe(1))
    expect(puts[0].body).toEqual({ identity: { occupation: '程序员' } })
    await waitFor(() => expect(document.body.textContent).not.toContain('小明'))
    cleanupFetch()
  })
})

describe('关心面板读不到设置时说的话', () => {
  it('后端回了 403 就念 403 的原因，别一口咬定"后端没在跑"', async () => {
    const { default: CareSection } = await import('../components/CareSection')
    const base = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/care/settings'))
        return mockResponse({ detail: '用户不在允许列表中' }, 403)
      return mockResponse({}, 200)
    }) as typeof fetch
    try {
      render(<CareSection />)
      await waitFor(() =>
        expect(document.body.textContent).toContain('读不到主动关心的设置')
      )
      const body = document.body.textContent || ''
      expect(body).toContain('用户不在允许列表中')
      expect(body).not.toContain('后端没在跑')
    } finally {
      globalThis.fetch = base
      cleanup()
    }
  })
})
