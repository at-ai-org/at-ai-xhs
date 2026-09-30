<template>
  <div id="app">
    <!--
      登录页要占满整屏（它自带背景），不能套侧边栏 / 顶栏那层布局。
      是否整屏由路由 meta.blank 决定，见 router/index.js。
    -->
    <router-view v-if="isBlankLayout" />

    <el-container v-else>
      <el-aside :width="isCollapse ? '64px' : '200px'">
        <div class="sidebar">
          <div class="logo">
            <img v-show="isCollapse" src="/logo.svg" alt="Logo" class="logo-img">
            <h2 v-show="!isCollapse">自媒体自动化运营系统</h2>
          </div>
          <el-menu
            :router="true"
            :default-active="activeMenu"
            :collapse="isCollapse"
            class="sidebar-menu"
            background-color="#001529"
            text-color="#fff"
            active-text-color="#409EFF"
          >
            <el-menu-item index="/">
              <el-icon><HomeFilled /></el-icon>
              <span>首页</span>
            </el-menu-item>
            <el-menu-item index="/account-management">
              <el-icon><User /></el-icon>
              <span>账号管理</span>
            </el-menu-item>
            <el-menu-item index="/material-management">
              <el-icon><Picture /></el-icon>
              <span>素材管理</span>
            </el-menu-item>
            <el-menu-item index="/background-music">
              <el-icon><Headset /></el-icon>
              <span>背景音乐</span>
            </el-menu-item>
            <el-menu-item index="/publish-center">
              <el-icon><Upload /></el-icon>
              <span>发布中心</span>
            </el-menu-item>
            <el-menu-item index="/publish-records">
              <el-icon><Tickets /></el-icon>
              <span>发布记录</span>
            </el-menu-item>
            <el-menu-item index="/xhs-workbench">
              <el-icon><Promotion /></el-icon>
              <span>小红书工作台</span>
            </el-menu-item>
            <el-menu-item index="/settings">
              <el-icon><Setting /></el-icon>
              <span>设置</span>
            </el-menu-item>
            <el-menu-item index="/about">
              <el-icon><DataAnalysis /></el-icon>
              <span>关于</span>
            </el-menu-item>
          </el-menu>
        </div>
      </el-aside>
      <el-container>
        <el-header>
          <div class="header-content">
            <div class="header-left">
              <el-icon class="toggle-sidebar" @click="toggleSidebar"><Fold /></el-icon>
            </div>
            <div class="header-right">
              <!-- 当前登录用户 + 退出（at-ai-xhs 改造新增） -->
              <el-dropdown @command="onUserCommand">
                <span class="user-dropdown">
                  <el-icon><UserFilled /></el-icon>
                  <span class="username">{{ userStore.username || 'admin' }}</span>
                  <el-icon><ArrowDown /></el-icon>
                </span>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="logout">退出登录</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </div>
        </el-header>
        <el-main>
          <router-view />
        </el-main>
      </el-container>
    </el-container>

    <!--
      发布途中「要人搭把手」的弹窗：抖音发布时平台常弹短信验证码，机器过不去，
      必须有人看一眼手机。后端那边的发布线程会原地等人，前端轮询到就弹这个窗。
      放在布局层而不是发布中心里：验证码等的是人，跟用户此刻停在哪个页面无关。
    -->
    <el-dialog
      v-if="!isBlankLayout"
      v-model="promptVisible"
      :title="activePrompt?.title || '需要你输入'"
      width="460px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
      append-to-body
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        :title="activePrompt?.platform_name ? `${activePrompt.platform_name} · ${activePrompt.account_name} 正在等你` : '发布任务正在等你'"
        :description="activePrompt?.material_title ? `素材：${activePrompt.material_title}` : ''"
      />
      <p class="prompt-message">{{ activePrompt?.message }}</p>
      <el-input
        ref="promptInputRef"
        v-model="promptValue"
        size="large"
        clearable
        :placeholder="activePrompt?.placeholder || '请输入'"
        @keyup.enter="submitPrompt"
      />
      <div class="prompt-hint">
        已经等了 {{ activePrompt?.age ?? 0 }} 秒；超过 {{ promptTimeoutText }} 没人填就自动跳过。
      </div>
      <template #footer>
        <el-button @click="skipPrompt">跳过这次</el-button>
        <el-button type="primary" :loading="promptSubmitting" @click="submitPrompt">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  HomeFilled, User, DataAnalysis, UserFilled, ArrowDown,
  Fold, Picture, Upload, Promotion, Setting, Tickets, Headset
} from '@element-plus/icons-vue'
import { publishApi } from '@/api/publish'
import { authApi } from '@/api/auth'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

