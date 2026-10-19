<template>
  <div class="settings-page">
    <div class="page-header">
      <h1>设置</h1>
      <span class="hint">视觉模型、发布默认值、账号续期都在这里配置</span>
    </div>

    <el-card shadow="never" class="mb16">
      <template #header>
        <div class="card-header">
          <span>视觉理解模型（OpenAI 兼容格式）</span>
          <div>
            <el-button :loading="testing" @click="doTest">测试连接</el-button>
            <el-button type="primary" :loading="saving" @click="doSave">保存</el-button>
          </div>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="填三个要素即可：服务地址、API Key、模型名"
        description="只要服务实现了 OpenAI 的 /chat/completions 接口就能用——官方 OpenAI、各类中转、通义/豆包等兼容层、vLLM、Ollama 都行。模型必须是多模态（能读图）的。"
      />

      <el-form label-width="130px" class="mt16 settings-form">
        <!-- 连接三要素：并排两列，免得三个长输入框把页面拉得老长 -->
        <div class="form-grid">
          <el-form-item label="服务地址" class="is-important">
            <el-input v-model="form.base_url" placeholder="https://api.openai.com/v1" />
            <div class="hint">填到 /v1 这一层即可，程序会自动拼 /chat/completions</div>
          </el-form-item>

          <el-form-item label="API Key" class="is-important">
            <el-input
              v-model="form.api_key"
              type="password"
              show-password
              :placeholder="form.has_key ? `已保存（${form.api_key_masked}），留空表示不修改` : 'sk-...'"
            />
            <div class="hint">明文保存在本机 settings.json（权限 600），接口只回传掩码</div>
          </el-form-item>

          <el-form-item label="模型名" class="is-important">
            <el-input v-model="form.model" placeholder="gpt-4o-mini / qwen-vl-max / doubao-1.5-vision-pro" />
            <div class="hint">必须是能读图的多模态模型</div>
          </el-form-item>

          <el-form-item label="超时（秒）">
            <el-input-number v-model="form.timeout" :min="10" :max="600" :step="10" class="w-full" />
            <div class="hint">生成一次文案最多等多久</div>
          </el-form-item>
        </div>

        <el-divider content-position="left">生成参数</el-divider>

        <div class="form-grid">
          <el-form-item label="最大输出长度" class="is-important">
            <el-input-number v-model="form.max_tokens" :min="500" :max="32000" :step="500" class="w-full" />
            <div class="hint">
              推理模型的思考也占这个额度：太小会截断正文。建议 8000 起步。
            </div>
          </el-form-item>

          <el-form-item label="正文字数上限" class="is-important">
            <el-input-number v-model="form.desc_max_chars" :min="30" :max="1000" :step="10" class="w-full" />
            <div class="hint">
              生成正文的字数上限（小红书正文可到 1000 字，这里是主动收窄）
            </div>
          </el-form-item>
        </div>

        <el-form-item label="额外要求">
          <el-input
            v-model="form.extra_prompt"
            type="textarea"
            :rows="3"
            placeholder="可留空。例如：语气活泼一点，面向 25-35 岁女性，突出性价比"
          />
          <div class="hint">会追加到生成文案的提示词里</div>
        </el-form-item>
      </el-form>
    </el-card>

    <!--
      账号会话续期：功能本体在后端（myUtils/session_renew.py），这里只是把开关和
      时间点也放到设置里。开关组件与「账号管理」页那个按钮是同一份状态，
      所以两边点哪个都一样。
    -->
    <el-card shadow="never" class="mb16">
      <template #header><span>账号会话续期</span></template>
      <SessionRenewToggle show-times />
    </el-card>

    <el-card shadow="never">
      <template #header><span>发布默认值</span></template>
      <el-form label-width="130px" class="settings-form">
        <div class="form-grid">
          <el-form-item label="默认合集" class="is-important">
            <el-input v-model="defaultCollection" placeholder="例如 风景" />
            <div class="hint">发布页会自动选中的合集名；留空则不自动选。找不到同名合集会保持不选。</div>
          </el-form-item>
          <el-form-item label="视频号视频标注" class="is-important">
            <el-select v-model="tencentContentLabel" placeholder="不标注（默认）" class="w-full">
              <el-option label="不标注（默认，实拍视频用这个）" value="" />
              <el-option label="含AI生成内容" value="含AI生成内容" />
            </el-select>
            <div class="hint">
              实拍视频用默认即可；成片含 AI 生成内容（TTS 配音 / AI 画面）时再选。
            </div>
          </el-form-item>
          <el-form-item label="抖音自主声明" class="is-important">
            <el-select v-model="douyinDeclaration" placeholder="不声明（默认）" class="w-full">
              <el-option label="不声明（默认，实拍视频用这个）" value="" />
              <el-option label="内容由AI生成" value="内容由AI生成" />
            </el-select>
            <div class="hint">
              默认不声明；成片确实含 AI 生成内容（TTS 配音 / AI 字幕 / AI 画面）时才需要选。
            </div>
          </el-form-item>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { xhsApi } from '@/api/xhs'
