import React, { useEffect, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import type { ModelPresets } from '../types'
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
  }, [])

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

  const handleSaveConfig = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!formModel.trim() || !formBaseUrl.trim()) {
      setSaveMessage({ type: 'error', text: '模型名称与 Base URL 不能为空' })
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
      setSaveMessage({ type: 'success', text: '配置保存成功，已即时生效！无需重启服务。' })
    } catch (err: any) {
      setSaveMessage({ type: 'error', text: err?.message || '保存失败，请检查网络或后端' })
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="dialog-overlay" onClick={onClose}>
        <div className="dialog-content model-dialog" onClick={(e) => e.stopPropagation()}>
          <div className="dialog-header">
            <h2>模型配置</h2>
            <button className="close-btn" onClick={onClose}>
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
      <div className="dialog-content model-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-header">
          <h2>模型配置</h2>
          <button className="close-btn" onClick={onClose}>
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
                      {modelsLoading ? '查询中...' : '获取模型列表'}
                    </button>
                    {models && (
                      <span className="model-fetch-hint">
                        {models.length} 个可用，点一下填进上面
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
                    <span>这个模型能看懂图片（多模态）</span>
                  </label>
                  <div className="model-vision-hint">
                    {formMultimodal
                      ? '已开启：聊天输入框会显示上传图片的按钮，发来的图片会交给模型识别。'
                      : '未开启：模型名里没有视觉关键词时默认关闭。如果你的服务方其实支持图片，勾上它才能让我看懂你发的图。'}
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
