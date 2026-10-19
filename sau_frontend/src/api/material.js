import { http } from '@/utils/request'
import { withToken } from '@/utils/session'

// 素材文件（预览/下载）走的是后端 `/getFile`，和接口同一个 baseURL
const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'

// 素材管理API
export const materialApi = {
  // 获取所有素材
  getAllMaterials: () => {
    return http.get('/getFiles')
  },
  
  // 上传素材
  uploadMaterial: (formData, onUploadProgress) => {
    // 使用http.upload方法，它已经配置了正确的Content-Type
    return http.upload('/uploadSave', formData, onUploadProgress)
  },
  
  // 删除素材
  deleteMaterial: (id) => {
    return http.get(`/deleteFile?id=${id}`)
  },
  
  // 下载素材
  downloadMaterial: (filePath) => {
    // 后端没有 /download 路由，下载和预览走同一个 /getFile（会带 Content-Disposition 之外的原始字节）
    const filename = String(filePath || '').split('/').pop()
    return getFileUrl(filename)
  },
  
  // 获取素材预览URL
  getMaterialPreviewUrl: (filename) => {
    return getFileUrl(filename)
  }
}

/**
 * 拼 `/getFile` 的地址。
 *
 * 两条缺一不可，都是踩过的坑：
 *
 * 1. **必须编码**：素材名可能含空格、中文、emoji（AI 重命名后会），直接拼进 query
 *    会被浏览器截断或解析错，表现就是"预览一直加载中"。
 * 2. **必须带 token**（`withToken` 挂在查询串上）：这张地址是给 `<video>` / `<img>` /
 *    `window.open` 用的，它们**带不了请求头**，而后端的 before_request 守卫只放行
 *    页面外壳和静态资源——不带 token 就是 401，界面上表现为"预览转圈转到天荒地老"
 *    （而且 `<video>` 里的 `<source>` 出错时不冒泡，连失败提示都不会弹）。
 *    和背景音乐试听（`musicApi.fileUrl`）、扫码登录的 SSE 是同一套做法。
 */
function getFileUrl(filename) {
  return withToken(`${API_BASE}/getFile?filename=${encodeURIComponent(filename || '')}`)
}