// 「定时续期」开关 + 时间点编辑（后端调度见 myUtils/session_renew.py）。
// 这是页面里**唯一**一个自带保存的组件：点开关立刻生效、改时间点要按它自己的「保存时间点」。
// 所以 payload() 里刻意不带 session_renew——主「保存」按钮只负责模型和发布默认值，
// 免得两个按钮抢着写同一个字段。
import SessionRenewToggle from '@/components/SessionRenewToggle.vue'

const form = reactive({
  base_url: '',
  api_key: '',
  api_key_masked: '',
  has_key: false,
  model: '',
  timeout: 120,
  max_tokens: 8000,
  desc_max_chars: 100,
  extra_prompt: ''
})
const defaultCollection = ref('')
const tencentContentLabel = ref('')
const douyinDeclaration = ref('')
const saving = ref(false)
const testing = ref(false)

async function load() {
  try {
    const res = await xhsApi.getSettings()
    if (res.code === 200) {
      const v = res.data?.vision || {}
      form.base_url = v.base_url || ''
      form.model = v.model || ''
      form.timeout = v.timeout || 120
      form.max_tokens = v.max_tokens || 8000
      form.desc_max_chars = v.desc_max_chars || 100
      form.extra_prompt = v.extra_prompt || ''
      form.api_key_masked = v.api_key_masked || ''
      form.has_key = !!v.has_key
      form.api_key = ''
      defaultCollection.value = res.data?.default_collection || ''
      tencentContentLabel.value = res.data?.tencent_content_label || ''
      douyinDeclaration.value = res.data?.douyin_declaration || ''
    }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

function payload() {
  // 注意：**不带 session_renew**。续期开关/时间点由上面的 SessionRenewToggle 自己保存
  // （它在两页上都出现，点了要立刻生效），这里再带一份就会互相覆盖。
  const p = {
    default_collection: defaultCollection.value,
    tencent_content_label: tencentContentLabel.value,
    douyin_declaration: douyinDeclaration.value,
    vision: {
      base_url: form.base_url,
      model: form.model,
      timeout: form.timeout,
      max_tokens: form.max_tokens,
      desc_max_chars: form.desc_max_chars,
      extra_prompt: form.extra_prompt
    }
  }
  // 留空表示不修改已保存的 key
  if (form.api_key) p.vision.api_key = form.api_key
  return p
}

async function doSave() {
  saving.value = true
  try {
    const res = await xhsApi.saveSettings(payload())
    if (res.code === 200) {
      ElMessage.success('已保存')
      form.api_key = ''
      await load()
    }
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

async function doTest() {
  testing.value = true
  try {
    const res = await xhsApi.testSettings(payload())
    if (res.code === 200) {
      ElMessage.success(`连接成功：${res.data?.reply || ''}`)
    }
  } catch (e) {
    /* 拦截器已提示具体原因 */
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<style scoped>
/* 不设 max-width：和素材管理/账号管理等页一样占满主区域，
   右侧不会留一大截空白（原来 1000px 限制在宽屏上右边的空白很扎眼） */
.settings-page {
  min-width: 0;
}
.page-header {
  margin-bottom: 20px;
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.page-header h1 {
  font-size: 22px;
  color: var(--el-text-color-primary);
  margin: 0;
}
.hint {
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
}
.page-header .hint {
  font-size: 13px;
}
.mb16 {
  margin-bottom: 16px;
}
.mt16 {
  margin-top: 16px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

/* 表单主体：两列网格。窄屏自动退回单列（媒体查询在 scoped 里正常生效） */
.settings-form .form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 24px;
}
@media (max-width: 900px) {
  .settings-form .form-grid {
    grid-template-columns: 1fr;
  }
}
.settings-form :deep(.el-form-item__content) {
  flex-direction: column;
  align-items: stretch;
}
.w-full {
  width: 100%;
}
/* 重要参数：标签加粗加黑，扫一眼就能看到关键项 */
.settings-form :deep(.is-important > .el-form-item__label) {
  font-weight: 700;
  color: var(--el-text-color-primary);
}
.settings-form :deep(.el-divider--horizontal) {
  margin: 20px 0 4px;
}
.settings-form :deep(.el-divider__text) {
  font-size: 13px;
  color: #909399;
  font-weight: 600;
}
</style>