// 当前激活的菜单项。
// 用 matched 里的记录路径而不是 route.path：设置页保留了老地址 `/model-settings` 作为别名
// （收藏 / 老链接还在用），走别名进来时 route.path 是 `/model-settings`，
// 直接比就会高亮不到「设置」这一项。
const activeMenu = computed(() => {
  const last = route.matched[route.matched.length - 1]
  return last?.path || route.path
})

// 登录页（meta.blank）要整屏显示，不套侧边栏／顶栏，也不轮询发布弹窗
const isBlankLayout = computed(() => !!route.meta.blank)

// 侧边栏折叠状态
const isCollapse = ref(false)

// 切换侧边栏折叠状态
const toggleSidebar = () => {
  isCollapse.value = !isCollapse.value
}

// ---- 登录态 ----------------------------------------------------------------
// 退出登录：清本地 token 回登录页就够了——后端签发的 token 是无状态的，
// 没有服务端会话要销毁（要作废所有已发的 token 就换 settings.json 里的 auth.secret_key）。
function onUserCommand(command) {
  if (command !== 'logout') return
  userStore.logout()
  ElMessage.success('已退出登录')
  router.replace('/login')
}

/**
 * 启动时验一次本地 token 是否还有效。
 *
 * 路由守卫读的是 localStorage，只能判断"有没有 token"，判断不了"还有没有效"
 * （比如后端换了 auth.secret_key，或者过了 7 天）。这里问一次后端，
 * 无效就清掉，免得用户进来先看到一屏报错。
 */
async function verifySession() {
  if (!userStore.isLoggedIn) return
  try {
    const res = await authApi.status()
    if (!res.data?.authenticated) {
      userStore.logout()
      router.replace('/login')
    }
  } catch (e) {
    // 后端没起来 / 网络不通：不动登录态，让用户自己去点一下看报什么错
  }
}

// ---- 发布途中的"等人输入"弹窗（抖音短信验证码等） ----------------------------
// 后端发布线程会原地等人，这里轮询 /publish/prompts，有请求就弹窗让人填。
const activePrompt = ref(null)
const promptValue = ref('')
const promptSubmitting = ref(false)
const promptInputRef = ref(null)
let promptTimer = null

const promptVisible = computed({
  get: () => !!activePrompt.value,
  set: (v) => {
    // 弹窗不允许手动关闭：要跳过必须点「跳过这次」，否则会分不清用户到底看没看到
    if (!v) activePrompt.value = null
  }
})

const promptTimeoutText = computed(() => {
  const seconds = Number(activePrompt.value?.timeout || 0)
  if (!seconds) return '几分钟'
  return seconds >= 60 ? `${Math.round(seconds / 60)} 分钟` : `${seconds} 秒`
})

async function pollPrompts() {
  // 提交中不打断（等这次请求返回，避免和「提交/跳过」抢同一个 prompt）
  if (promptSubmitting.value) return
  try {
    const res = await publishApi.listPrompts()
    if (res.code !== 200) return
    const list = res.data || []

    const current = activePrompt.value
    if (current) {
      const still = list.find((p) => p.prompt_id === current.prompt_id)
      if (still) {
        // 同一次请求：刷新"已经等了多久"这类现场信息，不重置用户正在输入的内容
        activePrompt.value = still
        return
      }
      // 请求没了：多半是后端等超时了、或者任务已经结束
      activePrompt.value = null
      ElMessage.warning('这次输入已经不用填了（等待超时或任务已结束）')
    }

    const next = list[0]
    if (!next) return
    activePrompt.value = next
    promptValue.value = ''
    await nextTick()
    promptInputRef.value?.focus()
  } catch (e) {
    /* 后端没起来 / 网络抖动都直接忽略，下一轮再试 */
  }
}

