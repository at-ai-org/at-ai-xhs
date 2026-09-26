import { http } from '@/utils/request'

// 小红书 Web 操作相关 API（at-ai-xhs 改造新增）
// 后端路由前缀 /xhs，见 sau_backend_xhs.py

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'

export const xhsApi = {
  /** 账号列表。check=true 会逐个校验 cookie（较慢） */
  listAccounts(check = false) {
    return http.get('/xhs/accounts', { params: { check: check ? 1 : 0 } })
  },

  /** 校验单个账号 cookie */
  checkAccount(account) {
    return http.get('/xhs/check', { params: { account } })
  },

  /**
   * 页面契约自检（不提交任何内容）。
   * 传 video 则做深自检：先上传测试视频把编辑器撑出来，才能验证合集等控件。
   */
  selftest(account, video = null) {
    return http.post('/xhs/selftest', { account, video: video || null })
  },

  /**
   * 读取账号合集。
   * 不传 video → 返回缓存（秒回）；传 video → 真实读取（会上传测试视频但不发布，分钟级）。
   */
  listCollections(account, video = null) {
    return http.post('/xhs/collections', { account, video: video || null })
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

  // ---- 模型设置（OpenAI 兼容格式）----

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
   */
  analyze(video, hints = '', save = false) {
    return http.post('/xhs/analyze', { video, hints, save })
  },

  /** 上传视频到素材库，返回素材文件名 */
  uploadVideo(formData, onUploadProgress) {
    return http.upload('/upload', formData, onUploadProgress)
  },

  /** 素材列表（直接读 videoFile/ 目录） */
  listMaterials() {
    return http.get('/xhs/materials')
  },

  /**
   * 扫码登录的 SSE 地址。
   * 二维码与登录结果都由后端通过该连接推送（见 sau_backend_xhs.py 的 /xhs/login/stream）。
   */
  loginStreamUrl(account) {
    return `${API_BASE}/xhs/login/stream?account=${encodeURIComponent(account)}`
  }
}

export default xhsApi
