/// <reference types="vitest/globals" />
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'
import CareSection, { dueNote, sortForPanel } from '../components/CareSection'
import { useStore } from '../store'
import type { CareItem } from '../types'

const originalFetch = globalThis.fetch
const DAY = 86400
const REAL_NOW = Date.now() / 1000
const stamp = (y: number, mo: number, d: number, h = 9) => new Date(y, mo, d, h).getTime() / 1000
// 固定参考日：2026 年 6 月 1 日上午十点，专门用来验日期滚动的算术
const TODAY = stamp(2026, 5, 1, 10)

function item(id: string, title: string, due_at: number, repeat = 'none', kind = 'event'): CareItem {
  return {
    id,
    user_id: 'web_user_001',
    kind,
    title,
    detail: '',
    due_at,
    repeat,
    status: 'active',
    source: 'auto',
    created_at: 0,
    updated_at: 0,
    last_fired_at: 0,
  } as CareItem
}

describe('关心事项列表里的日子', () => {
  it('一次性且日子过了就标「已过」，还没到的不标', () => {
    expect(dueNote(item('a', '项目答辩', stamp(2026, 2, 5)), TODAY)).toContain('已过')
    expect(dueNote(item('b', '体检', stamp(2026, 5, 8)), TODAY)).not.toContain('已过')
    expect(dueNote(item('n', '面试', 0), TODAY)).toBe(' · 未定时')
  })

  it('每年重复的往后滚到下一次，别把去年的日子摊给用户', () => {
    const birthday = item('c', '妈妈生日', stamp(2025, 9, 5), 'yearly', 'birthday')
    const note = dueNote(birthday, TODAY)
    expect(note).toContain('2026-10-05')
    expect(note).toContain('每年')
    expect(note).not.toContain('已过')
    // 今年那一天也过了 → 滚到明年，还是不"已过"
    expect(dueNote(birthday, stamp(2026, 11, 1))).toContain('2027-10-05')
  })

  it('每周/每天也滚，并且每周终于有标记了（以前只有每年和每天）', () => {
    expect(dueNote(item('d', '周会', stamp(2026, 4, 20), 'weekly'), TODAY)).toMatch(/2026-06-03 · 每周/)
    expect(dueNote(item('e', '吃药', stamp(2026, 4, 30), 'daily'), TODAY)).toMatch(/2026-06-01 · 每天/)
  })

  it('还没到的排前面，过去了的沉到底（最近的旧事在前）', () => {
    const rows = [
      item('p1', '三个月前的答辩', stamp(2026, 2, 5)),
      item('p2', '上个月的婚礼', stamp(2026, 4, 9)),
      item('live', '下周体检', stamp(2026, 5, 8)),
      item('none', '面试', 0),
    ]
    expect(sortForPanel(rows, TODAY).map((i) => i.id)).toEqual(['live', 'none', 'p2', 'p1'])
  })
})

const PANEL = [
  item('g1', '下周体检', REAL_NOW + 7 * DAY),
  item('g2', '三个月前的答辩', REAL_NOW - 90 * DAY),
  item('g3', '妈妈生日', REAL_NOW - 400 * DAY, 'yearly', 'birthday'),
  item('g4', '没定日子的面试', 0),
]

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {}
  useStore.setState({ userId: 'web_user_001' })
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    if (url.includes('/care/settings'))
      return new Response(
        JSON.stringify({
          enabled: true, remind_events: true, initiate_chat: true, province: '', city: '',
          quiet_start: '23:00', quiet_end: '08:00', talk_mode: 'auto', talk_score: 0.5,
          budget_today: 2, rain_reminder: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    if (url.includes('/care/related')) return new Response(JSON.stringify({ related: [] }), { status: 200 })
    if (url.includes('/care/graph')) return new Response(JSON.stringify({ edges: [], stats: {} }), { status: 200 })
    if (url.includes('/care/items'))
      return new Response(JSON.stringify({ items: PANEL }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    return new Response(JSON.stringify({}), { status: 200 })
  }) as typeof fetch
})

afterAll(() => {
  globalThis.fetch = originalFetch
})

afterEach(() => {
  cleanup()
})

describe('面板上真正渲染出来的顺序', () => {
  it('第一行是没到的事，旧事沉到最后并带着「已过」', async () => {
    render(<CareSection />)
    await waitFor(() => expect(document.querySelectorAll('.care-item').length).toBe(4))
    const rows = Array.from(document.querySelectorAll('.care-item'))
    const title = (r: Element) => r.querySelector('.care-item-title')?.textContent || ''
    const meta = (t: string) =>
      rows.find((r) => title(r) === t)?.querySelector('.care-item-meta')?.textContent || ''
    expect(rows.map(title)).toEqual(['下周体检', '妈妈生日', '没定日子的面试', '三个月前的答辩'])
    expect(meta('三个月前的答辩')).toContain('已过')
    expect(meta('下周体检')).not.toContain('已过')
    const yearly = meta('妈妈生日')
    expect(yearly).toContain('每年')
    // 「每年」那条显示的必须是还没到的那一天，不是库里那个旧日期
    const fmt = (t: number) => {
      const d = new Date(t * 1000)
      const p = (n: number) => String(n).padStart(2, '0')
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
    }
    const shown = yearly.match(/\d{4}-\d{2}-\d{2}/)?.[0] || ''
    expect(shown >= fmt(REAL_NOW)).toBe(true)
  })
})
