import { create } from 'zustand'
import type {
  Conversation,
  UserInfo,
  Message,
  MemoryStats,
  ModelConfig,
  PromptConfig,
} from './types'
import { api, setStoredAccessKey, setStoredAdminKey } from './api'
import { takeLegacyLocalAvatar, clearLegacyLocalAvatar } from './avatar'

let _msgIdCounter = 0
function nextMsgId(): string {
  return `msg_${Date.now()}_${++_msgIdCounter}`
}

/**
 * 错误直接进聊天气泡，所以不能是 `{"detail":...}` 也不能是英文。
 * 后端的 llm_errors 已经给过完整句子，这里只兜住前端自己的失败。
 */
function chatErrorText(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e ?? '')
  if (/failed to fetch|networkerror|err_connection|load failed/i.test(raw)) {
    return '没连上后端。确认后端还在跑（8000 端口），然后把刚才那句再发一次。'
  }
  if (/JSON|parse/i.test(raw) && !/[\u4e00-\u9fa5]/.test(raw)) {
    return '后端返回了看不懂的内容，可能没启动完成。稍等几秒再试。'
  }
  return raw || '这条没发出去，再试一次。'
}

/** 本地应用最常见的故障就是后端没开：报错要说"怎么办"，不是"某某加载失败"。 */
function backendDownText(what: string): string {
  return `${what}读不到：后端像是没在跑。双击项目里的 moz-app.bat 重新打开就好。`
}

interface AppState {
  userId: string
  avatar: string | null
  conversations: Conversation[]
  currentConvId: string | null
  messages: Message[]
  users: UserInfo[]
  memoryStats: MemoryStats | null
  modelConfig: ModelConfig | null
  promptConfig: PromptConfig | null
  isLoading: boolean
  /** 从发出到流结束：比 isLoading 长，isLoading 收到第一个字就false（打字指示条要让位给正文） */
  streaming: boolean
  searchQuery: string
  statusText: string
  authRequired: boolean
  isAuthenticated: boolean
  error: string | null
  lastFailedMessage: { content: string; imageData?: string } | null

  setUserId: (id: string) => void
  loadAvatar: () => Promise<void>
  uploadAvatar: (dataUrl: string) => Promise<void>
  deleteAvatar: () => Promise<void>
  setSearchQuery: (q: string) => void
  setStatusText: (t: string) => void

  loadConversations: () => Promise<void>
  loadConversation: (convId: string) => Promise<void>
  createConversation: () => Promise<string>
  renameConversation: (convId: string, title: string) => Promise<void>
  deleteConversation: (convId: string) => Promise<void>
  switchConversation: (convId: string) => Promise<void>

  addMessage: (msg: Message) => void
  setMessages: (msgs: Message[]) => void

  loadUsers: () => Promise<void>
  createUser: (userId: string) => Promise<void>
  switchUser: (userId: string) => Promise<void>

  loadMemoryStats: () => Promise<void>
  clearMemories: () => Promise<void>
  pullProactive: () => Promise<void>

  loadModelConfig: () => Promise<void>

  loadPromptConfig: () => Promise<void>
  updatePromptConfig: (prompt: string) => Promise<void>
  checkAuth: () => Promise<void>
  setAuthKeys: (accessKey: string, adminKey: string) => void

  sendMessage: (content: string, imageData?: string) => AsyncGenerator<void>
  retryLastMessage: () => AsyncGenerator<void>
}

