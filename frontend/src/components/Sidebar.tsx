import { useState } from 'react'
import { useStore } from '../store'
import ModelDialog from './ModelDialog'
import PromptDialog from './PromptDialog'
import MemoryViewerModal from './MemoryViewerModal'
import MePanel from './MePanel'
import './Sidebar.css'

export default function Sidebar() {
  const modelConfig = useStore((s) => s.modelConfig)
  const promptConfig = useStore((s) => s.promptConfig)
  const memoryStats = useStore((s) => s.memoryStats)
  const avatar = useStore((s) => s.avatar)
  const [showModel, setShowModel] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const [showMemory, setShowMemory] = useState(false)
  const [showMe, setShowMe] = useState(false)

  const total = memoryStats?.total ?? 0

  return (
    <aside className="rail">
      <button
        className="rail-item"
        onClick={() => setShowPrompt(true)}
        title={promptConfig?.is_custom ? '人设 · 已自定义' : '人设 · 默认'}
      >
        <span className="rail-icon">
          {avatar ? (
            <img src={avatar} alt="头像" className="rail-avatar" />
          ) : (
            <svg
              width="19"
              height="19"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
              <circle cx="12" cy="7" r="4" />
            </svg>
          )}
        </span>
        <span className="rail-label">人设</span>
      </button>

      <button
        className="rail-item"
        onClick={() => setShowMemory(true)}
        title="moz 记得什么 · 档案卡、记忆、还没聊完的话题"
      >
        <span className="rail-icon">
          <svg
            width="19"
            height="19"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polygon points="12 2 2 7 12 12 22 7 12 2" />
            <polyline points="2 17 12 22 22 17" />
            <polyline points="2 12 12 17 22 12" />
          </svg>
          {total > 0 && <span className="rail-badge">{total > 99 ? '99+' : total}</span>}
        </span>
        <span className="rail-label">记忆</span>
      </button>

      <div className="rail-spacer" />

      <button
        className="rail-item"
        onClick={() => setShowMe(true)}
        title="数据与设置 · 导出导入、日志、历史对话"
      >
        <span className="rail-icon">
          <svg
            width="19"
            height="19"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <line x1="4" y1="7" x2="20" y2="7" />
            <circle cx="9" cy="7" r="2.6" fill="#F3EDE6" />
            <line x1="4" y1="16" x2="20" y2="16" />
            <circle cx="15" cy="16" r="2.6" fill="#F3EDE6" />
          </svg>
        </span>
        <span className="rail-label">设置</span>
      </button>

      <button
        className="rail-item"
        onClick={() => setShowModel(true)}
        title={modelConfig ? `当前模型：${modelConfig.model}` : '模型设置'}
      >
        <span className="rail-icon">
          <svg
            width="19"
            height="19"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect x="3" y="4" width="18" height="14" rx="2" />
            <line x1="8" y1="21" x2="16" y2="21" />
          </svg>
        </span>
        <span className="rail-label">模型</span>
      </button>

      {showModel && <ModelDialog onClose={() => setShowModel(false)} />}
      {showPrompt && <PromptDialog onClose={() => setShowPrompt(false)} />}
      {showMemory && <MemoryViewerModal onClose={() => setShowMemory(false)} />}
      {showMe && <MePanel onClose={() => setShowMe(false)} />}
    </aside>
  )
}
