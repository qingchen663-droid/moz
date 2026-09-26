import { useState, useRef, useCallback, useEffect } from 'react'
import { useStore } from '../store'
import { stopGeneration } from '../api'
import './ChatInput.css'

interface ImageInfo {
  name: string
  type: string
  bytes: number
  width: number
  height: number
}

const MAX_IMAGE_BYTES = 8 * 1024 * 1024

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export default function ChatInput() {
  const [text, setText] = useState('')
  const [imagePreview, setImagePreview] = useState<string | null>(null)
  const [imageData, setImageData] = useState<string | null>(null)
  const [imageInfo, setImageInfo] = useState<ImageInfo | null>(null)
  const [imageNotice, setImageNotice] = useState('')
  const sendMessage = useStore((s) => s.sendMessage)
  const modelConfig = useStore((s) => s.modelConfig)
  const streaming = useStore((s) => s.streaming)
  const incoming = useStore((s) => {
    for (let i = s.messages.length - 1; i >= 0; i--) {
      if (s.messages[i].role === 'assistant') return s.messages[i].content.slice(-80)
    }
    return ''
  })
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const clearImage = useCallback(() => {
    setImagePreview(null)
    setImageData(null)
    setImageInfo(null)
    setImageNotice('')
  }, [])

  // Auto-resize textarea
  const resizeTextarea = useCallback(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 160) + 'px'
  }, [])

  useEffect(() => {
    resizeTextarea()
  }, [text, resizeTextarea])

  const handleSubmit = useCallback(async () => {
    const trimmed = text.trim()
    if (!trimmed && !imageData) return
    setText('')
    const payload = imageData || undefined
    clearImage()
    const gen = sendMessage(trimmed || '看看这张图', payload)
    for await (const _ of gen) {
      // consume
    }
  }, [text, imageData, sendMessage, clearImage])

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleSubmit()
      }
    },
    [handleSubmit]
  )

  const handleImageUpload = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0]
      if (fileRef.current) fileRef.current.value = ''
      if (!file) return
      if (file.size > MAX_IMAGE_BYTES) {
        setImageNotice(`图片 ${formatBytes(file.size)} 太大，请压到 8MB 以内`)
        return
      }
      const reader = new FileReader()
      reader.onerror = () => setImageNotice('读取图片失败，请换一张试试')
      reader.onload = () => {
        const result = String(reader.result)
        const probe = new Image()
        probe.onerror = () => {
          setImageNotice('这不是能识别的图片文件')
          setImagePreview(result)
          setImageData(result)
        }
        probe.onload = () => {
          setImageInfo({
            name: file.name,
            type: file.type || '未知类型',
            bytes: file.size,
            width: probe.naturalWidth,
            height: probe.naturalHeight,
          })
          setImageNotice(
            modelConfig?.multimodal ? '' : '当前模型未标记为能看图，发送前请到「模型」勾选多模态'
          )
          setImagePreview(result)
          setImageData(result)
        }
        probe.src = result
      }
      reader.readAsDataURL(file)
    },
    [modelConfig?.multimodal]
  )

  return (
    <div className="chat-input-wrapper">
      {imagePreview && (
        <div className="chat-input-preview">
          <img src={imagePreview} alt="preview" />
          <div className="chat-input-preview-meta">
            {imageInfo && (
              <>
                <div className="chat-input-preview-name" title={imageInfo.name}>
                  {imageInfo.name}
                </div>
                <div className="chat-input-preview-detail">
                  {imageInfo.width}×{imageInfo.height} · {formatBytes(imageInfo.bytes)} ·{' '}
                  {imageInfo.type.replace('image/', '')}
                </div>
              </>
            )}
            {imageNotice && <div className="chat-input-preview-notice">{imageNotice}</div>}
          </div>
          <button className="chat-input-preview-remove" onClick={clearImage}>
            ✕
          </button>
        </div>
      )}
      {streaming && (
        <div className="chat-input-incoming">
          <span className="chat-input-incoming-text">{incoming}</span>
          <button className="chat-input-stop" onClick={stopGeneration} title="中断这次回复">
            停止
          </button>
        </div>
      )}
      <div className="chat-input-container">
        <textarea
          ref={textareaRef}
          className="chat-input-textarea"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={streaming ? '想到什么先发，moz 会接着回' : '和我说说你的心事吧...'}
          rows={1}
        />
        <div className="chat-input-actions">
          <button
            className={`chat-input-attach ${modelConfig?.multimodal ? '' : 'chat-input-attach--muted'}`}
            onClick={() => fileRef.current?.click()}
            title={
              modelConfig?.multimodal
                ? '上传图片，我会看图内容'
                : '上传图片（当前模型未标记能看图，请到「模型」勾选多模态）'
            }
          >
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/jpeg,image/png,image/gif,image/webp"
            style={{ display: 'none' }}
            onChange={handleImageUpload}
          />
          <button
            className="chat-input-send"
            onClick={handleSubmit}
            disabled={!text.trim() && !imageData}
            title="发送"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
              <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  )
}