async function submitPrompt() {
  const prompt = activePrompt.value
  if (!prompt || promptSubmitting.value) return
  const value = promptValue.value.trim()
  if (!value) {
    ElMessage.warning('请先填写内容')
    return
  }
  promptSubmitting.value = true
  try {
    const res = await publishApi.submitPrompt(prompt.prompt_id, { value })
    if (res.code === 200) {
      ElMessage.success('已提交，发布流程继续')
      activePrompt.value = null
    }
  } catch (e) {
    /* 拦截器已提示（多半是"已经不需要了"，下一轮轮询会给出最新状态） */
    activePrompt.value = null
  } finally {
    promptSubmitting.value = false
  }
}

async function skipPrompt() {
  const prompt = activePrompt.value
  if (!prompt) return
  promptSubmitting.value = true
  try {
    await publishApi.submitPrompt(prompt.prompt_id, { skip: true })
    ElMessage.info('已跳过，发布流程继续')
  } catch (e) {
    /* 同上 */
  } finally {
    promptSubmitting.value = false
    activePrompt.value = null
  }
}

// 只在"已登录且不在登录页"时轮询。放在登录页上跑的话，每 2 秒一次 401，
// 会不停地弹"登录已过期"，把人从登录页反复踢来踢去。
function startPromptPolling() {
  if (promptTimer) return
  pollPrompts()
  promptTimer = setInterval(pollPrompts, 2000)
}

function stopPromptPolling() {
  if (promptTimer) {
    clearInterval(promptTimer)
    promptTimer = null
  }
  activePrompt.value = null
}

watch(
  () => [isBlankLayout.value, userStore.isLoggedIn],
  ([blank, authed]) => {
    if (!blank && authed) startPromptPolling()
    else stopPromptPolling()
  },
  { immediate: true }
)

onMounted(() => {
  verifySession()
})

onBeforeUnmount(() => {
  stopPromptPolling()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

#app {
  min-height: 100vh;
}

.el-container {
  height: 100vh;
}

.el-aside {
  background-color: #001529;
  color: #fff;
  height: 100vh;
  overflow: hidden;
  transition: width 0.3s;
  
  .sidebar {
    display: flex;
    flex-direction: column;
    height: 100%;
    
    .logo {
      height: 60px;
      padding: 0 16px;
      display: flex;
      align-items: center;
      background-color: #002140;
      overflow: hidden;
      
      .logo-img {
        width: 32px;
        height: 32px;
        margin-right: 12px;
      }
      
      h2 {
        color: #fff;
        font-size: 16px;
        font-weight: 600;
        white-space: nowrap;
        margin: 0;
      }
    }
    
    .sidebar-menu {
      border-right: none;
      flex: 1;
      
      .el-menu-item {
        display: flex;
        align-items: center;
        
        .el-icon {
          margin-right: 10px;
          font-size: 18px;
        }
      }
    }
  }
}

.el-header {
  background-color: #fff;
  box-shadow: 0 1px 4px rgba(0, 21, 41, 0.08);
  padding: 0;
  height: 60px;
  
  .header-content {
    display: flex;
    justify-content: space-between;
    align-items: center;
    height: 100%;
    padding: 0 16px;
    
    .header-left {
      .toggle-sidebar {
        font-size: 20px;
        cursor: pointer;
        color: $text-regular;
        
        &:hover {
          color: $primary-color;
        }
      }
    }
    
    .header-right {
      .user-dropdown {
        display: flex;
        align-items: center;
        cursor: pointer;
        outline: none;

        .el-icon {
          font-size: 14px;
          color: $text-secondary;
        }

        .username {
          margin: 0 8px;
          font-size: $font-size-base;
          color: $text-regular;
        }
      }
    }
  }
}

.el-main {
  background-color: $bg-color-page;
  padding: 20px;
  overflow-y: auto;
}

.prompt-message {
  margin: 12px 0;
  font-size: 13px;
  line-height: 1.7;
  color: $text-regular;
}

.prompt-hint {
  margin-top: 8px;
  font-size: 12px;
  color: $text-secondary;
}
</style>
