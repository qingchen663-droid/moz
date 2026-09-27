import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import MessageBubble from './MessageBubble'
import { stopGeneration } from '../api'
import ChatInput from './ChatInput'
import './ChatArea.css'

/** 等了多久才开始解释——中转发度时一句 25~35 秒是常态，太早开口反而像出错了。 */
export const WAIT_NOTICE_AFTER = 40

export function waitedLabel(sec: number): string {
  if (sec < 60) return `${sec} 秒`
  return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
}

/** 这句话她想了多久还没吐第一个字。第十五轮实测过 376 秒才回完一整句，
 *  那之前界面上只有一个转圈的点，用户不知道该等还是该走。 */
export function waitNotice(sec: number): string {
  if (sec >= 180)
    return `这句她已经想了 ${waitedLabel(sec)}，多半是中转卡住了。`
      + '再等下去不如点下面的「停止生成」，停下来重发一句通常就通。'
  return `这句她还在想（已经 ${waitedLabel(sec)}）。今天中转特别慢，`
    + '不是她没听懂；不想等可以点下面的「停止生成」。'
}

export default function ChatArea() {
  const messages = useStore((s) => s.messages)
  const isLoading = useStore((s) => s.isLoading)
  const statusText = useStore((s) => s.statusText)
  const error = useStore((s) => s.error)
  const lastFailedMessage = useStore((s) => s.lastFailedMessage)
  const retryLastMessage = useStore((s) => s.retryLastMessage)
  const memoryStats = useStore((s) => s.memoryStats)
  const bottomRef = useRef<HTMLDivElement>(null)
  // 统计还没读回来时别当人是新用户：那时光标一抖，老用户会看到"第一次怎么上手"
  const firstTime = memoryStats !== null && memoryStats.total === 0

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, statusText])

  // 这一句等了多久： isLoading 一真就起表，第一个字到手/结束自然归零
  const [waited, setWaited] = useState(0)
  useEffect(() => {
    if (!isLoading) {
      setWaited(0)
      return
    }
    const t0 = Date.now()
    const id = setInterval(() => setWaited(Math.floor((Date.now() - t0) / 1000)), 1000)
    return () => clearInterval(id)
  }, [isLoading])

  return (
    <div className="chat-area">
      <header className="chat-header">
        <h2 className="chat-header-title">moz</h2>
        <span className="chat-header-subtitle">
          {memoryStats ? `长期记忆 · ${memoryStats.total} 条` : '长期记忆'}
        </span>
      </header>

      <div className="chat-messages">
        {messages.length === 0 && !isLoading && (
          <div className="chat-welcome">
            <div className="chat-welcome-icon">
              <svg
                width="52"
                height="52"
                viewBox="0 0 24 24"
                fill="var(--color-accent)"
                opacity="0.35"
              >
                <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z" />
              </svg>
            </div>
            <h2 className="chat-welcome-title">我在这里，听你说</h2>
            <p className="chat-welcome-text">
              说什么都行：今天做了什么、烦什么、突然想起什么。
              重要的事它自己会记下来，下次不用你重复。
            </p>
            {firstTime && (
              <p className="chat-welcome-text chat-welcome-text--hint">
                想先试一下：发一句「记住，我妈生日是 10 月 5 日」，再点左边的「记忆」，
                看它把它记成了什么样。
              </p>
            )}
          </div>
        )}

        {messages.map((msg) => (
          <MessageBubble key={msg.id || `${msg.role}_${msg.content.slice(0, 20)}`} message={msg} />
        ))}

        {isLoading && (
          <>
            <div className="chat-thinking">
              <div className="chat-thinking-avatar">忆</div>
              <div className="chat-thinking-bubble">
                <span className="chat-thinking-dots">
                  <span />
                  <span />
                  <span />
                </span>
                {statusText && <span className="chat-thinking-text">{statusText}</span>}
              </div>
            </div>
            <button className="chat-stop-btn" onClick={stopGeneration}>
              停止生成
            </button>
            {waited >= WAIT_NOTICE_AFTER && !statusText && (
              <div className="chat-waiting-note" role="status">
                {waitNotice(waited)}
              </div>
            )}
          </>
        )}

        {error && !isLoading && (
          <div className="chat-error-banner">
            <span>{error}</span>
            {lastFailedMessage && (
              <button
                className="chat-retry-btn"
                onClick={() => {
                  const gen = retryLastMessage()
                  gen.next()
                }}
              >
                重试
              </button>
            )}
            <button
              className="chat-dismiss-btn"
              title="关掉这条提示"
              onClick={() => useStore.setState({ error: null })}
            >
              ✕
            </button>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <ChatInput />
    </div>
  )
}
