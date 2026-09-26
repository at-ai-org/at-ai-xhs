<template>
  <div id="app">
    <el-container>
      <el-aside :width="isCollapse ? '64px' : '200px'">
        <div class="sidebar">
          <div class="logo">
            <img v-show="isCollapse" src="/vite.svg" alt="Logo" class="logo-img">
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
            <el-menu-item index="/model-settings">
              <el-icon><Setting /></el-icon>
              <span>模型设置</span>
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
              <!-- 账号信息已移除 -->
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
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  HomeFilled, User, DataAnalysis,
  Fold, Picture, Upload, Promotion, Setting, Tickets
} from '@element-plus/icons-vue'
import { publishApi } from '@/api/publish'

const route = useRoute()

// 当前激活的菜单项
const activeMenu = computed(() => {
  return route.path
})

// 侧边栏折叠状态
const isCollapse = ref(false)

// 切换侧边栏折叠状态
const toggleSidebar = () => {
  isCollapse.value = !isCollapse.value
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

onMounted(() => {
  pollPrompts()
  promptTimer = setInterval(pollPrompts, 2000)
})

onBeforeUnmount(() => {
  if (promptTimer) {
    clearInterval(promptTimer)
    promptTimer = null
  }
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
        
        .username {
          margin: 0 8px;
          color: $text-regular;
        }
        
        .el-icon {
          font-size: 12px;
          color: $text-secondary;
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