export const useStore = create<AppState>((set, get) => ({
  userId: 'web_user_001',
  avatar: null,
  conversations: [],
  currentConvId: null,
  messages: [],
  users: [],
  memoryStats: null,
  modelConfig: null,
  promptConfig: null,
  isLoading: false,
  streaming: false,
  searchQuery: '',
  statusText: '',
  authRequired: false,
  isAuthenticated: true,
  error: null,
  lastFailedMessage: null,

  setUserId: (id) => set({ userId: id }),
  loadAvatar: async () => {
    const { userId } = get()
    try {
      const blob = await api.fetchAvatar(userId)
      if (!blob && takeLegacyLocalAvatar()) {
        // 老版本把头像存在 localStorage，这里一次性搬到后端，之后不再依赖本地存储
        await api.setAvatar(userId, takeLegacyLocalAvatar())
        clearLegacyLocalAvatar()
        await get().loadAvatar()
        return
      }
      clearLegacyLocalAvatar()
      set({ avatar: blob ? URL.createObjectURL(blob) : null })
    } catch (e) {
      console.error('avatar load failed:', e)
      set({ avatar: null })
    }
  },
  uploadAvatar: async (dataUrl) => {
    await api.setAvatar(get().userId, dataUrl)
    await get().loadAvatar()
  },
  deleteAvatar: async () => {
    await api.setAvatar(get().userId, null)
    set({ avatar: null })
  },
  setSearchQuery: (q) => set({ searchQuery: q }),
  setStatusText: (t) => set({ statusText: t }),

  loadConversations: async () => {
    const { userId } = get()
    try {
      const data = await api.getConversations(userId)
      set({
        // 后端半启动时这里可能拿到空对象，不兜住会让历史列表整块崩掉
        conversations: Array.isArray(data.conversations) ? data.conversations : [],
        currentConvId: data.current_id || null,
      })
    } catch {
      // 历史对话是打开应用第一眼看到的东西：说清"后端没跑"，别甩个未捕获异常
      set({ conversations: [], currentConvId: null, error: backendDownText('历史对话') })
    }
  },

  loadConversation: async (convId) => {
    const { userId } = get()
    const data = await api.getConversation(userId, convId)
    set({
      currentConvId: convId,
      messages: data.messages || [],
    })
  },

  createConversation: async () => {
    const { userId } = get()
    const data = await api.createConversation(userId)
    await get().loadConversations()
    await get().loadConversation(data.id)
    return data.id
  },

  renameConversation: async (convId, title) => {
    const { userId } = get()
    await api.renameConversation(userId, convId, title)
    await get().loadConversations()
  },

  deleteConversation: async (convId) => {
    const { userId, currentConvId } = get()
    const result = await api.deleteConversation(userId, convId)
    if (currentConvId === convId) {
      await get().loadConversation(result.current_id)
    }
    await get().loadConversations()
  },

  switchConversation: async (convId) => {
    await get().loadConversation(convId)
    set({ messages: get().messages })
  },

  addMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),
  setMessages: (msgs) => set({ messages: msgs }),

  loadUsers: async () => {
    const users = await api.getUsers()
    const { userId } = get()
    const exists = users.some((u) => u.id === userId)
    if (!exists && users.length > 0) {
      set({ userId: users[0].id })
    }
    set({ users })
  },

  createUser: async (newUserId) => {
    await api.createUser(newUserId)
    await get().loadUsers()
  },

  switchUser: async (newUserId) => {
    set({ userId: newUserId, messages: [], conversations: [], currentConvId: null })
    await get().loadConversations()
    const { currentConvId } = get()
    if (currentConvId) {
      await get().loadConversation(currentConvId)
    }
    await get().loadMemoryStats()
  },

  loadMemoryStats: async () => {
    const { userId } = get()
    try {
      const stats = await api.getMemoryStats(userId)
      set({ memoryStats: stats, error: null })
    } catch (e) {
      set({ error: backendDownText('记忆数量') })
    }
  },

  clearMemories: async () => {
    const { userId } = get()
    await api.clearMemories(userId)
    await Promise.all([get().loadMemoryStats(), get().loadUsers()])
  },

  pullProactive: async () => {
    const { userId, isLoading } = get()
    if (isLoading) return // 正在流式回复时不插话，下一轮再取
    try {
      const { items } = await api.getPendingProactive(userId)
      if (!items.length) return
      const msgs: Message[] = items.map((it) => ({
        id: `proactive_${it.id}`,
        role: 'assistant',
        content: it.text,
      }))
      set((s) => ({ messages: [...s.messages, ...msgs] }))
      await api.ackProactive(
        userId,
        items.map((it) => it.id)
      )
      await get().loadConversations()
    } catch {
      // 后端未就绪或此刻不该说话：静默等下一轮轮询，不打扰
    }
  },

  loadModelConfig: async () => {
    try {
      const config = await api.getModelConfig()
      set({ modelConfig: config, error: null })
    } catch (e) {
      set({ error: backendDownText('模型配置') })
    }
  },

  loadPromptConfig: async () => {
    try {
      const config = await api.getPromptConfig()
      set({ promptConfig: config, error: null })
    } catch (e) {
      set({ error: backendDownText('人设') })
    }
  },

  updatePromptConfig: async (prompt) => {
    const result = await api.updatePromptConfig(prompt)
    set((s) => ({
      promptConfig: s.promptConfig
        ? { ...s.promptConfig, prompt: result.prompt, is_custom: result.is_custom }
        : { prompt: result.prompt, default_prompt: '', is_custom: result.is_custom },
    }))
  },

  checkAuth: async () => {
    try {
      const health = await api.getHealth()
      const required = health.auth_required
      if (!required) {
        set({ authRequired: false, isAuthenticated: true })
        return
      }
      const storedKey = localStorage.getItem('moz_access_key')
      if (!storedKey) {
        set({ authRequired: true, isAuthenticated: false })
        return
      }
      try {
        await api.getConversations(get().userId)
        set({ authRequired: true, isAuthenticated: true })
      } catch {
        set({ authRequired: true, isAuthenticated: false })
      }
    } catch {
      set({ authRequired: false, isAuthenticated: true })
    }
  },

  setAuthKeys: (accessKey, adminKey) => {
    setStoredAccessKey(accessKey)
    setStoredAdminKey(adminKey)
    set({ isAuthenticated: true })
  },

  sendMessage: async function* (content, imageData) {
    const { userId, currentConvId, messages } = get()
    set({ isLoading: true, streaming: true, statusText: '', error: null, lastFailedMessage: { content, imageData } })

    const userMsgId = nextMsgId()
    const userMsg: Message = { id: userMsgId, role: 'user', content, image: imageData || undefined }
    set((s) => ({ messages: [...s.messages, userMsg] }))

    const assistantMsgId = nextMsgId()
    let assistantMsg: Message = { id: assistantMsgId, role: 'assistant', content: '' }
    let assistantAdded = false

    try {
      const stream = api.sendMessage(userId, content, currentConvId, messages, imageData)
      for await (const event of stream) {
        if (event.type === 'status') {
          set({ statusText: event.text || '' })
        } else if (event.type === 'token') {
          // 累积 token 内容
          assistantMsg = { ...assistantMsg, content: assistantMsg.content + (event.text || '') }

          if (!assistantAdded) {
            // 第一次收到 token，添加 assistant 消息
            assistantAdded = true
            set((s) => ({
              messages: [...s.messages, assistantMsg],
              isLoading: false,
              statusText: '',
            }))
          } else {
            // 后续 token，更新已存在的 assistant 消息
            set((s) => {
              const msgIndex = s.messages.findIndex((m) => m.id === assistantMsgId)
              if (msgIndex === -1) {
                // 如果找不到，追加到末尾（异常情况）
                return { messages: [...s.messages, assistantMsg] }
              }
              const newMessages = [...s.messages]
              newMessages[msgIndex] = assistantMsg
              return { messages: newMessages }
            })
          }
        } else if (event.type === 'reply') {
          // 收到完整回复，更新消息内容（只在 assistantAdded=true 时执行）
          if (assistantAdded) {
            const finalContent = event.text || assistantMsg.content
            assistantMsg = { ...assistantMsg, content: finalContent }
            set((s) => {
              const msgIndex = s.messages.findIndex((m) => m.id === assistantMsgId)
              if (msgIndex === -1) {
                return { messages: [...s.messages, assistantMsg] }
              }
              const newMessages = [...s.messages]
              newMessages[msgIndex] = assistantMsg
              return { messages: newMessages }
            })
          }
        } else if (event.type === 'done') {
          if (event.conversation_id && !currentConvId) {
            set({ currentConvId: event.conversation_id })
          }
          // 发出去了就不留"重发上一条"，否则几小时前的旧话会被这个按钮再发一遍
          set({ lastFailedMessage: null })
          get().loadConversations()
          get().loadMemoryStats()
          yield
        } else if (event.type === 'error') {
          set({ isLoading: false, streaming: false, statusText: '' })
          throw new Error(event.text || 'Unknown error')
        }
      }
      // 中途点"停止"时流是正常结束的（没有抛错），这里必须收掉"正在回复"，否则输入框一直锁着
      set({ isLoading: false, streaming: false, statusText: '' })
    } catch (e) {
      const errMsg: Message = {
        id: nextMsgId(),
        role: 'assistant',
        content: chatErrorText(e),
      }
      if (!assistantAdded) {
        set((s) => ({ messages: [...s.messages, errMsg], isLoading: false, streaming: false, statusText: '' }))
      } else {
        set((s) => {
          const msgIndex = s.messages.findIndex((m) => m.id === assistantMsgId)
          if (msgIndex === -1) {
            return { messages: [...s.messages, errMsg], isLoading: false, statusText: '' }
          }
          const newMessages = [...s.messages]
          newMessages[msgIndex] = errMsg
          return { messages: newMessages, isLoading: false, streaming: false, statusText: '' }
        })
      }
    }
  },

  retryLastMessage: async function* () {
    const { lastFailedMessage } = get()
    if (!lastFailedMessage) return
    yield* get().sendMessage(lastFailedMessage.content, lastFailedMessage.imageData)
  },
}))
