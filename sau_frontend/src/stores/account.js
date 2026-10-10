import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useAccountStore = defineStore('account', () => {
  // 存储所有账号信息
  const accounts = ref([])
  
  // 平台类型映射
  const platformTypes = {
    1: '小红书',
    2: '视频号',
    3: '抖音',
    4: '快手',
    5: '拼多多视频'
  }
  
  // user_info.status 那一列 → 界面上的三个状态。
  // -1 是**界面自己**用的临时值（「验证中」），后端不会返回它；decode 收在这里，
  // 是为了别让 `=== 1 ? '正常' : '异常'` 这条约定散落到各个页面。
  const decodeStatus = (rawStatus) =>
    rawStatus === -1 ? '验证中' : (rawStatus === 1 ? '正常' : '异常')

  // 设置账号列表
  const setAccounts = (accountsData) => {
    // 转换后端返回的数据格式为前端使用的格式
    accounts.value = accountsData.map(item => {
      return {
        id: item[0],
        type: item[1],
        filePath: item[2],
        name: item[3],
        status: decodeStatus(item[4]),
        platform: platformTypes[item[1]] || '未知'
      }
    })
  }

  // 把列表里所有账号标成「验证中」。
  //
  // **只能在"真的开始校验了"的时候调**（用户点了刷新按钮）。进页面那一步只是读一次
  // 数据库（毫秒级），并没有在跑校验——标成验证中，用户会以为后台已经在查了，
  // 然后盯着一个这一轮根本不会结束的状态等；账号校验要开浏览器，是分钟级的。
  const markAllVerifying = () => {
    accounts.value = accounts.value.map(account => ({ ...account, status: '验证中' }))
  }

  // 用后端刚校验出来的原始状态（user_info.status）更新一行的状态。
  //
  // 传原始值而不是 '正常'/'异常' 字符串，是为了让"没校验成"的账号能自然退回原状态：
  // 被占用 / 出错时后端不会写库，返回的还是原值（见 sau_backend.py::getValidAccounts）。
  const setAccountStatus = (id, rawStatus) => {
    const index = accounts.value.findIndex(acc => acc.id === id)
    if (index === -1) return
    accounts.value[index] = { ...accounts.value[index], status: decodeStatus(rawStatus) }
  }
  
  // 添加账号
  const addAccount = (account) => {
    accounts.value.push(account)
  }
  
  // 更新账号
  const updateAccount = (id, updatedAccount) => {
    const index = accounts.value.findIndex(acc => acc.id === id)
    if (index !== -1) {
      accounts.value[index] = { ...accounts.value[index], ...updatedAccount }
    }
  }
  
  // 删除账号
  const deleteAccount = (id) => {
    accounts.value = accounts.value.filter(acc => acc.id !== id)
  }
  
  // 根据平台获取账号
  const getAccountsByPlatform = (platform) => {
    return accounts.value.filter(acc => acc.platform === platform)
  }
  
  return {
    accounts,
    setAccounts,
    markAllVerifying,
    setAccountStatus,
    addAccount,
    updateAccount,
    deleteAccount,
    getAccountsByPlatform
  }
})