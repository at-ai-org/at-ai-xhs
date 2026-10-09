import { http } from '@/utils/request'

// 账号数据（粉丝 / 互动）相关 API（at-ai-xhs 改造新增）。
//
// 后端在 `sau_backend_stats.py`，抓取本体在 `myUtils/account_stats/`。
// 刻意**不放在 xhs.js 里**：这份数据对四个平台一视同仁，跟小红书没关系。
export const accountStatsApi = {
  /**
   * 一个账号的粉丝 / 互动 / 作品数据（「详情」弹窗用）。
   *
   * **第一次点击会真的去平台抓**（小红书 / 快手 / 视频号要开浏览器，几秒到十几秒；
   * 抖音走 HTTP，一两秒），抓完落库。之后再打开就是缓存，基本瞬间。
   *
   * @param {number} id         账号 id（user_info.id）
   * @param {object} [options]
   * @param {boolean} [options.refresh]    true = 忽略缓存重新抓（「刷新」按钮）
   * @param {number}  [options.mediaLimit] 作品条数上限，默认 20
   * @param {boolean} [options.withMedia]  false = 不取作品列表（更快）
   */
  getStats(id, options = {}) {
    const params = { id }
    if (options.refresh) params.refresh = 1
    if (options.mediaLimit) params.media_limit = options.mediaLimit
    if (options.withMedia === false) params.with_media = 0
    return http.get('/account-stats', params)
  },

  /**
   * 所有账号的粉丝数一览。**只读后端缓存，不会触发抓取**。
   *
   * 列表页拿它做"已经看过的账号显示上次的数字"，所以它很轻，可以随列表一起拉。
   */
  getSummary() {
    return http.get('/account-stats/summary')
  },

  /**
   * **手动同步**：拉平台实时数据覆盖本地库（详情页的「同步」按钮）。
   *
   * 和 `getStats(id, { refresh: true })` 的区别在语义：
   * 那个是"打开详情顺手更新一下"，这个是**明确的同步动作**——不看缓存 TTL、必抓、必覆盖，
   * 并额外回一个 `sync.status`（ok / invalid / busy / error）让界面给出准确提示。
   *
   * 返回结构和 `getStats` 一致，所以同步完可以直接用它刷新弹窗。
   *
   * @param {number} id 账号 id
   * @param {object} [options] `{ mediaLimit, withMedia }`
   */
  sync(id, options = {}) {
    const body = { id }
    if (options.mediaLimit) body.media_limit = options.mediaLimit
    if (options.withMedia === false) body.with_media = false
    return http.post('/account-stats/sync', body)
  }
}

export default accountStatsApi
