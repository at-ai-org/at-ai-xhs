import { computed, reactive } from 'vue'
import { ElMessage } from 'element-plus'
// 续期接口在 accountApi 里（/session-renew）——它管的是账号管理下的**全部账号**，
// 四个平台一视同仁，所以刻意没放进 xhsApi。
import { accountApi } from '@/api/account'

// 账号会话定时续期的前端共享状态（at-ai-xhs 改造新增）。
//
// 为什么状态放在模块级、而不是写在那个按钮组件里：
// 这个按钮要出现在两个地方（「账号管理」和「设置」），而「账号管理」一个页面里就有
// 5 份工具栏（全部 / 快手 / 抖音 / 视频号 / 小红书各一份）。放在组件里的话就是 5 次
// 请求、5 份各自为政的状态——点一下开关只有被点中的那一个按钮变样，
// 切到别的平台标签又变回旧状态，看着像没生效。
// 共享一份之后：一次请求、一处状态，所有按钮同步。

const state = reactive({
  enabled: true,
  times: [],
  nextRunTime: '',
  lastRun: null,
  running: false,
  loading: false,
  saving: false,
  /**
   * 读不到状态时的提示。**只有这一处**用来表达"现在看到的不作数"——
   * enabled 保留默认值（开启，和后端默认一致），不去胡乱改成关闭。
   */
  error: ''
})

// 一次会话只拉一次；两个页面来回切的时候不会再打一遍接口（要刷新传 force）
let loadedOnce = false
let inflight = null

function applyStatus(data) {
  if (!data) return
  state.enabled = !!data.enabled
  state.times = Array.isArray(data.times) ? [...data.times] : []
  state.nextRunTime = data.next_run_time || ''
  state.lastRun = data.last_run || null
  state.running = !!data.running
  state.error = ''
}

async function loadSessionRenew(force = false) {
  if (loadedOnce && !force) return
  if (inflight) return inflight
  state.loading = true
  inflight = accountApi
    .getSessionRenew()
    .then((res) => {
      if (res.code === 200) {
        applyStatus(res.data)
        loadedOnce = true
      }
    })
    .catch(() => {
      // 拦截器已经弹过具体错误了，这里只记下"没读到"。
      // 开关仍按后端的默认值（开启）显示——**不假装读到了**，靠悬浮提示里那句
      // "读取续期状态失败"告诉用户现在看到的不作数。
      state.error = '读取续期状态失败（后端可能还没重启）'
    })
    .finally(() => {
      state.loading = false
      inflight = null
    })
  return inflight
}

/** 开关定时续期。成功后返回新的开关状态（失败返回 null） */
async function setSessionRenewEnabled(enabled) {
  state.saving = true
  try {
    const res = await accountApi.saveSessionRenew({ enabled: !!enabled })
    if (res.code === 200) {
      applyStatus(res.data)
      loadedOnce = true
      ElMessage.success(
        enabled
          ? `已开启定时续期${state.times.length ? `：每天 ${state.times.join('、')}` : ''}`
          : '已关闭定时续期'
      )
      return state.enabled
    }
    return null
  } catch (e) {
    return null
  } finally {
    state.saving = false
  }
}

/** 保存时间点。传进来的是 "HH:MM" 数组 */
async function saveSessionRenewTimes(times) {
  state.saving = true
  try {
    const res = await accountApi.saveSessionRenew({ times })
    if (res.code === 200) {
      applyStatus(res.data)
      loadedOnce = true
      ElMessage.success('续期时间点已保存')
      return true
    }
    return false
  } catch (e) {
    // 后端会挡掉"全是无效时间点"的提交（否则开关还显示开启，却一个点都不跑）
    return false
  } finally {
    state.saving = false
  }
}

// 按钮上悬浮提示的内容：到底什么时候跑、上次跑成什么样
const renewSummary = computed(() => {
  if (state.error) return state.error
  if (!state.enabled) return '已关闭：账号登录态不再定期保活，可能提前掉线'
  if (!state.times.length) return '没有可用的时间点，续期不会执行'
  const parts = [`每天 ${state.times.join('、')} 自动为「账号管理」里的全部账号续期会话`]
  if (state.nextRunTime) parts.push(`下次 ${state.nextRunTime}`)
  if (state.lastRun) {
    const at = (state.lastRun.finished_at || state.lastRun.started_at || '').slice(5, 16)
    parts.push(
      `上次 ${at}：有效 ${state.lastRun.ok}/${state.lastRun.total}` +
        (state.lastRun.invalid ? `，失效 ${state.lastRun.invalid}` : '') +
        (state.lastRun.busy ? `，跳过 ${state.lastRun.busy}` : '') +
        (state.lastRun.error ? `，出错 ${state.lastRun.error}` : '')
    )
  }
  return parts.join('；')
})

// 上次续期结果的一句话描述（设置页里显示用，没有记录时为空）
const lastRunText = computed(() => {
  if (!state.lastRun) return ''
  const run = state.lastRun
  const parts = [`${run.total} 个账号：有效 ${run.ok}`]
  if (run.invalid) parts.push(`失效 ${run.invalid}`)
  if (run.busy) parts.push(`跳过 ${run.busy}`)
  if (run.error) parts.push(`出错 ${run.error}`)
  return `${run.finished_at || run.started_at || ''}（${run.reason || ''}）  ${parts.join('，')}`
})

export function useSessionRenew() {
  return {
    state,
    loadSessionRenew,
    setSessionRenewEnabled,
    saveSessionRenewTimes,
    renewSummary,
    lastRunText
  }
}
