import { http } from '@/utils/request'

// 小红书 Web 操作相关 API（at-ai-xhs 改造新增）
// 后端路由前缀 /xhs，见 sau_backend_xhs.py
//
// 账号不再单独扫码登录：统一从「账号管理」选小红书平台的账号，
// 请求体传 account_id（user_info 的 id），后端据此解析 cookiesFile/ 下的 cookie。

export const xhsApi = {
  /**
   * 读取账号合集。
   * 不传 video → 返回缓存（秒回）；传 video → 真实读取（会上传测试视频但不发布，分钟级）。
   */
  listCollections(accountId, video = null) {
    return http.post('/xhs/collections', { account_id: accountId, video: video || null })
  },

  /** 创建发布任务，返回 { task_id } */
  publish(payload) {
    return http.post('/xhs/publish', payload)
  },

  /** 查询发布任务进度（含每步结果） */
  getTask(taskId) {
    return http.get(`/xhs/task/${taskId}`)
  },

  /** 二次确认：确认后才真正执行发布（可带上改好的文案） */
  confirmTask(taskId, payload = {}) {
    return http.post(`/xhs/task/${taskId}/confirm`, payload)
  },

  /** 重新生成文案（仅「待确认」状态可用） */
  regenerateTask(taskId, hints = null) {
    return http.post(`/xhs/task/${taskId}/regenerate`, hints === null ? {} : { hints })
  },

  /** 放弃一个还没真正发布的任务 */
  cancelTask(taskId) {
    return http.post(`/xhs/task/${taskId}/cancel`, {})
  },

  /** 最近的任务列表 */
  listTasks() {
    return http.get('/xhs/tasks')
  },

  // ---- 设置：视觉理解模型（OpenAI 兼容格式）----

  /** 读取设置（api_key 只返回掩码） */
  getSettings() {
    return http.get('/xhs/settings')
  },

  /** 保存设置。vision.api_key 传空字符串表示不修改 */
  saveSettings(payload) {
    return http.post('/xhs/settings', payload)
  },

  /** 连通性测试：验证 url / key / model 是否可用 */
  testSettings(payload = {}) {
    return http.post('/xhs/settings/test', payload)
  },

  /** 发布页默认值（例如默认合集） */
  getDefaults() {
    return http.get('/xhs/defaults')
  },

  /**
   * AI 生成文案：抽首帧+尾帧交给视觉模型。
   * save=true 时顺便把结果存到素材上，之后发布中心选中素材即可自动带出。
   * config 透传给 axios（批量生成时用 { silent: true } 自己收集错误）。
   */
  analyze(video, hints = '', save = false, config = {}) {
    return http.post('/xhs/analyze', { video, hints, save }, config)
  },

  /** 上传视频到素材库，返回素材文件名 */
  uploadVideo(formData, onUploadProgress) {
    return http.upload('/upload', formData, onUploadProgress)
  },

  /** 素材列表（直接读 videoFile/ 目录） */
  listMaterials() {
    return http.get('/xhs/materials')
  }
}

export default xhsApi
