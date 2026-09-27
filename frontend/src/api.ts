import type {
  Conversation,
  UserInfo,
  MemoryStats,
  ModelConfig,
  SavedModel,
  Message,
  LogEntry,
  PromptConfig,
  MemoryDetailResponse,
  UserProfileResponse,
  CareItem,
  CareRelatedNode,
  CareEdge,
  CareSettings,
  ProactiveItem,
} from './types'

const BASE = '/api'

const ACCESS_KEY_STORAGE = 'moz_access_key'
const ADMIN_KEY_STORAGE = 'moz_admin_key'

export function getStoredAccessKey(): string {
  return localStorage.getItem(ACCESS_KEY_STORAGE) || ''
}

export function setStoredAccessKey(key: string) {
  if (key) {
    localStorage.setItem(ACCESS_KEY_STORAGE, key)
  } else {
    localStorage.removeItem(ACCESS_KEY_STORAGE)
  }
}

export function getStoredAdminKey(): string {
  return localStorage.getItem(ADMIN_KEY_STORAGE) || ''
}

export function setStoredAdminKey(key: string) {
  if (key) {
    localStorage.setItem(ADMIN_KEY_STORAGE, key)
  } else {
    localStorage.removeItem(ADMIN_KEY_STORAGE)
  }
}

function buildHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const accessKey = getStoredAccessKey()
  if (accessKey) headers['X-Access-Key'] = accessKey
  const adminKey = getStoredAdminKey()
  if (adminKey) headers['X-Admin-Key'] = adminKey
  return { ...headers, ...extra }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    ...options,
    headers: buildHeaders(options?.headers as Record<string, string> | undefined),
  })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(readableError(err, res.status) || `HTTP ${res.status}`)
  }
  return res.json()
}

/**
 * 后端 detail 有时是中文短句，有时是网关的 HTML 或 JSON 残渣。
 * 这些会直接出现在聊天气泡里，所以先剥成一句话，别把 {"detail":"..."} 甩给用户。
 */
export function readableError(raw: string, status = 0): string {
  const text = (raw || '').trim()
  if (!text) return status ? `服务返回了空响应（${status}）` : ''
  if (text.startsWith('<')) return '服务没正常响应（返回的是网关页面），稍后再试一次'
  let detail = text
  try {
    const parsed = JSON.parse(text)
    const d = parsed?.detail ?? parsed?.error?.message ?? parsed?.error
    if (typeof d === 'string' && d.trim()) detail = d.trim()
  } catch {
    /* 不是 JSON，原文就是提示语 */
  }
  return detail.length > 160 ? detail.slice(0, 160) + '…' : detail
}

/**
 * 后端答了话就别赖它"没应答"——把能读的原因念出来。
 * 第十四轮在同类接口上实测到 403「用户不在允许列表中」，
 * 而界面上原来一律显示"后端没应答"，照着这句话去重启后端就是白工。
 */
export function whyFailed(e: unknown): string {
  const msg = e instanceof Error ? e.message.trim() : ''
  return msg && msg !== 'Failed to fetch' ? msg : '后端没应答，稍后再试一次'
}

// Module-level abort controller for stopping generation
let _activeController: AbortController | null = null

export function stopGeneration() {
  if (_activeController) {
    _activeController.abort()
    _activeController = null
  }
}

