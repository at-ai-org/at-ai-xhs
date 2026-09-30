import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  clearSession,
  getToken,
  getUsername,
  setSession as persistSession
} from '@/utils/session'

// 登录态（at-ai-xhs 改造新增）。
//
// 之前这个 store 是个占位实现（userInfo / isLoggedIn 都是写死的，没有任何地方在用）。
// 现在它是真的：数据的**唯一来源是 localStorage**（见 utils/session.js），
// store 只是给它套一层响应式的壳，方便界面订阅。
// 刷新页面后 store 重建，会从 localStorage 里读回同一个 token。
export const useUserStore = defineStore('user', () => {
  const token = ref(getToken())
  const username = ref(getUsername())

  const isLoggedIn = computed(() => !!token.value)

  /** 登录成功后调用：落盘 + 更新内存。 */
  const setSession = (newToken, newUsername = '') => {
    persistSession(newToken, newUsername)
    token.value = newToken || ''
    username.value = newUsername || ''
  }

  /** 退出登录 / 登录过期：只清本地。后端签发的 token 是无状态的（签名 + 有效期），
   *  没有服务端会话要销毁；要"让所有已发出的 token 立刻作废"就换 settings.json 里的
   *  auth.secret_key（见 myUtils/web_auth.py）。 */
  const logout = () => {
    clearSession()
    token.value = ''
    username.value = ''
  }

  return {
    token,
    username,
    isLoggedIn,
    setSession,
    logout
  }
})

export default useUserStore
