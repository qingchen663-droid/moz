import { useEffect, useRef } from 'react'
import { useStore } from '../store'
import MessageBubble from './MessageBubble'
import { stopGeneration } from '../api'
import ChatInput from './ChatInput'
import './ChatArea.css'

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
