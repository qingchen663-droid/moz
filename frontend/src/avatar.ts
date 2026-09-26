// 头像现在存在后端（backend/avatars/<user_id>.jpg），随导出快照一起走。
// 这里的 localStorage 键只用于把旧版本地头像一次性迁移上去。

const LEGACY_AVATAR_KEY = 'moz_avatar'
const MAX_EDGE = 256

export function takeLegacyLocalAvatar(): string | null {
  const value = localStorage.getItem(LEGACY_AVATAR_KEY)
  return value && value.startsWith('data:image/jpeg') ? value : null
}

export function clearLegacyLocalAvatar() {
  localStorage.removeItem(LEGACY_AVATAR_KEY)
}

// 原图动辄几 MB，统一裁成正方形并压到 256px，避免上传超大文件
export function fileToAvatarDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.onload = () => {
      const img = new Image()
      img.onerror = () => reject(new Error('这不是有效的图片文件'))
      img.onload = () => {
        const side = Math.min(img.width, img.height)
        const sx = (img.width - side) / 2
        const sy = (img.height - side) / 2
        const canvas = document.createElement('canvas')
        canvas.width = MAX_EDGE
        canvas.height = MAX_EDGE
        const ctx = canvas.getContext('2d')
        if (!ctx) {
          reject(new Error('浏览器不支持图片处理'))
          return
        }
        ctx.fillStyle = '#FFFFFF'
        ctx.fillRect(0, 0, MAX_EDGE, MAX_EDGE)
        ctx.drawImage(img, sx, sy, side, side, 0, 0, MAX_EDGE, MAX_EDGE)
        resolve(canvas.toDataURL('image/jpeg', 0.86))
      }
      img.src = String(reader.result)
    }
    reader.readAsDataURL(file)
  })
}
