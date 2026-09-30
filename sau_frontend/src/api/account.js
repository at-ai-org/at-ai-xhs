import { http } from '@/utils/request'

// 账号管理相关API
export const accountApi = {
  // 获取有效账号列表（带验证）
  getValidAccounts() {
    return http.get('/getValidAccounts')
  },

  // 获取账号列表（不带验证，快速加载）
  getAccounts() {
    return http.get('/getAccounts')
  },

  // 添加账号
  addAccount(data) {
    return http.post('/account', data)
  },

  // 更新账号
  updateAccount(data) {
    return http.post('/updateUserinfo', data)
  },

  // 删除账号
  deleteAccount(id) {
    return http.get(`/deleteAccount?id=${id}`)
  },

  // ---- 账号会话定时续期（at-ai-xhs 改造新增）----
  // 后端是 sau_backend_session.py 的 /session-renew，调度实现在 myUtils/session_renew.py。
  // 放在"账号管理"这条 API 里（而不是 xhs.js）：续期的对象是账号管理下的**全部账号**，
  // 四个平台一视同仁，跟小红书没关系。

  /** 当前状态：开关、时间点、上次续期结果、下次续期时间 */
  getSessionRenew() {
    return http.get('/session-renew')
  },

  /** 改开关 / 时间点。两个字段都可选，只传哪个改哪个 */
  saveSessionRenew(payload) {
    return http.post('/session-renew', payload)
  }
}