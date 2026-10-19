import { http } from '@/utils/request'

// 发布中心相关 API（at-ai-xhs 改造新增）
// 后端路由前缀 /publish，见 sau_backend_publish.py

/** 平台定义。前后端都按这套 key 对齐：1 小红书 / 2 视频号 / 3 抖音 / 4 快手 / 5 拼多多视频
 *  拼多多这条只有「达人」身份（扫码），发布时不挂商品——达人挂品要先在拼多多后台
 *  过「匹配度测试」，见 uploader/pdd_uploader/main.py。 */
export const PLATFORMS = [
  { key: 1, name: '小红书' },
  { key: 2, name: '视频号' },
  { key: 3, name: '抖音' },
  { key: 4, name: '快手' },
  { key: 5, name: '拼多多视频' }
]

export const platformName = (key) => PLATFORMS.find((p) => p.key === key)?.name || `平台${key}`

/** 任务状态 → 展示文案 / 标签颜色 */
export const STATUS_META = {
  queued: { text: '排队中', type: 'info' },
  running: { text: '发布中', type: 'primary' },
  success: { text: '成功', type: 'success' },
  failed: { text: '失败', type: 'danger' },
  cancelled: { text: '已取消', type: 'warning' },
  interrupted: { text: '已中断', type: 'warning' }
}

/** 发布来源：同一条记录可能来自发布中心的批量提交，也可能来自小红书工作台的单条发布 */
export const SOURCE_TEXT = {
  publish_center: '发布中心',
  xhs_workbench: '小红书工作台'
}

export const statusMeta = (status) => STATUS_META[status] || { text: status || '-', type: 'info' }

export const publishApi = {
  /**
   * 提交一批发布任务。
   * 后端会拆成「每个素材 × 每个平台 × 每个账号」一个任务，逐个执行。
   * 返回 { job_id, task_count, tasks[] }
   */
  submit(payload) {
    return http.post('/publish/submit', payload)
  },

  /**
   * 重复发布检查：这批「素材 × 平台 × 账号」里，哪些此前已经发过了？
   *
   * 同一个账号再发一次同一条素材，平台上就会出现两条重复内容。所以点「开始发布」
   * 之前先问一次，有重复就把明细列出来让人确认。
   * 返回 { conflicts[], total, published, in_flight, task_count }。
   */
  checkPublished(payload) {
    return http.post('/publish/check-published', payload)
  },

  /** 批次实时进度（含平台 / 账号 / 任务三层和每个任务的最新动作） */
  getJob(jobId) {
    return http.get(`/publish/job/${jobId}`)
  },

  /** 单个任务详情（逐步结果 + 完整日志尾巴） */
  getTask(taskId) {
    return http.get(`/publish/task/${taskId}`)
  },

  /** 最近的批次。传 { active: 1 } 只看还没跑完的（刷新页面后据此恢复进度） */
  listJobs(params = {}) {
    return http.get('/publish/jobs', params)
  },

  /**
   * 中断**单个**任务：排队中的、正在跑的都能中断。
   *
   * 后端会立刻给结论（不会等那个可能卡死的线程收尾），所以刷新进度就能看到「已取消」。
   */
  cancelTask(taskId) {
    return http.post(`/publish/task/${taskId}/cancel`, {})
  },

  /**
   * 取消批次里的任务。
   * 默认只取消**还没开始**的；传 { running: true } 连正在跑的一起中断
   * （中断一个发到一半的任务有代价，所以得用户明确要）。
   */
  cancelJob(jobId, { running = false } = {}) {
    return http.post(`/publish/job/${jobId}/cancel`, { running })
  },

  /**
   * 发布记录。支持 page / page_size / platform / account / status / keyword / start / end
   */
  listRecords(params = {}) {
    return http.get('/publish/records', params)
  },

  /** 单条记录详情（含逐步结果与日志） */
  getRecord(recordId) {
    return http.get(`/publish/records/${recordId}`)
  },

  /**
   * 首页 Dashboard 的多维统计：总量与成败、按平台、按账号、近 14 天趋势、
   * 来源占比、素材与文案覆盖。只读，一次拉全。
   */
  statsOverview() {
    return http.get('/publish/stats/overview')
  },

  /**
   * 当前所有「发布途中在等人输入」的请求（抖音短信验证码…）。
   * 刻意做成全局接口：验证码等的是坐在电脑前的人，跟用户停在哪个页面无关，
   * 所以由布局层 App.vue 轮询它来弹窗。
   */
  listPrompts() {
    return http.get('/publish/prompts')
  },

  /** 回答一次等人输入。传 { skip: true } 表示跳过，发布流程照旧往下走。 */
  submitPrompt(promptId, payload = {}) {
    return http.post(`/publish/prompts/${promptId}`, payload)
  }
}

export default publishApi
