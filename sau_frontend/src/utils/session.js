// 登录态在浏览器侧的存取（at-ai-xhs 改造新增）。
//
// 单独抽一个模块而不是直接写 localStorage，是为了避免循环依赖：
//   request.js（要在 401 时清登录态）、router（要在路由守卫里读）、
//   stores/user.js（要给界面用）三方都要碰这份数据，
//   如果互相 import 就会绕成一个环（router → views → api → request → router）。
//
// 真正的登录态由后端签发（见 myUtils/web_auth.py），这里只是把它存下来。

const TOKEN_KEY = 'at-ai-xhs-token'
const USERNAME_KEY = 'at-ai-xhs-username'

// 后端签发时的有效期，仅用于本文件注释参考：7 天（TOKEN_MAX_AGE）。
// 前端不做过期判断，过期由后端返回 401、再由 request.js 统一处理。

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch (e) {
    // 隐私模式 / 禁用了 localStorage：退化成"每次都要重新登录"，不影响功能
    return ''
  }
}

export function getUsername() {
  try {
    return localStorage.getItem(USERNAME_KEY) || ''
  } catch (e) {
    return ''
  }
}

export function isLoggedIn() {
  return !!getToken()
}

export function setSession(token, username = '') {
  try {
    localStorage.setItem(TOKEN_KEY, token || '')
    localStorage.setItem(USERNAME_KEY, username || '')
  } catch (e) {
    /* 同上 */
  }
}

export function clearSession() {
  try {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USERNAME_KEY)
  } catch (e) {
    /* 同上 */
  }
}

/**
 * 把 token 拼到 URL 上。
 *
 * `EventSource` 没法自定义请求头，"扫码登录"那两个 SSE 接口只能靠查询串带上 token
 * （后端在 myUtils/web_auth.py 的 token_from() 里同时接受请求头和查询串）。
 */
export function withToken(url) {
  const token = getToken()
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}
