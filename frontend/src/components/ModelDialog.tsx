import React, { useEffect, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import type { ModelPresets, SavedModel } from '../types'
import './ModelDialog.css'

interface Props {
  onClose: () => void
}

export default function ModelDialog({ onClose }: Props) {
  const { modelConfig, loadModelConfig } = useStore()
  const [presets, setPresets] = useState<ModelPresets>({})
  const [selectedPreset, setSelectedPreset] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // 动态表单状态
  const [formModel, setFormModel] = useState('')
  const [formBaseUrl, setFormBaseUrl] = useState('')
  const [formApiKey, setFormApiKey] = useState('')
  const [formThinking, setFormThinking] = useState(false)
  const [formMultimodal, setFormMultimodal] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveMessage, setSaveMessage] = useState<{
    type: 'success' | 'error'
    text: string
  } | null>(null)

  // 「获取模型列表」结果
  const [models, setModels] = useState<string[] | null>(null)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelsError, setModelsError] = useState<string | null>(null)

  // 我存的模型
  const [saved, setSaved] = useState<SavedModel[]>([])
  const [savedMax, setSavedMax] = useState(12)
  const [saveAsName, setSaveAsName] = useState('')
  const [savedBusy, setSavedBusy] = useState(false)

  useEffect(() => {
    loadModelConfig()
    api
      .getModelPresets()
      .then((data) => {
        setPresets(data)
        setLoading(false)
      })
      .catch((err) => {
        console.error('加载模型预设失败:', err)
        setLoading(false)
      })
    api
      .getSavedModels()
      .then((r) => {
        setSaved(r.items ?? [])
        setSavedMax(r.max ?? 12)
      })
      .catch(() => {
        /* 后端没起来时这块就是空的，不额外打扰用户 */
      })
  }, [])

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [onClose])

  // 当选择预设或自定义时，初始化表单值
  const handleSelectPreset = (name: string) => {
    setSelectedPreset(name)
    setSaveMessage(null)
    if (name === '自定义') {
      setFormModel(modelConfig?.model || '')
      setFormBaseUrl(modelConfig?.base_url || '')
      setFormApiKey('')
      // 两个开关都要以当前生效配置起步，否则保存时会把用户已开的项静默关掉
      setFormThinking(!!modelConfig?.use_thinking)
      setFormMultimodal(!!modelConfig?.multimodal)
    } else if (presets[name]) {
      const p = presets[name]
      setFormModel(p.model || '')
      setFormBaseUrl(p.base_url || '')
      setFormApiKey('')
      setFormThinking(!!p.use_thinking)
      setFormMultimodal(!!modelConfig?.multimodal)
    }
  }

  const handleFetchModels = async () => {
    setModelsLoading(true)
    setModelsError(null)
    setModels(null)
    try {
      const res = await api.listProviderModels(formBaseUrl.trim(), formApiKey.trim())
      if (res.ok && res.models.length) {
        setModels(res.models)
      } else {
        setModelsError(res.error || '没有拉到任何模型，请手动填写模型名称')
      }
    } catch (err) {
      setModelsError(err instanceof Error ? err.message : '请求失败，请确认后端在运行')
    } finally {
      setModelsLoading(false)
    }
  }

  const errText = (e: unknown) => (e instanceof Error ? e.message : '后端没应答')

  const handleSaveCurrentAs = async () => {
    const name = saveAsName.trim()
    if (!name) {
      setSaveMessage({ type: 'error', text: '先给这套配置起个名字，比如「中转 deepseek」。' })
      return
    }
    setSavedBusy(true)
    setSaveMessage(null)
    try {
      const r = await api.saveModelAs(name)
      setSaved(r.items ?? [])
      setSavedMax(r.max ?? savedMax)
      setSaveAsName('')
      setSaveMessage({ type: 'success', text: `已存成「${r.saved}」，以后点一下就换回来。` })
    } catch (e) {
      setSaveMessage({ type: 'error', text: `没存上：${errText(e)}` })
    } finally {
      setSavedBusy(false)
    }
  }

  const handleUseSaved = async (name: string) => {
    setSavedBusy(true)
    setSaveMessage(null)
    try {
      const r = await api.useSavedModel(name)
      setSaved(r.items ?? [])
      await loadModelConfig()
      setSaveMessage({ type: 'success', text: `已切到「${name}」，下一条消息就用它。` })
    } catch (e) {
      setSaveMessage({ type: 'error', text: `没换成：${errText(e)}` })
    } finally {
      setSavedBusy(false)
    }
  }

  const handleDeleteSaved = async (name: string) => {
    if (!window.confirm(`不再要「${name}」这套配置？现在正在用的模型不受影响。`)) return
    setSavedBusy(true)
    try {
      const r = await api.deleteSavedModel(name)
      setSaved(r.items ?? [])
    } catch (e) {
      setSaveMessage({ type: 'error', text: `没删掉：${errText(e)}` })
    } finally {
      setSavedBusy(false)
    }
  }

  const handleSaveConfig = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!formModel.trim() || !formBaseUrl.trim()) {
      setSaveMessage({ type: 'error', text: '模型名字和接口地址都得填，少了哪一样 moz 都不知道发到哪。' })
      return
    }

    setSaving(true)
    setSaveMessage(null)
    try {
      await api.updateModelConfig({
        model: formModel.trim(),
        base_url: formBaseUrl.trim(),
        api_key: formApiKey.trim(),
        use_thinking: formThinking,
        multimodal: formMultimodal,
      })
      await loadModelConfig()
      setSaveMessage({
        type: 'success',
        text: '已保存，下一条消息就用它。这个中转回一句通常要 20~35 秒，别以为卡住了。',
      })
    } catch (err: any) {
      setSaveMessage({
        type: 'error',
        text: `保存没成功：${err?.message || '后端没应答'}。可以先点「查地址和密钥通不通」验证，再保存。`,
      })
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="dialog-overlay" onClick={onClose}>
        <div
          className="dialog-content model-dialog"
          role="dialog"
          aria-label="模型配置"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="dialog-header">
            <h2>模型配置</h2>
            <button className="close-btn" onClick={onClose} title="关闭">
              ×
            </button>
          </div>
          <div className="loading-state">加载中...</div>
        </div>
      </div>
    )
  }

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div
        className="dialog-content model-dialog"
        role="dialog"
        aria-label="模型配置"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="dialog-header">
          <h2>模型配置</h2>
          <button className="close-btn" onClick={onClose} title="关闭">
            ×
          </button>
        </div>

        <div className="dialog-body">
          {/* 当前运行中的模型 */}
          <div className="current-model-section">
            <div className="current-model-label">当前运行模型</div>
            <div className="current-model-info">
              <span className="model-name">{modelConfig?.model || '未知'}</span>
              {modelConfig?.multimodal && <span className="model-badge">多模态</span>}
            </div>
            <div className="current-model-url">{modelConfig?.base_url || ''}</div>
          </div>

          {/* 我存的模型：同一套中转/密钥下想切来切去的几份配置 */}
          <div className="saved-models-section">
            <h3>我存的模型</h3>
            {saved.length === 0 ? (
              <div className="saved-models-empty">
                还没存过。调好一套之后在下面起个名字存起来，以后点一下就切回来。
              </div>
            ) : (
              <div className="saved-models-list">
                {saved.map((m) => (
                  <div key={m.name} className={`saved-model ${m.is_active ? 'saved-model--active' : ''}`}>
                    <div className="saved-model-main">
                      <div className="saved-model-name">
                        {m.name}
                        {m.is_active && <span className="saved-model-now">使用中</span>}
                      </div>
                      <div className="saved-model-meta">
                        {m.model} · {(m.base_url || '').replace(/^https?:\/\//, '').slice(0, 24)}
                        {m.use_thinking ? ' · 深度思考' : ''}
                        {m.multimodal ? ' · 能发图' : ''}
                        {m.has_key ? '' : ' · 没带密钥'}
                      </div>
                    </div>
                    {!m.is_active && (
                      <button
                        className="saved-model-btn"
                        onClick={() => handleUseSaved(m.name)}
                        disabled={savedBusy}
                      >
                        用这个
                      </button>
                    )}
                    <button
                      className="saved-model-del"
                      onClick={() => handleDeleteSaved(m.name)}
                      title={`删掉「${m.name}」这套配置`}
                      disabled={savedBusy}
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="saved-model-save">
              <input
                className="form-input"
                value={saveAsName}
                maxLength={20}
                placeholder="把当前这套存成…（如：中转 deepseek）"
                onChange={(e) => setSaveAsName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleSaveCurrentAs()
                }}
              />
              <button
                type="button"
                className="model-fetch-btn"
                onClick={handleSaveCurrentAs}
                disabled={savedBusy}
                title="把正在用的这套模型、地址、密钥存成一个可点回来的名字"
              >
                存起来
              </button>
            </div>
            {saved.length >= savedMax && (
              <div className="saved-models-hint">最多存 {savedMax} 套，先删一套再存新的。</div>
            )}
          </div>

          {!selectedPreset ? (
            <>
              <div className="presets-section">
                <h3>选择模型或自定义</h3>
                <div className="presets-grid">
                  {Object.entries(presets).map(([name, preset]) => (
                    <div
                      key={name}
                      className="preset-card"
                      onClick={() => handleSelectPreset(name)}
                    >
                      <div className="preset-name">{name}</div>
                      <div className="preset-model">{preset.model}</div>
                      {preset.use_thinking && <span className="preset-badge">深度思考</span>}
                    </div>
                  ))}
                  <div
                    className="preset-card preset-custom"
                    onClick={() => handleSelectPreset('自定义')}
                  >
                    <div className="preset-name">✨ 自定义模型</div>
                    <div className="preset-model">直接输入任意模型、Base URL 和 Key</div>
                  </div>
                </div>
              </div>

              <div className="help-section">
                <h3>说明</h3>
                <ul className="help-list">
                  <li>
                    点击上方预设或自定义卡片，即可
                    <strong>直接在界面输入 API Key 或修改参数并即时保存</strong>。
                  </li>
                  <li>支持一键启用深度思考模式 (Thinking)。</li>
                  <li>无需手动重启后端服务，保存后新对话立即生效。</li>
                </ul>
              </div>
            </>
          ) : (
            <div className="model-edit-container">
              <div className="config-guide-header">
                <h3>{selectedPreset === '自定义' ? '自定义模型配置' : `配置 ${selectedPreset}`}</h3>
                <button className="close-guide-btn" onClick={() => setSelectedPreset(null)}>
                  返回选择
                </button>
              </div>

              {presets[selectedPreset]?.support_url && (
                <div className="preset-quick-link">
                  <span>密钥获取：</span>
                  <a
                    href={presets[selectedPreset].support_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="support-link"
                  >
                    {presets[selectedPreset].support_text} →
                  </a>
                </div>
              )}

              <form className="model-edit-form" onSubmit={handleSaveConfig}>
                <div className="form-group">
                  <label>模型名称 (Model)</label>
                  <input
                    type="text"
                    className="form-input"
                    value={formModel}
                    onChange={(e) => setFormModel(e.target.value)}
                    placeholder="如：deepseek-v4-flash, gpt-4o, qwen-max..."
                    required
                  />
                </div>

                <div className="form-group">
                  <label>接口地址 (Base URL)</label>
                  <input
                    type="text"
                    className="form-input"
                    value={formBaseUrl}
                    onChange={(e) => {
                      setFormBaseUrl(e.target.value)
                      setModels(null)
                      setModelsError(null)
                    }}
                    placeholder="如：https://api.deepseek.com/v1"
                    required
                  />
                </div>

                <div className="form-group">
                  <label>
                    API Key
                    <span className="label-tip">（留空则沿用已保存的密钥）</span>
                  </label>
                  <input
                    type="password"
                    className="form-input"
                    value={formApiKey}
                    onChange={(e) => {
                      setFormApiKey(e.target.value)
                      setModels(null)
                      setModelsError(null)
                    }}
                    placeholder="输入 sk-..."
                    autoComplete="off"
                  />
                </div>

                <div className="form-group">
                  <div className="model-fetch-row">
                    <button
                      type="button"
                      className="model-fetch-btn"
                      onClick={handleFetchModels}
                      disabled={modelsLoading || !formBaseUrl.trim()}
                      title={
                        formBaseUrl.trim() ? '用上面的地址和密钥查询可用模型' : '请先填写接口地址'
                      }
                    >
                      {modelsLoading ? '查询中...' : '查地址和密钥通不通'}
                    </button>
                    {models && (
                      <span className="model-fetch-hint">
                        能连上，{models.length} 个模型可用；点一下填进上面
                      </span>
                    )}
                    {!modelsLoading && !models && !modelsError && (
                      <span className="model-fetch-hint">
                        这一步只验证地址和密钥，不代表一定能出字
                      </span>
                    )}
                  </div>

                  {modelsError && <div className="model-fetch-error">{modelsError}</div>}

                  {models && (
                    <div className="model-pick-list">
                      {models.map((id) => (
                        <button
                          type="button"
                          key={id}
                          className={`model-pick-item ${id === formModel.trim() ? 'active' : ''}`}
                          onClick={() => setFormModel(id)}
                        >
                          {id}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                <div className="form-checkbox-group">
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={formThinking}
                      onChange={(e) => setFormThinking(e.target.checked)}
                    />
                    <span>开启深度思考模式 (Reasoning / Thinking)</span>
                  </label>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={formMultimodal}
                      onChange={(e) => setFormMultimodal(e.target.checked)}
                    />
                    <span>这个模型支持发图（多模态）</span>
                  </label>
                  <div className="model-vision-hint">
                    {formMultimodal
                      ? '已开启：输入框会出现上传按钮，图会跟着话一起发出去。这条中转有时会丢图，我说的不一定准。'
                      : '未开启：模型名里没有视觉关键词时默认关闭。如果你的服务方其实支持图片，勾上才会显示上传按钮。'}
                  </div>
                </div>

                {saveMessage && (
                  <div className={`model-dialog-message model-dialog-message--${saveMessage.type}`}>
                    {saveMessage.text}
                  </div>
                )}

                <div className="form-actions">
                  <button type="submit" className="save-btn" disabled={saving}>
                    {saving ? '保存生效中...' : '💾 保存并立即生效'}
                  </button>
                  <button
                    type="button"
                    className="cancel-btn"
                    onClick={() => setSelectedPreset(null)}
                  >
                    取消
                  </button>
                </div>
              </form>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
