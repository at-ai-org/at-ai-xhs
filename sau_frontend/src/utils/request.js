import axios from 'axios'
import { ElMessage } from 'element-plus'
import { clearSession, getToken } from '@/utils/session'

// 创建axios实例
const request = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409',
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
request.interceptors.request.use(
  (config) => {
    // 带上下载态的 token。后端在 myUtils/web_auth.py 的 token_from() 里读
    // `Authorization: Bearer xxx`；SSE 那种没法设请求头的接口改用查询串
    // （见 utils/session.js 的 withToken）。
    const token = getToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 登录接口自己会返回 401（密码错），不能拿它去触发"登录过期"那一套，
// 否则用户在登录页输错密码会被立刻"重定向到登录页"，提示也看不清。
function isLoginRequest(config) {
  return (config?.url || '').includes('/auth/login')
}

// 多个请求同时失效时（页面一加载会并发好几个），只提示一次、只跳一次
let redirecting = false

function handleSessionExpired() {
  clearSession()
  if (redirecting) return
  redirecting = true
  ElMessage.warning('登录已过期，请重新登录')
  // 这里刻意不 import router：router → views → api → request 会绕成循环依赖，
  // 而改 hash 本来就等价于走一次路由。
  if (!window.location.hash.startsWith('#/login')) {
    window.location.hash = '#/login'
  }
  // 留一点时间让当前这轮并发请求的提示都散掉，再允许下一次提示
  setTimeout(() => {
    redirecting = false
  }, 1000)
}

// 响应拦截器
request.interceptors.response.use(
  (response) => {
    const { data } = response
    
    // 根据后端接口规范处理响应
    if (data.code === 200 || data.success) {
      return data
    } else {
      ElMessage.error(data.msg || data.message || '请求失败')
      return Promise.reject(new Error(data.msg || data.message || '请求失败'))
    }
  },
  (error) => {
    console.error('响应错误:', error)
    
    // 处理HTTP错误状态码
    if (error.response) {
      const { status, data } = error.response

      // 401：token 缺失 / 过期 / 被改过（例如 settings.json 换了 secret_key）。
      // 统一在这里收口——清掉本地登录态并回登录页，业务代码不用各自处理。
      if (status === 401 && !isLoginRequest(error.config)) {
        const serverMsg = data && typeof data === 'object' ? data.msg : ''
        // 已经跳过一次就不再重复弹（并发请求会一起 401）
        if (!redirecting) handleSessionExpired()
        else if (serverMsg) console.warn('401:', serverMsg)
        return Promise.reject(error)
      }

      // 后端出错时自己会带 msg（例如"视频文件不存在: xxx"、"还没有配置模型服务地址"），
      // 比"服务器内部错误"这种笼统提示有用得多，优先透出。
      // 注意：Flask 自己产生的错误页（如 413 的 HTML）不是 JSON，取不到 msg，走下面的兜底。
      const serverMsg = data && typeof data === 'object' ? data.msg || data.message : ''
      if (serverMsg) {
        ElMessage.error(serverMsg)
        return Promise.reject(error)
      }
      switch (status) {
        case 401:
          ElMessage.error('未授权，请重新登录')
          break
        case 403:
          ElMessage.error('拒绝访问')
          break
        case 404:
          ElMessage.error('请求地址不存在')
          break
        case 429:
          ElMessage.error('操作太频繁，请稍后再试')
          break
        case 500:
          ElMessage.error('服务器内部错误')
          break
        default:
          ElMessage.error('网络错误')
      }
    } else {
      ElMessage.error('网络连接失败')
    }
    
    return Promise.reject(error)
  }
)

// 封装常用的请求方法
export const http = {
  get(url, params) {
    return request.get(url, { params })
  },
  
  post(url, data, config = {}) {
    return request.post(url, data, config)
  },
  
  put(url, data, config = {}) {
    return request.put(url, data, config)
  },
  
  delete(url, params) {
    return request.delete(url, { params })
  },
  
  upload(url, formData, onUploadProgress) {
    return request.post(url, formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      },
      onUploadProgress
    })
  }
}

export default request
