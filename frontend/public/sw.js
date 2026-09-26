// 只为满足 PWA 安装资格而存在的 service worker。
// 刻意不注册 fetch、不缓存任何资源：一旦缓存就会破坏 Vite 热更新，
// 出现「改了代码页面却不更新」。activate 时顺手清掉历史缓存。

self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys()
      await Promise.all(keys.map((key) => caches.delete(key)))
      await self.clients.claim()
    })()
  )
})