export const api = {
  getHealth(): Promise<{ status: string; auth_required: boolean; admin_key_configured: boolean }> {
    return request('/health')
  },

  getConversations(userId: string): Promise<{ conversations: Conversation[]; current_id: string }> {
    return request(`/conversations/${userId}`)
  },

  getConversation(
    userId: string,
    convId: string
  ): Promise<{ title: string; messages: Message[]; created: string }> {
    return request(`/conversations/${userId}/${convId}`)
  },

  createConversation(userId: string): Promise<{ id: string; title: string; created: string }> {
    return request(`/conversations/${userId}`, { method: 'POST' })
  },

  renameConversation(userId: string, convId: string, title: string) {
    return request(`/conversations/${userId}/${convId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    })
  },

  deleteConversation(userId: string, convId: string): Promise<{ ok: boolean; current_id: string }> {
    return request(`/conversations/${userId}/${convId}`, { method: 'DELETE' })
  },

  getUsers(): Promise<UserInfo[]> {
    return request('/users')
  },

  createUser(userId: string) {
    return request(`/users?user_id=${encodeURIComponent(userId)}`, { method: 'POST' })
  },

  getMemoryStats(userId: string): Promise<MemoryStats> {
    return request(`/memory/${userId}/stats`)
  },

  clearMemories(userId: string) {
    return request(`/memory/${userId}`, { method: 'DELETE' })
  },

  deleteMemory(userId: string, memoryId: string) {
    return request(`/memory/${userId}/${memoryId}`, { method: 'DELETE' })
  },

  feedbackMemory(
    userId: string,
    memoryId: string,
    feedback: 'helpful' | 'irrelevant' | 'wrong' | 'outdated'
  ) {
    return request(`/memory/${userId}/${memoryId}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ feedback }),
    })
  },

  getModelConfig(): Promise<ModelConfig> {
    return request('/config/model')
  },

  updateModelConfig(data: {
    model: string
    base_url: string
    api_key?: string
    use_thinking?: boolean
    multimodal?: boolean | null
  }): Promise<{ ok: boolean; message: string }> {
    return request('/config/model', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  },

  getModelPresets(): Promise<Record<string, any>> {
    return request('/config/model-presets')
  },

  listProviderModels(
    baseUrl: string,
    apiKey: string
  ): Promise<{ ok: boolean; models: string[]; error: string | null }> {
    return request('/config/list-models', {
      method: 'POST',
      body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
    })
  },

  // ── 我存的模型（密钥留在后端，前端只拿到 has_key）──
  getSavedModels(): Promise<{ items: SavedModel[]; max: number }> {
    return request('/config/saved-models')
  },

  saveModelAs(name: string): Promise<{ ok: boolean; saved: string; items: SavedModel[]; max: number }> {
    return request('/config/saved-models', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
  },

  useSavedModel(name: string): Promise<{ ok: boolean; model: string; items: SavedModel[]; max: number }> {
    return request('/config/saved-models/use', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
  },

  deleteSavedModel(name: string): Promise<{ ok: boolean; items: SavedModel[]; max: number }> {
    return request(`/config/saved-models/${encodeURIComponent(name)}`, { method: 'DELETE' })
  },

  getPromptConfig(): Promise<PromptConfig> {
    return request('/config/prompt')
  },

  updatePromptConfig(prompt: string): Promise<{ ok: boolean; prompt: string; is_custom: boolean }> {
    return request('/config/prompt', {
      method: 'PUT',
      body: JSON.stringify({ prompt }),
    })
  },

  getLogs(limit = 200): Promise<{ logs: LogEntry[] }> {
    return request(`/logs?limit=${limit}`)
  },

  getMemoryDetail(userId: string): Promise<MemoryDetailResponse> {
    return request(`/memory/${userId}/detail`)
  },

  getSummaries(userId: string): Promise<{
    summaries: Array<{
      id: number
      period_type: string
      period_key: string
      content: string
      created_at: number
    }>
  }> {
    return request(`/summaries/${userId}`)
  },

  exportUserData(userId: string): Promise<Record<string, unknown>> {
    return request(`/export/${userId}`)
  },

  // 头像用 fetch 取成 blob 再转 object URL：<img src> 直连不会带上访问密钥头
  async fetchAvatar(userId: string): Promise<Blob | null> {
    const res = await fetch(`${BASE}/avatar/${encodeURIComponent(userId)}`, {
      headers: buildHeaders({ Accept: 'image/*' }),
    })
    if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`)
    // 没设头像时后端回的是 {"avatar": null}，不是图片字节
    if ((res.headers.get('content-type') || '').includes('application/json')) return null
    return res.blob()
  },

  setAvatar(userId: string, dataUrl: string | null): Promise<{ ok: boolean; bytes: number }> {
    return request(`/avatar/${userId}`, {
      method: 'PUT',
      body: JSON.stringify({ data_url: dataUrl }),
    })
  },

  importUserData(
    userId: string,
    data: Record<string, unknown>
  ): Promise<{ status: string; memories_imported: number; care_items_imported?: number }> {
    return request(`/import/${userId}`, { method: 'POST', body: JSON.stringify(data) })
  },

  getUserProfile(userId: string): Promise<UserProfileResponse> {
    return request(`/profile/${userId}`)
  },

  /** 档案卡是整段替换的：改一条也要把那一整段发回去（后端 PUT /api/profile）。 */
  updateProfile(
    userId: string,
    section: string,
    value: Record<string, unknown>
  ): Promise<{ ok: boolean; updated_fields: string[]; version: number }> {
    return request(`/profile/${encodeURIComponent(userId)}`, {
      method: 'PUT',
      body: JSON.stringify({ [section]: value }),
    })
  },

  // ── 主动关心 / 个人关心数据库 ─────────────────────────
  getCareItems(userId: string, status = 'active'): Promise<{ items: CareItem[] }> {
    return request(`/care/items?user_id=${encodeURIComponent(userId)}&status=${status}`)
  },

  getCareRelated(userId: string, itemId: string, limit = 3): Promise<{ related: CareRelatedNode[] }> {
    const q = `user_id=${encodeURIComponent(userId)}&type=item&id=${encodeURIComponent(itemId)}&limit=${limit}`
    return request(`/care/related?${q}`)
  },

  getCareGraph(userId: string): Promise<{ edges: CareEdge[]; hubs: Record<string, number> }> {
    return request(`/care/graph?user_id=${encodeURIComponent(userId)}`)
  },

  getSaveQueue(userId: string): Promise<{ pending: number; running: number; pending_for_user: number }> {
    return request(`/care/save-queue?user_id=${encodeURIComponent(userId)}`)
  },

  deleteCareItem(userId: string, itemId: string): Promise<{ ok: boolean }> {
    return request(`/care/items/${itemId}?user_id=${encodeURIComponent(userId)}`, {
      method: 'DELETE',
    })
  },

  getCareSettings(userId: string): Promise<CareSettings> {
    return request(`/care/settings?user_id=${encodeURIComponent(userId)}`)
  },

  saveCareSettings(userId: string, patch: Partial<CareSettings>): Promise<CareSettings> {
    return request(`/care/settings?user_id=${encodeURIComponent(userId)}`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    })
  },

  getPendingProactive(userId: string): Promise<{ items: ProactiveItem[] }> {
    return request(`/care/pending?user_id=${encodeURIComponent(userId)}`)
  },

  ackProactive(userId: string, ids: string[]): Promise<{ ok: boolean; acked: number }> {
    return request(`/care/ack?user_id=${encodeURIComponent(userId)}`, {
      method: 'POST',
      body: JSON.stringify({ ids }),
    })
  },

  dryRunCare(
    userId: string
  ): Promise<{ would_say: Array<Record<string, string>>; budget_today: number }> {
    return request(`/care/dry-run?user_id=${encodeURIComponent(userId)}`, { method: 'POST' })
  },

  async *sendMessage(
    userId: string,
    message: string,
    conversationId: string | null,
    history: Message[],
    imageData?: string
  ): AsyncGenerator<{ type: string; text?: string; conversation_id?: string }> {
    let lastError: Error | undefined
    // 不重试：/api/chat 会写库，失败很可能是"回复已生成但落库断了"，重发就等于说两遍
    for (let attempt = 0; attempt < 1; attempt++) {
      // catch 取不到 try 里声明的变量，所以超时标记放外面
      let timedOut = false
      try {
        const controller = new AbortController()
        _activeController = controller
        // 中转一次正常回答就要 25~35s，30s 会把成功误判成超时
        const timeoutId = setTimeout(() => {
          timedOut = true
          controller.abort()
        }, 90000)

        let res: Response
        try {
          res = await fetch(`${BASE}/chat/${userId}`, {
            method: 'POST',
            headers: buildHeaders(),
            body: JSON.stringify({
              message,
              conversation_id: conversationId,
              // 只回传最近的文字：历史里的图片 data URL 会让请求体涨到中转直接拒收
              conversation_history: history
                .filter((m) => m.content)
                .slice(-20)
                .map((m) => ({ role: m.role, content: m.content })),
              image_data: imageData || null,
            }),
            signal: controller.signal,
          })
        } finally {
          clearTimeout(timeoutId)
        }

        if (!res.ok) {
          throw new Error(await readableError(await res.text()))
        }

        const reader = res.body?.getReader()
        if (!reader) throw new Error('No response body')

        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const data = line.slice(6)
              if (data === '[DONE]') return
              try {
                yield JSON.parse(data)
              } catch {
                /* 跳过格式错误的数据 */
              }
            }
          }
        }

        if (buffer.trim()) {
          for (const line of buffer.split('\n')) {
            if (line.startsWith('data: ')) {
              const data = line.slice(6)
              if (data === '[DONE]') return
              try {
                yield JSON.parse(data)
              } catch {
                /* 跳过格式错误的数据 */
              }
            }
          }
        }
        return
      } catch (e) {
        const err = e as Error
        if (err?.name === 'AbortError') {
          if (!timedOut) return // 用户自己点了停止：已经流出来的字留着，别谎报网络错误
          throw new Error('这次等得太久了（超过一分半），先没答上来。可以再发一次。')
        }
        lastError = err
        if (attempt === 0) {
          continue
        }
        throw lastError
      }
    }
    throw lastError || new Error('连接失败')
  },
}
