import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  handleReload = () => {
    // 渲染期报错往往是确定性的：只在内存里清掉状态，下一帧会再崩一次。
    // 对不懂技术的用户来说，"刷新一下"是唯一说得通、也真能自救的动作。
    window.location.reload()
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100dvh',
            background: '#F9F6F2',
            fontFamily:
              "'Segoe UI', 'SF Pro Display', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif",
          }}
        >
          <div
            style={{
              textAlign: 'center',
              padding: '48px',
              maxWidth: '420px',
              background: '#FFFFFF',
              borderRadius: '24px',
              boxShadow: '0 8px 24px rgba(45,36,32,0.08)',
            }}
          >
            <div
              style={{
                width: '56px',
                height: '56px',
                borderRadius: '50%',
                background: 'linear-gradient(135deg, #E8A87C, #D4785C)',
                color: '#fff',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '24px',
                fontWeight: 700,
                margin: '0 auto 16px',
              }}
            >
              !
            </div>
            <h2
              style={{ color: '#2D2420', marginBottom: '8px', fontSize: '20px', fontWeight: 700 }}
            >
              界面崩了一下
            </h2>
            <p
              style={{ color: '#7A6E64', fontSize: '14px', marginBottom: '24px', lineHeight: 1.7 }}
            >
              界面刚才崩了一下。点「重新载入」就好——你说过的话和它记住的东西都存在这台电脑上，
              不会因为这次崩溃丢掉。
            </p>
            <details style={{ marginBottom: '20px', textAlign: 'left' }}>
              <summary style={{ cursor: 'pointer', color: '#B0A59A', fontSize: '12px' }}>
                错误详情
              </summary>
              <pre
                style={{
                  marginTop: '8px',
                  padding: '12px',
                  background: '#F8F3ED',
                  borderRadius: '8px',
                  fontSize: '11px',
                  overflow: 'auto',
                  color: '#5A4E44',
                  whiteSpace: 'pre-wrap',
                  fontFamily: "'SF Mono', 'Fira Code', monospace",
                }}
              >
                {this.state.error?.message}
                {this.state.error?.stack}
              </pre>
            </details>
            <button
              onClick={this.handleReload}
              style={{
                padding: '12px 28px',
                borderRadius: '14px',
                border: 'none',
                background: '#D4785C',
                color: '#fff',
                fontSize: '15px',
                fontWeight: 600,
                cursor: 'pointer',
                boxShadow: '0 2px 8px rgba(212,120,92,0.2)',
                transition: 'background 0.2s, transform 0.15s',
              }}
              onMouseEnter={(e) => {
                ;(e.target as HTMLElement).style.background = '#C0684C'
              }}
              onMouseLeave={(e) => {
                ;(e.target as HTMLElement).style.background = '#D4785C'
              }}
            >
              重新载入
            </button>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}
