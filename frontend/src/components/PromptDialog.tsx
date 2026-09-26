import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { fileToAvatarDataUrl } from '../avatar'
import './PromptDialog.css'

interface Props {
  onClose: () => void
}

export default function PromptDialog({ onClose }: Props) {
  const promptConfig = useStore((s) => s.promptConfig)
  const loadPromptConfig = useStore((s) => s.loadPromptConfig)
  const updatePromptConfig = useStore((s) => s.updatePromptConfig)
  const avatar = useStore((s) => s.avatar)
  const uploadAvatar = useStore((s) => s.uploadAvatar)
  const deleteAvatar = useStore((s) => s.deleteAvatar)

  const [editing, setEditing] = useState(false)
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)
  const [avatarBusy, setAvatarBusy] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'failed'>('loading')
  const avatarInputRef = useRef<HTMLInputElement>(null)

  const persona = promptConfig?.prompt?.trim() ?? ''

  useEffect(() => {
    let alive = true
    setLoadState('loading')
    // 后端没起来或半启动时只会拿到 null / 空对象：界面不能永远写"加载中..."
    Promise.resolve(loadPromptConfig()).finally(() => {
      if (alive) setLoadState(useStore.getState().promptConfig?.prompt?.trim() ? 'ready' : 'failed')
    })
    return () => {
      alive = false
    }
  }, [loadPromptConfig, attempt])

  // 编辑过又没保存时，任何退出方式都得先问一句
  const confirmDiscard = () =>
    !editing || text === persona || window.confirm('这些改动还没保存，丢掉吗？')

  const requestClose = () => {
    if (confirmDiscard()) onClose()
  }
  const closeRef = useRef(requestClose)
  closeRef.current = requestClose

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeRef.current()
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [])

  const handleStartEdit = () => {
    setText(persona)
    setEditing(true)
    setMessage(null)
  }

  const handleCancelEdit = () => {
    if (!confirmDiscard()) return
    setEditing(false)
    setMessage(null)
  }

  const handleReset = () => {
    setText(promptConfig?.default_prompt || '')
    setMessage({ type: 'success', text: '已填回默认人设，还没保存；点「保存并生效」才真的换掉' })
  }

  const handleAvatarPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    try {
      setAvatarBusy(true)
      await uploadAvatar(await fileToAvatarDataUrl(file))
      setMessage({ type: 'success', text: '头像已保存到服务端' })
    } catch (err) {
      setMessage({ type: 'error', text: err instanceof Error ? err.message : '头像设置失败' })
    } finally {
      setAvatarBusy(false)
    }
  }

  const handleAvatarReset = async () => {
    if (!window.confirm('换回默认头像？你上传的那张会被删掉（之前导出过快照的话，快照里还留着）。'))
      return
    try {
      setAvatarBusy(true)
      await deleteAvatar()
      setMessage({ type: 'success', text: '已恢复默认头像' })
    } catch (err) {
      setMessage({ type: 'error', text: err instanceof Error ? err.message : '清除头像失败' })
    } finally {
      setAvatarBusy(false)
    }
  }

  const handleSave = async () => {
    setSaving(true)
    setMessage(null)
    try {
      await updatePromptConfig(text)
      await loadPromptConfig()
      setEditing(false)
      setMessage({ type: 'success', text: '人设已保存，新对话即刻生效' })
    } catch (e) {
      setMessage({
        type: 'error',
        text: `保存失败：${e instanceof Error ? e.message : '未知错误'}`,
      })
    } finally {
      setSaving(false)
    }
  }

  const isCustom = promptConfig?.is_custom

  return (
    <div className="dialog-overlay" onClick={requestClose}>
      <div
        className="dialog-content prompt-dialog"
        role="dialog"
        aria-label="人设"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="dialog-title">
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ marginRight: 8, verticalAlign: -3 }}
          >
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
            <circle cx="12" cy="7" r="4" />
          </svg>
          人设
        </h3>

        <p className="prompt-dialog-desc">
          moz 是什么性格、怎么说话、跟你是什么关系，都写在这儿。改完立即生效，不用重启。
        </p>

        <div className="prompt-avatar">
          <div className="prompt-avatar-preview">
            {avatar ? (
              <img src={avatar} alt="当前头像" />
            ) : (
              <svg
                width="26"
                height="26"
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
          </div>
          <div className="prompt-avatar-meta">
            <div className="prompt-avatar-title">头像</div>
            <div className="prompt-avatar-desc">
              选一张本地图片，自动裁成正方形；头像存在后端，换浏览器也认得，并随数据导出一起备份
            </div>
          </div>
          <div className="prompt-avatar-btns">
            <input
              ref={avatarInputRef}
              type="file"
              accept="image/*"
              style={{ display: 'none' }}
              onChange={handleAvatarPick}
            />
            <button
              className="dialog-btn dialog-btn--secondary"
              onClick={() => avatarInputRef.current?.click()}
              disabled={avatarBusy}
            >
              {avatarBusy ? '处理中...' : avatar ? '更换头像' : '上传头像'}
            </button>
            {avatar && (
              <button
                className="dialog-btn dialog-btn--secondary"
                onClick={handleAvatarReset}
                disabled={avatarBusy}
              >
                恢复默认
              </button>
            )}
          </div>
        </div>

        {isCustom && !editing && (
          <div className="prompt-dialog-badge">
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="currentColor"
              style={{ marginRight: 4 }}
            >
              <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
            </svg>
            当前使用自定义人设
          </div>
        )}

        {!editing ? (
          <div className="prompt-dialog-preview">
            <div className="prompt-dialog-preview-label">
              当前人设
              {isCustom && <span className="prompt-dialog-custom-tag">自定义</span>}
            </div>
            {loadState === 'failed' && !persona ? (
              <div className="prompt-dialog-load">
                <div>读不到当前人设：后端可能没在跑（8000 端口），或者这一路超时了。</div>
                <button
                  className="dialog-btn dialog-btn--secondary"
                  onClick={() => setAttempt((n) => n + 1)}
                >
                  再试一次
                </button>
              </div>
            ) : (
              <pre className="prompt-dialog-preview-text">{persona || '加载中...'}</pre>
            )}
            <div className="prompt-dialog-actions">
              <button
                className="dialog-btn dialog-btn--primary"
                onClick={handleStartEdit}
                disabled={!persona}
              >
                {isCustom ? '修改人设' : '自定义人设'}
              </button>
              <button className="dialog-btn dialog-btn--secondary" onClick={requestClose}>
                关闭
              </button>
            </div>
          </div>
        ) : (
          <div className="prompt-dialog-editor">
            <div className="prompt-dialog-editor-label">编辑人设</div>
            <textarea
              className="prompt-dialog-textarea"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="想要 moz 怎样：它的名字和性格、说话语气、什么事它不该主动提..."
              rows={12}
              autoFocus
            />
            <div className="prompt-dialog-char-count">{text.length} / 5000</div>
            <div className="prompt-dialog-editor-tips">
              <p>💡 写具体比写抽象好用，比如：</p>
              <ul>
                <li>它是谁（名字、性格、跟你的关系）</li>
                <li>怎么说话（语气、长短、用不用表情）</li>
                <li>遇到什么该怎么反应（你难过时、你敷衍时）</li>
              </ul>
            </div>
            <div className="prompt-dialog-actions">
              <button
                className="dialog-btn dialog-btn--primary"
                onClick={handleSave}
                disabled={saving || text.length > 5000}
              >
                {saving ? '保存中...' : '保存并生效'}
              </button>
              <button className="dialog-btn dialog-btn--secondary" onClick={handleReset}>
                恢复默认
              </button>
              <button className="dialog-btn dialog-btn--secondary" onClick={handleCancelEdit}>
                取消
              </button>
            </div>
          </div>
        )}

        {message && (
          <div className={`prompt-dialog-message prompt-dialog-message--${message.type}`}>
            {message.text}
          </div>
        )}
      </div>
    </div>
  )
}
