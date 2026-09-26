import { useState } from 'react'
import { useStore } from '../store'
import ConfirmDialog from './ConfirmDialog'
import './ConversationList.css'

export default function ConversationList() {
  const conversations = useStore((s) => s.conversations)
  const currentConvId = useStore((s) => s.currentConvId)
  const searchQuery = useStore((s) => s.searchQuery)
  const setSearchQuery = useStore((s) => s.setSearchQuery)
  const switchConversation = useStore((s) => s.switchConversation)
  const renameConversation = useStore((s) => s.renameConversation)
  const deleteConversation = useStore((s) => s.deleteConversation)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null)
  const [editTitle, setEditTitle] = useState('')

  const filtered = searchQuery
    ? conversations.filter((c) => c.title.toLowerCase().includes(searchQuery))
    : conversations

  const handleStartRename = (id: string, currentTitle: string) => {
    setEditingId(id)
    setEditTitle(currentTitle)
  }

  const handleSaveRename = async (id: string) => {
    if (editTitle.trim()) {
      await renameConversation(id, editTitle.trim())
    }
    setEditingId(null)
    setEditTitle('')
  }

  const handleDelete = async (id: string) => {
    await deleteConversation(id)
    setDeleteConfirmId(null)
  }

  return (
    <div className="conv-list">
      <div className="conv-list-search">
        <svg
          className="conv-list-search-icon"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        >
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="搜索对话…"
          className="conv-list-search-input"
        />
      </div>

      <div className="conv-list-items">
        {filtered.length === 0 ? (
          <div className="conv-list-empty">
            {searchQuery
              ? `没搜到「${searchQuery}」——这里只按标题搜，换个词或者清空搜索框就能看全部。`
              : '还没有第二条对话。你说第一句之后就会出现在这儿，切回来接着聊就行。'}
          </div>
        ) : (
          filtered.map((conv) => {
            const isActive = conv.id === currentConvId
            const isEditing = editingId === conv.id
            const displayTitle = conv.title.length > 14 ? conv.title.slice(0, 14) + '…' : conv.title

            if (isEditing) {
              return (
                <div key={conv.id} className="conv-item conv-item--editing">
                  <input
                    type="text"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') handleSaveRename(conv.id)
                      if (e.key === 'Escape') setEditingId(null)
                    }}
                    className="conv-item-edit-input"
                    autoFocus
                  />
                  <button className="conv-item-action" onClick={() => handleSaveRename(conv.id)}>
                    ✓
                  </button>
                  <button
                    className="conv-item-action conv-item-action--danger"
                    onClick={() => setEditingId(null)}
                  >
                    ✕
                  </button>
                </div>
              )
            }

            return (
              <div key={conv.id} className={`conv-item ${isActive ? 'conv-item--active' : ''}`}>
                <button className="conv-item-title" onClick={() => switchConversation(conv.id)}>
                  {isActive && <span className="conv-item-dot">●</span>}
                  {displayTitle}
                </button>
                <button
                  className="conv-item-action"
                  onClick={() => handleStartRename(conv.id, conv.title)}
                  title="重命名"
                >
                  ✎
                </button>
                <button
                  className="conv-item-action conv-item-action--danger"
                  onClick={() => setDeleteConfirmId(conv.id)}
                  title="删除"
                >
                  ✕
                </button>
              </div>
            )
          })
        )}
      </div>

      {deleteConfirmId && (
        <ConfirmDialog
          title="删除对话"
          message="删除后这条对话的记录不再出现在历史里，长期记忆不受影响。"
          onConfirm={() => handleDelete(deleteConfirmId)}
          onCancel={() => setDeleteConfirmId(null)}
        />
      )}
    </div>
  )
}
