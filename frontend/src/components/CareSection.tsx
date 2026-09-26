import { useCallback, useEffect, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import type { CareItem, CareKind, CareSettings } from '../types'
import './CareSection.css'

const KIND_LABELS: Record<CareKind, string> = {
  birthday: '生日/纪念日',
  event: '事件提醒',
  promise: '约定',
  checkin: '跟进进展',
  health: '健康',
  person: '关系人',
  note: '随口记住',
}

const TALK_LABELS: Record<CareSettings['talk_mode'], string> = {
  auto: '自动（按你的习惯判断）',
  quiet: '话少：每天最多 1 次',
  normal: '适中：每天最多 2 次',
  chatty: '话多：每天最多 4 次',
}

function fromEpoch(sec: number): string {
  if (!sec) return ''
  const d = new Date(sec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(
    d.getMinutes()
  )}`
}

export default function CareSection() {
  const userId = useStore((s) => s.userId)
  const [settings, setSettings] = useState<CareSettings | null>(null)
  const [items, setItems] = useState<CareItem[]>([])
  const [notice, setNotice] = useState('')
  const [dryRun, setDryRun] = useState('')
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    const [s, i] = await Promise.all([api.getCareSettings(userId), api.getCareItems(userId)])
    setSettings(s)
    setItems(i.items)
  }, [userId])

  useEffect(() => {
    reload().catch(() => setNotice('读取关心设置失败，请确认后端在运行'))
  }, [reload])

  const patch = (p: Partial<CareSettings>) => setSettings((cur) => (cur ? { ...cur, ...p } : cur))

  const handleSaveSettings = async () => {
    if (!settings) return
    setBusy(true)
    setNotice('')
    try {
      const saved = await api.saveCareSettings(userId, settings)
      setSettings(saved)
      setNotice('设置已保存')
    } catch {
      setNotice('保存失败，请确认后端在运行')
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (id: string) => {
    await api.deleteCareItem(userId, id)
    await reload()
  }

  const handleDryRun = async () => {
    setDryRun('正在判定...')
    try {
      const res = await api.dryRunCare(userId)
      const first = res.would_say[0]
      setDryRun(
        first
          ? `「${first.text}」— 理由：${first.why}`
          : '此刻没有够格开口的理由（可能正处安静时段，或今日配额已用完）'
      )
    } catch {
      setDryRun('判定失败，请确认后端在运行')
    }
  }

  if (!settings) return <div className="care-loading">加载关心设置...</div>

  return (
    <div className="care-section">
      <div className="care-block">
        <label className="care-switch">
          <input
            type="checkbox"
            checked={settings.enabled}
            onChange={(e) => patch({ enabled: e.target.checked })}
          />
          <span>允许 moz 主动找我说话</span>
        </label>
        <label className="care-switch">
          <input
            type="checkbox"
            checked={settings.rain_reminder}
            onChange={(e) => patch({ rain_reminder: e.target.checked })}
          />
          <span>要下雨时提醒带伞</span>
        </label>
      </div>

      <div className="care-grid">
        <label className="care-field">
          <span>省份</span>
          <input
            className="care-input"
            value={settings.province}
            placeholder="聊到会自动填"
            onChange={(e) => patch({ province: e.target.value })}
          />
        </label>
        <label className="care-field">
          <span>城市</span>
          <input
            className="care-input"
            value={settings.city}
            placeholder="聊到会自动填"
            onChange={(e) => patch({ city: e.target.value })}
          />
        </label>
        <label className="care-field">
          <span>安静时段起</span>
          <input
            className="care-input"
            type="time"
            value={settings.quiet_start}
            onChange={(e) => patch({ quiet_start: e.target.value })}
          />
        </label>
        <label className="care-field">
          <span>安静时段止</span>
          <input
            className="care-input"
            type="time"
            value={settings.quiet_end}
            onChange={(e) => patch({ quiet_end: e.target.value })}
          />
        </label>
      </div>

      <label className="care-field care-field--full">
        <span>话多还是话少</span>
        <select
          className="care-input"
          value={settings.talk_mode}
          onChange={(e) => patch({ talk_mode: e.target.value as CareSettings['talk_mode'] })}
        >
          {(Object.keys(TALK_LABELS) as CareSettings['talk_mode'][]).map((k) => (
            <option key={k} value={k}>
              {TALK_LABELS[k]}
            </option>
          ))}
        </select>
        {settings.talk_mode === 'auto' && (
          <span className="care-hint">
            当前自动判断值 {(settings.talk_score * 100).toFixed(0)}
            %，会随你回复的长短和主动程度慢慢调
          </span>
        )}
      </label>

      <div className="care-actions">
        <button className="care-btn care-btn--primary" onClick={handleSaveSettings} disabled={busy}>
          保存设置
        </button>
        <button className="care-btn" onClick={handleDryRun}>
          现在会说什么
        </button>
      </div>
      {dryRun && <div className="care-dryrun">{dryRun}</div>}

      <div className="care-items">
        {items.length === 0 ? (
          <div className="care-empty">
            聊到生日、考试、复诊这类事，我会自己记下来，不用你填。想说的时候直接说就行。
          </div>
        ) : (
          <>
            <div className="care-list-label">我记着的事（自动从对话里记的，说错了可以删）</div>
            {items.map((it) => (
              <div key={it.id} className="care-item">
                <div className="care-item-main">
                  <span className="care-item-title">{it.title}</span>
                  <span className="care-item-meta">
                    {KIND_LABELS[it.kind]}
                    {it.due_at ? ` · ${fromEpoch(it.due_at).slice(0, 10)}` : ' · 未定时'}
                    {it.repeat === 'yearly' ? ' · 每年' : it.repeat === 'daily' ? ' · 每天' : ''}
                    {it.source === 'auto' ? ' · 自动记下' : ''}
                  </span>
                </div>
                <button
                  className="care-item-del"
                  onClick={() => handleDelete(it.id)}
                  title="忘掉这件事"
                >
                  ✕
                </button>
              </div>
            ))}
          </>
        )}
      </div>
      {notice && <div className="care-notice">{notice}</div>}
    </div>
  )
}
