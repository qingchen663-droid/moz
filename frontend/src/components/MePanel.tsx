import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import ConversationList from './ConversationList'
import CareSection from './CareSection'
import LogViewerModal from './LogViewerModal'
import ConfirmDialog from './ConfirmDialog'
import './MePanel.css'

export default function MePanel({ onClose }: { onClose: () => void }) {
  const userId = useStore((s) => s.userId)
  const memoryStats = useStore((s) => s.memoryStats)
  const modelConfig = useStore((s) => s.modelConfig)
  const promptConfig = useStore((s) => s.promptConfig)
  const clearMemories = useStore((s) => s.clearMemories)
  const loadPromptConfig = useStore((s) => s.loadPromptConfig)

  const [showLogs, setShowLogs] = useState(false)
  const [showClearConfirm, setShowClearConfirm] = useState(false)
  const [notice, setNotice] = useState('')
  const importInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    loadPromptConfig()
  }, [loadPromptConfig])

  const modalOpen = showLogs || showClearConfirm

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !modalOpen) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, modalOpen])

  const handleClearMemories = async () => {
    await clearMemories()
    setShowClearConfirm(false)
    setNotice('长期记忆已清除')
  }

  const handleExport = async () => {
    try {
      const data = await api.exportUserData(userId)
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `moz_export_${userId}_${Date.now()}.json`
      a.click()
      URL.revokeObjectURL(url)
      setNotice('快照已下载到本地')
    } catch (e) {
      console.error('export failed:', e)
      setNotice('导出失败，请确认后端在运行')
    }
  }

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    try {
      const data = JSON.parse(await file.text())
      const res = await api.importUserData(userId, data)
      setNotice(`已恢复 ${res.memories_imported} 条记忆`)
      await useStore.getState().loadConversations()
      await useStore.getState().loadMemoryStats()
      await useStore.getState().loadAvatar()
    } catch (err) {
      console.error('import failed:', err)
      setNotice('导入失败，请检查是否为有效的导出 JSON')
    } finally {
      e.target.value = ''
    }
  }

  return (
    <>
      <div className="me-overlay" onClick={onClose}>
        <section
          className="me-panel"
          role="dialog"
          aria-label="数据与设置"
          onClick={(e) => e.stopPropagation()}
        >
          <header className="me-head">
            <div className="me-head-text">
              <h2 className="me-title">数据与设置</h2>
              <p className="me-sub">同一段长期记忆会跨对话自动延续，无需新建会话</p>
            </div>
            <button className="me-close" onClick={onClose} aria-label="关闭">
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              >
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </header>

          <div className="me-body">
            {notice && <div className="me-notice">{notice}</div>}
            <div className="me-stats">
              <div className="me-stat">
                <div className="me-stat-value">{memoryStats?.total ?? 0}</div>
                <div className="me-stat-key">记忆总数</div>
              </div>
              <div className="me-stat">
                <div className="me-stat-value">{memoryStats?.consolidated_count ?? 0}</div>
                <div className="me-stat-key">已巩固</div>
              </div>
              <div className="me-stat">
                <div className="me-stat-value">{(memoryStats?.avg_importance ?? 0).toFixed(1)}</div>
                <div className="me-stat-key">平均重要性</div>
              </div>
            </div>

            <div className="me-group">
              <div className="me-group-label">状态</div>
              <div className="me-row me-row--static">
                <svg
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                  <circle cx="12" cy="7" r="4" />
                </svg>
                <span className="me-row-name">人设</span>
                <span className="me-row-hint">
                  {promptConfig?.is_custom ? '已自定义' : '默认人设'}
                </span>
              </div>
              <div className="me-row me-row--static">
                <svg
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <rect x="3" y="4" width="18" height="14" rx="2" />
                  <line x1="8" y1="21" x2="16" y2="21" />
                </svg>
                <span className="me-row-name">当前模型</span>
                <span className="me-row-hint">{modelConfig?.model ?? '未加载'}</span>
              </div>
            </div>

            <div className="me-group">
              <div className="me-group-label">主动关心</div>
              <CareSection />
            </div>

            <div className="me-group">
              <div className="me-group-label">数据与维护</div>
              <button className="me-row" onClick={handleExport}>
                <svg
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                <span className="me-row-name">导出数据</span>
                <span className="me-row-hint">JSON 快照</span>
              </button>
              <input
                ref={importInputRef}
                type="file"
                accept="application/json,.json"
                style={{ display: 'none' }}
                onChange={handleImport}
              />
              <button className="me-row" onClick={() => importInputRef.current?.click()}>
                <svg
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="17 8 12 3 7 8" />
                  <line x1="12" y1="3" x2="12" y2="15" />
                </svg>
                <span className="me-row-name">导入数据</span>
                <span className="me-row-hint">从快照恢复</span>
              </button>
              <button className="me-row" onClick={() => setShowLogs(true)}>
                <svg
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                  <line x1="16" y1="13" x2="8" y2="13" />
                  <line x1="16" y1="17" x2="8" y2="17" />
                </svg>
                <span className="me-row-name">查看日志</span>
                <span className="me-row-hint">运行记录</span>
              </button>
              <button className="me-row me-row--danger" onClick={() => setShowClearConfirm(true)}>
                <svg
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <polyline points="3 6 5 6 21 6" />
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                </svg>
                <span className="me-row-name">清空记忆与档案</span>
                <span className="me-row-hint">不可撤销</span>
              </button>
            </div>

            <div className="me-group">
              <div className="me-group-label">历史对话</div>
              <div className="me-history">
                <ConversationList />
              </div>
            </div>
          </div>
        </section>
      </div>

      {showLogs && <LogViewerModal onClose={() => setShowLogs(false)} />}
      {showClearConfirm && (
        <ConfirmDialog
          title="清空记忆与档案"
          message="将删除：记忆条目、总结、工作记忆、档案卡。对话记录不在其中，可在下方「历史对话」单独删除。此操作不可撤销。"
          confirmText="全部清空"
          onConfirm={handleClearMemories}
          onCancel={() => setShowClearConfirm(false)}
        />
      )}
    </>
  )
}
