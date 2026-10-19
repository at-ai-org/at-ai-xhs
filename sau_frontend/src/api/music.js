import { http } from '@/utils/request'
import { withToken } from '@/utils/session'

// 背景音乐 API（at-ai-xhs 改造新增）
// 后端路由前缀 /music，见 sau_backend_music.py

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'

export const musicApi = {
  /** 音乐库列表 */
  list() {
    return http.get('/music/list')
  },

  /** 上传音乐（formData：file / name） */
  upload(formData, onUploadProgress) {
    return http.upload('/music/upload', formData, onUploadProgress)
  },

  /** 删除音乐（连同后台文件） */
  remove(id) {
    return http.post(`/music/${id}/delete`, {})
  },

  /**
   * 给一条素材合成背景音乐 → { task_id }
   * config 透传给 axios（批量合成时用 { silent: true } 自己收集错误）。
   */
  compose(material, musicId, config = {}) {
    return http.post('/music/compose', { material, music_id: musicId }, config)
  },

  /** 合成进度（前端进度条靠它） */
  getTask(taskId) {
    return http.get(`/music/task/${taskId}`)
  },

  /** 中断合成 */
  cancelTask(taskId) {
    return http.post(`/music/task/${taskId}/cancel`, {})
  },

  /** 全量「素材 → 背景音乐状态」，素材管理列表轮询用 */
  statuses() {
    return http.get('/music/statuses')
  },

  /**
   * 试听地址。
   * `<audio src>` 没法自定义请求头，token 只能挂在查询串上
   * （后端 web_auth.token_from 同时接受请求头和查询串，和扫码登录的 SSE 是同一套）。
   */
  fileUrl(id) {
    return withToken(`${API_BASE}/music/${id}/file`)
  }
}

export default musicApi
