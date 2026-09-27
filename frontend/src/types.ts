export interface Message {
  id?: string
  role: 'user' | 'assistant'
  content: string
  image?: string
}

export interface Conversation {
  id: string
  title: string
  created: string
  message_count: number
  is_active: boolean
  messages?: Message[]
}

export interface UserInfo {
  id: string
  memory_count: number
  consolidated_count: number
  avg_importance: number
}

export interface MemoryStats {
  total: number
  emotion_distribution?: Record<string, number>
  category_distribution?: Record<string, number>
  avg_importance: number
  consolidated_count: number
}

export interface ModelConfig {
  model: string
  base_url: string
  multimodal: boolean
  multimodal_declared?: boolean
  use_thinking?: boolean
}

/** 用户自己存的模型配置：后端不返回密钥，只返回 has_key */
export interface SavedModel {
  name: string
  model: string
  base_url: string
  use_thinking: boolean
  multimodal: boolean | null
  has_key: boolean
  saved_at: number
  is_active: boolean
}

export interface ModelPreset {
  model: string
  base_url: string
  support_url: string
  support_text: string
  use_thinking: boolean
}

export interface ModelPresets {
  [key: string]: ModelPreset
}

export type CareKind = 'birthday' | 'event' | 'promise' | 'checkin' | 'health' | 'person' | 'note'

export type CareRepeat = 'none' | 'daily' | 'weekly' | 'yearly'

export interface CareItem {
  id: string
  user_id: string
  kind: CareKind
  title: string
  detail: string
  due_at: number
  repeat: CareRepeat
  status: string
  source: string
  created_at: number
  updated_at: number
  last_fired_at: number
}

export interface CareSettings {
  enabled: boolean
  /** 到点提醒记下的事：生日、面试、复诊 */
  remind_events: boolean
  /** 平时没来由地主动搭话：问候、追问上次没说完的话头 */
  initiate_chat: boolean
  province: string
  city: string
  quiet_start: string
  quiet_end: string
  talk_mode: 'auto' | 'quiet' | 'normal' | 'chatty'
  talk_score: number
  /** 后端换算结果：没来由的搭话一天最多几条 */
  budget_today?: number
  rain_reminder: boolean
  user_id?: string
}

export interface ProactiveItem {
  id: string
  user_id: string
  kind: string
  text: string
  created_at: number
  acked_at: number | null
}

export interface LogEntry {
  timestamp: number
  level: string
  message: string
}

export interface SSEEvent {
  type: 'status' | 'token' | 'reply' | 'done' | 'error'
  text?: string
  conversation_id?: string
}

export interface PromptConfig {
  prompt: string
  default_prompt: string
  is_custom: boolean
}

export interface MemoryDetail {
  id: string
  content: string
  emotion: string
  emotion_emoji: string
  emotion_intensity: number
  category: string
  importance: number
  access_count: number
  created_at: number
  last_accessed: number
  is_consolidated: boolean
  tags: string[]
  temporal_data: Record<string, any>
}

export interface WorkingMemory {
  summary: string
  open_topics: Array<string | { id?: string; topic?: string; status?: string; due_at?: number; created_at?: number }>
  current_emotion: string
  updated_at: number
}

export interface MemoryDetailResponse {
  layers: {
    core: MemoryDetail[]
    important: MemoryDetail[]
    regular: MemoryDetail[]
  }
  working_memory: WorkingMemory
}

export interface UserProfileData {
  identity: Record<string, any>
  preferences: Record<string, any>
  relationships: Record<string, any>
  emotional_profile: Record<string, any>
}

export interface UserProfileResponse {
  user_id: string
  profile: UserProfileData
  prompt_context: string
  version: number
}
