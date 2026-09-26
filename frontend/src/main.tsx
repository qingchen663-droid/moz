import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

const SW_URL = '/sw.js'

function swScriptUrl(reg: ServiceWorkerRegistration): string {
  return (reg.active || reg.waiting || reg.installing)?.scriptURL || ''
}

// 注册零缓存壳（PWA 安装资格），并清掉历史遗留的其他 SW，
// 避免旧缓存壳让"改了代码页面不更新"。失败只影响安装，不影响应用本身。
function registerShellServiceWorker() {
  if (!('serviceWorker' in navigator)) return
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .getRegistrations()
      .then((regs) =>
        Promise.all(
          regs.filter((reg) => !swScriptUrl(reg).endsWith(SW_URL)).map((reg) => reg.unregister())
        )
      )
      .then(() => navigator.serviceWorker.register(SW_URL))
      .catch((e) => console.warn('service worker 注册失败:', e))
  })
}

registerShellServiceWorker()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
