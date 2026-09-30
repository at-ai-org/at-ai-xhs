<template>
  <div class="settings-page">
    <div class="page-header">
      <h1>设置</h1>
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

      <el-form label-width="130px" class="mt16">
        <el-form-item label="服务地址">
          <el-input
            v-model="form.base_url"
            placeholder="https://api.openai.com/v1"
            style="max-width: 520px"
          />
          <div class="hint">填到 /v1 这一层即可，程序会自动拼 /chat/completions</div>
        </el-form-item>

        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="form.has_key ? `已保存（${form.api_key_masked}），留空表示不修改` : 'sk-...'"
            style="max-width: 520px"
          />
          <div class="hint">明文保存在本机 settings.json（权限 600），接口只回传掩码</div>
        </el-form-item>

        <el-form-item label="模型名">
          <el-input v-model="form.model" placeholder="gpt-4o-mini / qwen-vl-max / doubao-1.5-vision-pro" style="max-width: 520px" />
        </el-form-item>

        <el-form-item label="超时（秒）">
          <el-input-number v-model="form.timeout" :min="10" :max="600" :step="10" />
        </el-form-item>

        <el-form-item label="最大输出长度">
          <el-input-number v-model="form.max_tokens" :min="500" :max="32000" :step="500" />
          <div class="hint">
            推理模型（会输出思考过程的）思考也占这个额度：设太小会导致正文被截断、话题标签丢失。
            建议 8000 起步，出现"话题为空"就往上调。
          </div>
        </el-form-item>

        <el-form-item label="正文字数上限">
          <el-input-number v-model="form.desc_max_chars" :min="30" :max="1000" :step="10" />
          <div class="hint">
            生成正文的字数上限（小红书正文本身可到 1000 字，这里是主动收窄）。默认 100 字。
          </div>
        </el-form-item>

        <el-form-item label="额外要求">
          <el-input
            v-model="form.extra_prompt"
            type="textarea"
            :rows="3"
            placeholder="可留空。例如：语气活泼一点，面向 25-35 岁女性，突出性价比"
            style="max-width: 640px"
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
      <el-form label-width="130px">
        <el-form-item label="默认合集">
          <el-input v-model="defaultCollection" placeholder="例如 风景" style="max-width: 320px" />
          <div class="hint">发布页会自动选中的合集名；留空则不自动选。找不到同名合集会保持不选。</div>
        </el-form-item>
        <el-form-item label="视频号视频标注">
          <el-select v-model="tencentContentLabel" placeholder="不标注（默认）" style="max-width: 320px">
            <el-option label="不标注（默认，实拍视频用这个）" value="" />
            <el-option label="含AI生成内容" value="含AI生成内容" />
          </el-select>
          <div class="hint">
            视频号发布页的「视频标注」。默认不设置——实拍视频没有 AI 生成内容，平台也不强制，
            硬去点这一项只会白等一次超时。成片确实含 AI 生成内容（TTS 配音 / AI 画面）时再选上。
          </div>
        </el-form-item>
        <el-form-item label="抖音自主声明">
          <el-select v-model="douyinDeclaration" placeholder="不声明（默认）" style="max-width: 320px">
            <el-option label="不声明（默认，实拍视频用这个）" value="" />
            <el-option label="内容由AI生成" value="内容由AI生成" />
          </el-select>
          <div class="hint">
            抖音发布页的「自主声明」。默认什么都不声明——实拍视频没有 AI 生成内容，平台也不强制。
            成片确实含 AI 生成内容（TTS 配音 / AI 字幕 / AI 画面）时才需要选上。
          </div>
        </el-form-item>
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
.settings-page {
  max-width: 900px;
}
.page-header {
  margin-bottom: 20px;
}
.page-header h1 {
  font-size: 24px;
  color: var(--el-text-color-primary);
  margin: 0;
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
.hint {
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
}
</style>
