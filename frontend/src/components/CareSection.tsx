import { useCallback, useEffect, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import type { CareEdge, CareItem, CareKind, CareRelatedNode, CareSettings } from '../types'
import { stripInternal } from '../memoryText'
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
  auto: '自动（按你的聊法判断）',
  quiet: '偏安静：搭话每天最多 1 次',
  normal: '适中：搭话每天最多 2 次',
  chatty: '爱聊：搭话每天最多 4 次',
}

function fromEpoch(sec: number): string {
  if (!sec) return ''
  const d = new Date(sec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(
    d.getMinutes()
  )}`
}

function viaNote(via: string): string {
  if (via === 'together') return '同一句话记着'
  const [kind, hub] = via.split(':')
  if (kind === 'person') return `关于${hub}`
  return `和「${hub}」有关`
}

/** 记忆条目的正文带着抽取器的 [关于用户] 这类前缀，是内部标记，别给用户看 */
function plainLabel(label: string): string {
  return stripInternal(label)
}

/** 先后链：src 是晚发生的那件，dst 是早的那件。挂在事项后面显示给用户的就一句话。 */
function chainNotes(items: CareItem[], edges: CareEdge[]): Record<string, string> {
  const titles = new Map(items.map((i) => [i.id, i.title]))
  const out: Record<string, string> = {}
  for (const e of edges) {
    if (e.rel !== 'after') continue
    const days = Math.max(1, Math.round(e.offset_days || 0))
    const later = titles.get(e.src_id) || plainLabel(e.label)
    const earlier = titles.get(e.dst_id)
    if (earlier && !out[e.dst_id]) out[e.dst_id] = `${days} 天后：${later}`
    if (later && !out[e.src_id]) out[e.src_id] = `晚于「${earlier || plainLabel(e.label)}」${days} 天`
  }
  return out
}

const DAY = 86400

function startOfDay(t: number): number {
  const d = new Date(t * 1000)
  d.setHours(0, 0, 0, 0)
  return d.getTime() / 1000
}

/** 还没到的那个日子。重复事项要往后滚——「妈妈生日 · 每年」标着去年那一天，读起来像坏了。 */
function nextDue(it: CareItem, today: number): number {
  if (!it.due_at) return 0
  const step = it.repeat === 'daily' ? DAY : it.repeat === 'weekly' ? 7 * DAY : 0
  if (step) {
    let t = it.due_at
    const floor = startOfDay(today)
    while (t < floor) t += step
    return t
  }
  if (it.repeat !== 'yearly') return it.due_at
  const due = new Date(it.due_at * 1000)
  const now = new Date(today * 1000)
  const cand = new Date(
    now.getFullYear(),
    due.getMonth(),
    due.getDate(),
    due.getHours(),
    due.getMinutes()
  )
  if (cand.getTime() / 1000 < startOfDay(today)) cand.setFullYear(cand.getFullYear() + 1)
  return cand.getTime() / 1000
}

/** 一次性且日子已经过了。不改成"已完成"：状态一变，关联图会把它的边剪掉，
 *  「答辩 → 出结果」这类先后链就没了——所以只在显示上沉底并标出来。 */
function isGone(it: CareItem, today: number): boolean {
  return !!it.due_at && it.repeat === 'none' && it.due_at < startOfDay(today)
}

export function dueNote(it: CareItem, today: number = Date.now() / 1000): string {
  if (!it.due_at) return ' · 未定时'
  const rep =
    it.repeat === 'yearly'
      ? ' · 每年'
      : it.repeat === 'weekly'
        ? ' · 每周'
        : it.repeat === 'daily'
          ? ' · 每天'
          : ''
  return ` · ${fromEpoch(nextDue(it, today)).slice(0, 10)}${isGone(it, today) ? ' 已过' : ''}${rep}`
}

/** 还没到的排前面，过去了的沉到底（按最近的旧事在前）。接口按日期升序返回，
 *  聊过三个月之后第一屏会全是早过去的事（实测 11 条里 6 条、54%）。 */
export function sortForPanel(items: CareItem[], today: number = Date.now() / 1000): CareItem[] {
  const live = items.filter((i) => !isGone(i, today))
  const past = items.filter((i) => isGone(i, today))
  // 按"下一次什么时候"排，不是按库里那个原始日期：每年重复的那条存的可能是去年的日子
  const next = (i: CareItem) => nextDue(i, today) || Number.POSITIVE_INFINITY
  live.sort((a, b) => next(a) - next(b))
  past.sort((a, b) => b.due_at - a.due_at)
  return [...live, ...past]
}

export default function CareSection() {
  const userId = useStore((s) => s.userId)
  const [settings, setSettings] = useState<CareSettings | null>(null)
  const [items, setItems] = useState<CareItem[]>([])
  const [links, setLinks] = useState<Record<string, CareRelatedNode[]>>({})
  const [chains, setChains] = useState<Record<string, string>>({})
  const [pendingRounds, setPendingRounds] = useState(0)
  const [notice, setNotice] = useState('')
  const [dryRun, setDryRun] = useState('')
  const [busy, setBusy] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)

  const reload = useCallback(async () => {
    const [s, i] = await Promise.all([api.getCareSettings(userId), api.getCareItems(userId)])
    const list = i?.items ?? []
    setSettings(s)
    setItems(list)
    setLoadFailed(false)

    const graph = await api.getCareGraph(userId).catch(() => null)
    setChains(chainNotes(list, graph?.edges ?? []))
    const queue = await api.getSaveQueue(userId).catch(() => null)
    setPendingRounds(queue?.pending_for_user ?? 0)
    const pairs = await Promise.all(
      list.slice(0, 24).map(async (it) => {
        const res = await api.getCareRelated(userId, it.id).catch(() => ({ related: [] }))
        return [it.id, res.related ?? []] as const
      })
    )
    setLinks(Object.fromEntries(pairs))
  }, [userId])

  useEffect(() => {
    reload().catch(() => {
      setLoadFailed(true)
      setNotice('读取关心设置失败，请确认后端在运行')
    })
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

  const handleDelete = async (id: string, title: string) => {
    if (!window.confirm(`确定不再提醒「${title}」？删掉后要重新让它记住。`)) return
    setBusy(true)
    setNotice('')
    try {
      await api.deleteCareItem(userId, id)
      await reload()
      setNotice(`已经不记着「${title}」了`)
    } catch {
      setNotice('删除失败，请确认后端在运行')
    } finally {
      setBusy(false)
    }
  }

  const handleDryRun = async () => {
    setDryRun('正在判定...')
    try {
      const res = await api.dryRunCare(userId)
      const first = res.would_say[0]
      setDryRun(
        first
          ? `「${first.text}」— 为什么是现在：${first.why}`
          : '此刻没有该说的：要么没到点的事，要么我今天的话已经说够了。'
      )
    } catch {
      setDryRun('判定失败，请确认后端在运行')
    }
  }

  if (!settings)
    return (
      <div className={loadFailed ? 'care-notice care-notice--fail' : 'care-loading'}>
        {loadFailed
          ? '读不到主动关心的设置：后端没在跑，或者这个接口挂了。下面的开关暂时点不动。'
          : '加载关心设置...'}
      </div>
    )

  return (
    <div className="care-section">
      <div className="care-block">
        <label className="care-switch">
          <input
            type="checkbox"
            checked={settings.remind_events}
            onChange={(e) => patch({ remind_events: e.target.checked })}
          />
          <span>到点提醒我：生日、面试、复诊这些我记着的事</span>
        </label>
        <label className="care-switch">
          <input
            type="checkbox"
            checked={settings.initiate_chat}
            onChange={(e) => patch({ initiate_chat: e.target.checked })}
          />
          <span>平时没来由地找我说话：问候一句、追上次没说完的</span>
        </label>
        <label className="care-switch">
          <input
            type="checkbox"
            checked={settings.rain_reminder}
            onChange={(e) => patch({ rain_reminder: e.target.checked })}
          />
          <span>要下雨时提醒带伞</span>
        </label>
        <div className="care-hint">前两个是分开的：只勾第一个，我不会没话找话。</div>
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
        <span className="care-hint">
          {settings.talk_mode === 'auto'
            ? `按你最近的聊法，没来由的搭话一天最多 ${settings.budget_today ?? 2} 条，会跟着你的习惯慢慢调。`
            : `没来由的搭话一天最多 ${settings.budget_today ?? '—'} 条。`}
          到点提醒不走这个名额。
        </span>
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
            {sortForPanel(items).map((it) => (
              <div key={it.id} className="care-item">
                <div className="care-item-main">
                  <span className="care-item-title">{it.title}</span>
                  <span className="care-item-meta">
                    {KIND_LABELS[it.kind]}
                    {dueNote(it)}
                    {it.source === 'auto' ? ' · 自动记下' : ''}
                  </span>
                  {(chains[it.id] || links[it.id]?.length) && (
                    <span className="care-item-links">
                      <span className="care-links-label">这件事还连着</span>
                      {(links[it.id] ?? []).map((r) => (
                        <em key={`${r.via}-${r.id}`} className="care-link-chip">
                          {viaNote(r.via)} · {plainLabel(r.label)}
                        </em>
                      ))}
                      {chains[it.id] && (
                        <em className="care-link-chip care-link-chip--chain">{chains[it.id]}</em>
                      )}
                    </span>
                  )}
                </div>
                <button
                  className="care-item-del"
                  onClick={() => handleDelete(it.id, it.title)}
                  title="忘掉这件事"
                >
                  ✕
                </button>
              </div>
            ))}
          </>
        )}
      </div>
      {pendingRounds > 0 && (
        <div className="care-notice">刚说完的那 {pendingRounds} 轮还在往长期记忆里存，不用重说一遍。</div>
      )}
      {notice && <div className="care-notice">{notice}</div>}
    </div>
  )
}
