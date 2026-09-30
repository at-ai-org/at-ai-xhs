import { http } from '@/utils/request'

// 后台登录相关 API（at-ai-xhs 改造新增）
// 后端路由见 myUtils/web_auth.py 的 install()：/auth/login、/auth/status
export const authApi = {
  /**
   * 登录。账号固定 admin，密码在 settings.json 的 auth.password（首次启动随机生成）。
   * 成功返回 { token, username, expires_in }。
   */
  login(username, password) {
    return http.post('/auth/login', { username, password })
  },

  /**
   * 探活：当前 token 还有效吗。
   * 这个接口**不需要登录也能调**，未登录时回 `{ authenticated: false }` 而不是 401，
   * 所以可以放心在启动时用它来验证本地存着的 token 是否已经失效。
   */
  status() {
    return http.get('/auth/status')
  }
}

export default authApi
