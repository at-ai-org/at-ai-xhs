<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <img src="/logo.svg" alt="Logo" class="brand-logo">
        <h1 class="brand-title">自媒体自动化运营系统</h1>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        size="large"
        @submit.prevent="doLogin"
      >
        <el-form-item label="账号" prop="username">
          <el-input
            v-model="form.username"
            placeholder="admin"
            autocomplete="username"
            :prefix-icon="User"
            @keyup.enter="doLogin"
          />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            show-password
            autocomplete="current-password"
            :prefix-icon="Lock"
            @keyup.enter="doLogin"
          />
        </el-form-item>

        <el-button
          type="primary"
          class="login-button"
          size="large"
          :loading="loading"
          @click="doLogin"
        >
          {{ loading ? '登录中…' : '登 录' }}
        </el-button>
      </el-form>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { authApi } from '@/api/auth'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const formRef = ref(null)
const loading = ref(false)

const form = reactive({
  // 账号固定是 admin，预先填好省一次输入（仍可改，万一以后支持多账号）
  username: 'admin',
  password: ''
})

const rules = {
  username: [{ required: true, message: '请输入账号', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

async function doLogin() {
  if (loading.value) return

  // 先过表单校验；校验失败时 element-plus 会自己标红，这里不用再提示
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    const res = await authApi.login(form.username.trim(), form.password)
    const data = res.data || {}
    userStore.setSession(data.token, data.username || form.username.trim())
    ElMessage.success('登录成功')

    // 回到被拦下来的那个页面；没有就回首页。
    // `redirect` 来自 URL 查询串，等于用户可控的输入，所以只认"站内绝对路径"：
    // 必须 / 开头，且不能是 //（协议相对地址，会被浏览器当成外域）。
    const raw = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    const redirect = raw.startsWith('/') && !raw.startsWith('//') ? raw : '/'
    router.replace(redirect)
  } catch (e) {
    // 具体原因（"账号或密码不正确"、"失败次数过多"）由 request.js 的拦截器统一弹出，
    // 这里只负责把密码清掉，避免用户对着一个错误密码反复点。
    form.password = ''
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  // 从别处被踢回来时，可能还留着上一次的输入
  form.password = ''
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: $spacing-lg;
  background: linear-gradient(135deg, #001529 0%, #003a70 55%, #0b5ea8 100%);
}

.login-card {
  width: 100%;
  max-width: 420px;
  background: $bg-color;
  border-radius: 10px;
  padding: 36px 32px 28px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.28);
}

.brand {
  text-align: center;
  margin-bottom: $spacing-xl;

  .brand-logo {
    width: 56px;
    height: 56px;
    // 全局 reset 把 img 设成了 display:block（见 styles/reset.scss），
    // 所以父级的 text-align:center 对它无效，得自己 margin:auto 居中。
    margin: 0 auto;
  }

  .brand-title {
    margin: $spacing-md 0 0;
    font-size: 20px;
    font-weight: 600;
    color: $text-primary;
  }
}

.login-button {
  width: 100%;
  margin-top: $spacing-sm;
  letter-spacing: 2px;
}
</style>
