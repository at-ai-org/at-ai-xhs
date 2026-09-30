<template>
  <div class="xhs-workbench">
    <!-- 账号 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <div class="card-header">
          <span>① 账号与登录</span>
          <el-button link type="primary" @click="loadAccounts">刷新</el-button>
        </div>
      </template>
      <el-form :inline="true">
        <el-form-item label="账号标识">
          <el-input v-model="account" placeholder="例如 me" style="width: 180px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loggingIn" @click="startLogin">扫码登录</el-button>
          <el-button :disabled="!account" @click="checkAccount">校验登录态</el-button>
        </el-form-item>
      </el-form>

      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="同一个小红书账号只允许一个网页端会话"
        description="工具登录后请不要再在别的浏览器登录小红书网页版，否则会把工具踢下线（手机 App 不受影响）。"
      />

      <el-table v-if="accounts.length" :data="accounts" size="small" class="mt12">
        <el-table-column prop="account" label="账号" width="140" />
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.valid === true" type="success" size="small">有效</el-tag>
            <el-tag v-else-if="row.valid === false" type="danger" size="small">已失效</el-tag>
            <el-tag v-else type="info" size="small">未校验</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="file" label="Cookie 文件" show-overflow-tooltip />
      </el-table>
    </el-card>

    <!-- 扫码 -->
    <el-card v-if="qrDataUrl || loginStatus" shadow="never" class="mb16">
      <template #header><span>扫码登录</span></template>
      <div class="qr-area">
        <img v-if="qrDataUrl" :src="qrDataUrl" alt="小红书登录二维码" class="qr-img" />
        <div class="qr-side">
          <el-alert :type="loginStatusType" :closable="false" show-icon :title="loginStatus" />
          <p class="hint">请用小红书 App 扫一扫。登录成功后本页会自动刷新账号状态。</p>
          <el-button v-if="!loggingIn && qrDataUrl" @click="startLogin">重新获取二维码</el-button>
        </div>
      </div>
    </el-card>

    <!-- 自检 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <div class="card-header">
          <span>② 页面契约自检（不提交任何内容）</span>
          <div>
            <el-select v-model="selftestVideo" placeholder="选测试视频" clearable style="width: 220px; margin-right: 8px">
              <el-option v-for="m in materials" :key="m.name" :label="m.name" :value="m.name" />
            </el-select>
            <el-button :loading="testing" @click="runSelftest(false)">浅自检</el-button>
            <el-button type="primary" :loading="testing" :disabled="!selftestVideo" @click="runSelftest(true)">
              深自检（先上传）
            </el-button>
          </div>
        </div>
      </template>
      <p class="hint">
        逐条检查发布页的控件是否还在。页面改版后跑一次就知道坏在哪条契约上，
        改 <code>uploader/xiaohongshu_uploader/selectors.py</code> 里对应的 candidates 即可。
      </p>
      <el-alert
        class="mb8"
        type="info"
        :closable="false"
        show-icon
        title="浅自检只能验证上传区：标题/正文/话题/定时/合集/发布按钮这些控件，要上传视频之后页面才会渲染出来"
        description="想确认合集控件长什么样，请选一个测试视频做「深自检」——它会真的上传（但不会发布），把编辑器撑出来后再探测。"
      />
      <template v-if="selftest">
        <el-alert
          class="mb8"
          :type="selftest.ok ? 'success' : 'error'"
          :closable="false"
          show-icon
          :title="`命中 ${selftest.hit}/${selftest.total}${selftest.ok ? '，必填契约全部存在' : '，有必填契约缺失'}`"
        />
        <el-table :data="selftest.items" size="small" max-height="360">
          <el-table-column prop="name" label="契约" width="200" />
          <el-table-column prop="page" label="页面" width="90" />
          <el-table-column label="必填" width="70">
            <template #default="{ row }">{{ row.required ? '是' : '否' }}</template>
          </el-table-column>
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.found" type="success" size="small">命中</el-tag>
              <el-tag v-else-if="row.required" type="danger" size="small">缺失</el-tag>
              <el-tag v-else type="info" size="small">无(可选)</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="note" label="说明" show-overflow-tooltip />
        </el-table>
      </template>
    </el-card>

    <!-- 合集 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <div class="card-header">
          <span>③ 合集</span>
          <div>
            <el-select v-model="collectionProbeVideo" placeholder="选测试视频以刷新" clearable style="width: 210px; margin-right: 8px">
              <el-option v-for="m in materials" :key="m.name" :label="m.name" :value="m.name" />
            </el-select>
            <el-button :loading="loadingCollections" @click="loadCollections()">读缓存</el-button>
            <el-button
              type="primary"
              :loading="loadingCollections"
              :disabled="!collectionProbeVideo"
              @click="loadCollections(collectionProbeVideo)"
            >
              刷新合集
            </el-button>
          </div>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="创作平台没有独立的合集管理页，合集控件只在「发布页 + 已上传视频」时才渲染"
        description="所以刷新一次需要上传测试视频并等转码（不会发布），是分钟级操作。读到之后会缓存下来，平时点「读缓存」秒回。"
      />
      <el-alert v-if="collectionMessage" class="mt8" :title="collectionMessage" type="success" :closable="false" show-icon />
      <div v-if="collections.length" class="mt8">
        <el-tag v-for="c in collections" :key="c" class="mr8 mb8" effect="plain">{{ c }}</el-tag>
      </div>
    </el-card>

    <!-- 发布 -->
    <el-card shadow="never">
      <template #header><span>④ 发布视频笔记</span></template>
      <el-form label-width="96px">
        <el-form-item label="视频素材">
          <el-upload
            :show-file-list="false"
            :http-request="doUpload"
            accept="video/*"
            :disabled="uploading"
          >
            <el-button :loading="uploading">上传本地视频</el-button>
            <span v-if="uploading && uploadPercent" class="hint ml8">上传中 {{ uploadPercent }}%</span>
          </el-upload>
          <el-select v-model="form.video" placeholder="或从素材库选择" style="width: 320px; margin-left: 8px" clearable filterable>
            <el-option
              v-for="m in materials"
              :key="m.name"
              :label="`${m.name} · ${m.size_mb}MB · ${fmtTime(m.mtime)}`"
              :value="m.name"
            />
          </el-select>
          <div v-if="form.video" class="hint">将使用: {{ form.video }}</div>
        </el-form-item>

        <el-form-item label="AI 生成">
          <el-switch v-model="form.ai_generate" />
          <span class="hint ml8">
            默认开启。提交后自动解析视频首尾画面生成标题与正文，生成完进入「待确认」
          </span>
        </el-form-item>

        <el-form-item v-if="form.ai_generate" label="补充信息">
          <el-input
            v-model="form.hints"
            placeholder="可选，例如：突出性价比 / 面向学生党 / 强调氛围感"
            style="width: 420px"
          />
          <span class="hint ml8">会一并交给模型参考</span>
        </el-form-item>

        <el-form-item label="标题">
          <el-input v-model="form.title" maxlength="20" show-word-limit placeholder="留空则由 AI 生成" style="width: 480px" />
        </el-form-item>

        <el-form-item label="正文">
          <el-input v-model="form.desc" type="textarea" :rows="5" maxlength="1000" show-word-limit placeholder="留空则由 AI 生成" />
        </el-form-item>

        <el-form-item label="话题标签">
          <el-input v-model="tagsText" placeholder="逗号分隔，不带 #，最多 10 个" style="width: 480px" />
        </el-form-item>

        <el-form-item label="合集">
          <el-select v-model="form.collection" placeholder="不选则保持不选（宁可不选也不乱选）" clearable filterable allow-create style="width: 320px">
            <el-option v-for="c in collections" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>

        <el-form-item label="可见范围">
          <el-select v-model="form.visibility" style="width: 200px">
            <el-option label="公开可见（默认）" value="公开可见" />
            <el-option label="仅自己可见" value="仅自己可见" />
            <el-option label="仅互关好友可见" value="仅互关好友可见" />
          </el-select>
        </el-form-item>

        <el-form-item label="定时发布">
          <el-date-picker
            v-model="form.schedule"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm"
            format="YYYY-MM-DD HH:mm"
            placeholder="留空则立即发布（平台要求 1 小时后 ~ 14 天内）"
            style="width: 320px"
          />
        </el-form-item>

        <el-form-item label="原创声明">
          <el-switch v-model="form.original" />
          <span class="hint ml8">仅在内容确实原创时才开启</span>
        </el-form-item>

        <el-form-item label="演练模式">
          <el-switch v-model="form.dry_run" />
          <span class="hint ml8">
            默认关闭（正常发布）。打开后只走完流程但不点提交按钮，用来验证页面操作是否正常。
          </span>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="doPublish">
            {{ form.dry_run ? '提交演练任务' : '提交到队列' }}
          </el-button>
          <span class="hint ml8">
            提交后立即返回，在后台队列执行。AI 生成完文案会停在「待确认」，
            需要你在下面的任务列表里核对并点「确认发布」，**才会真正发出**。
          </span>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 任务队列 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <div class="card-header">
          <span>⑤ 发布任务（后台队列，串行执行）</span>
          <el-button link type="primary" @click="loadTasks">刷新</el-button>
        </div>
      </template>
      <el-table :data="tasks" size="small" empty-text="还没有任务">
        <el-table-column prop="title" label="标题" show-overflow-tooltip min-width="160" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="stateTagType(row.state)" size="small">{{ stateLabel(row.state) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="模式" width="80">
          <template #default="{ row }">
            {{ row.dry_run ? '演练' : row.scheduled ? '定时' : '立即' }}
          </template>
        </el-table-column>
        <el-table-column label="浏览器" width="90">
          <template #default="{ row }">
            <el-tag v-if="browserModeLabel(row) === '有头'" type="success" size="small">有头</el-tag>
            <span v-else>{{ browserModeLabel(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="collection" label="合集" width="110" show-overflow-tooltip />
        <el-table-column prop="message" label="说明" show-overflow-tooltip min-width="180" />
        <el-table-column label="操作" width="240">
          <template #default="{ row }">
            <template v-if="row.state === 'pending_confirm'">
              <el-button type="primary" size="small" @click="openConfirm(row)">确认发布</el-button>
              <el-button link type="primary" size="small" @click="doRegenerate(row)">重生成</el-button>
              <el-button link type="danger" size="small" @click="doCancel(row)">放弃</el-button>
            </template>
            <el-button v-else link type="primary" size="small" @click="viewTask(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>

      <template v-if="task">
        <el-divider>{{ task.title }} · 执行进度</el-divider>
        <el-alert
          :type="taskType"
          :closable="false"
          show-icon
          :title="`状态: ${stateLabel(task.state)}${task.message ? ' — ' + task.message : ''}`"
        />
        <!-- 确认发布会同时写一条「发布记录」，给个入口省得用户自己找 -->
        <el-button
          v-if="task.record_id"
          link
          type="primary"
          size="small"
          class="mt8"
          @click="router.push('/publish-records')"
        >
          这条已记入「发布记录」→
        </el-button>
        <el-table :data="task.steps || []" size="small" class="mt12">
          <el-table-column prop="step" label="步骤" width="200" />
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag :type="stepTagType(row.status)" size="small">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="message" label="说明" show-overflow-tooltip />
        </el-table>
      </template>
    </el-card>

    <!-- 二次确认：确认后才真正发布 -->
    <el-dialog v-model="confirmVisible" title="确认发布" width="680px" top="6vh">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="点「确认发布」后才会真正执行——之前不会发布任何内容"
        description="下面是 AI 生成的文案，可以直接改——改成你自己的话比什么都重要。确认后会在本机弹出浏览器窗口完成上传与发布。"
      />
      <el-form label-width="90px" class="mt12">
        <el-form-item label="标题">
          <el-input v-model="confirmForm.title" maxlength="20" show-word-limit />
        </el-form-item>
        <el-form-item label="正文">
          <el-input v-model="confirmForm.desc" type="textarea" :rows="7" maxlength="1000" show-word-limit />
          <div class="hint">
            AI 按 {{ descMaxChars }} 字以内生成（可在「设置」里调）。
            <span v-if="confirmForm.desc.length > descMaxChars" class="over-limit">
              当前 {{ confirmForm.desc.length }} 字，已超出目标长度
            </span>
          </div>
        </el-form-item>
        <el-form-item label="话题标签">
          <el-input v-model="confirmForm.tags" placeholder="逗号分隔，最多 10 个" />
        </el-form-item>
        <el-form-item label="合集">
          <el-select v-model="confirmForm.collection" clearable filterable allow-create style="width: 260px">
            <el-option v-for="c in collections" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="可见范围">
          <el-select v-model="confirmForm.visibility" style="width: 200px">
            <el-option label="公开可见" value="公开可见" />
            <el-option label="仅自己可见" value="仅自己可见" />
            <el-option label="仅互关好友可见" value="仅互关好友可见" />
          </el-select>
        </el-form-item>
        <el-form-item label="定时发布">
          <el-date-picker
            v-model="confirmForm.schedule"
            type="datetime"
            placeholder="留空则立即发布"
            format="YYYY-MM-DD HH:mm"
            value-format="YYYY-MM-DD HH:mm"
            style="width: 260px"
          />
        </el-form-item>
        <el-form-item label="原创声明">
          <el-switch v-model="confirmForm.original" />
          <span class="hint ml8">仅在内容确实原创时开启</span>
        </el-form-item>
        <el-form-item label="演练模式">
          <el-switch v-model="confirmForm.dry_run" />
          <span class="hint ml8">打开则只走流程、不点提交按钮</span>
        </el-form-item>
      </el-form>

      <div class="mode-box">
        <div class="mode-title">浏览器模式</div>
        <div class="hint">
          <b>有头（固定）</b>：发布时在本机弹出真实浏览器窗口，全程可见。
          无头发布已移除——平台明确禁止"利用技术手段模拟真人"，无头是这条链路上最扎眼的
          行为特征，也是最容易被判定为「AI 托管」的触发点。<br />
          发布期间请不要关闭/最小化那个窗口，也不要手动点页面上的按钮。
        </div>
      </div>

      <template #footer>
        <el-button :disabled="!!confirmingMode" @click="confirmVisible = false">再想想</el-button>
        <el-button
          type="success"
          :loading="confirmingMode === 'headed'"
          :disabled="!!confirmingMode"
          @click="doConfirm('headed')"
        >
          确认发布（有头浏览器）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { xhsApi } from '@/api/xhs'

const router = useRouter()
const account = ref('default')
const accounts = ref([])
const loggingIn = ref(false)
const loginStatus = ref('')
const loginStatusType = ref('info')
const qrDataUrl = ref('')

const testing = ref(false)
const selftest = ref(null)
const selftestVideo = ref('')

const loadingCollections = ref(false)
const collections = ref([])
const collectionMessage = ref('')
const collectionProbeVideo = ref('')

const materials = ref([])
const uploading = ref(false)
const uploadPercent = ref(0)
const submitting = ref(false)
const tasks = ref([])
const defaultCollection = ref('')
const descMaxChars = ref(100)
const tagsText = ref('')
const task = ref(null)

// 二次确认
const confirmVisible = ref(false)
// 确认按钮的 loading 状态：'' | 'headed'（只剩有头一档）
const confirmingMode = ref('')
const confirmTaskId = ref('')
const confirmForm = ref({
  title: '',
  desc: '',
  tags: '',
  collection: '',
  visibility: '公开可见',
  schedule: '',
  original: false,
  dry_run: false
})

const form = ref({
  video: '',
  title: '',
  desc: '',
  collection: '',
  visibility: '公开可见',
  schedule: '',
  original: false,
  // 默认关闭：默认就是正常发布，演练模式是手动打开的验证手段
  dry_run: false,
  // 默认开启：提交后自动生成文案，生成完停在"待确认"由人把关
  ai_generate: true,
  hints: ''
})

let eventSource = null
let pollTimer = null

const STATE_LABELS = {
  pending: '待执行',
  analyzing: 'AI 生成中',
  pending_confirm: '待确认',
  queued: '排队中',
  running: '发布中',
  success: '成功',
  failed: '失败',
  cancelled: '已放弃'
}

const stateLabel = (s) => STATE_LABELS[s] || s || '-'

const stateTagType = (s) => {
  if (s === 'success') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'running') return 'primary'
  if (s === 'queued') return 'warning'
  if (s === 'pending_confirm') return 'warning'
  if (s === 'analyzing') return 'primary'
  return 'info'
}

const taskType = computed(() => {
  if (!task.value) return 'info'
  if (task.value.state === 'success') return 'success'
  if (task.value.state === 'failed') return 'error'
  return 'info'
})

const stepTagType = (status) => {
  if (status === 'ok') return 'success'
  if (status === 'failed') return 'danger'
  return 'warning'
}

/** 发布一律走有头（无头档已移除），所以这里不再有"确认时选"的中间态 */
function browserModeLabel(row) {
  if (!row || row.state === 'cancelled') return '-'
  return '有头'
}

async function loadAccounts() {
  try {
    const res = await xhsApi.listAccounts(false)
    if (res.code === 200) accounts.value = res.data || []
  } catch (e) {
    /* 拦截器已提示 */
  }
}

async function loadMaterials() {
  try {
    const res = await xhsApi.listMaterials()
    if (res.code === 200) {
      materials.value = res.data || []
    }
  } catch (e) {
    /* 忽略 */
  }
}

function closeLoginStream() {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}

function startLogin() {
  if (!account.value) {
    ElMessage.warning('请先填写账号标识')
    return
  }
  closeLoginStream()
  qrDataUrl.value = ''
  loggingIn.value = true
  loginStatus.value = '正在启动浏览器并获取二维码…'
  loginStatusType.value = 'info'

  eventSource = new EventSource(xhsApi.loginStreamUrl(account.value))
  eventSource.onmessage = async (e) => {
    let payload
    try {
      payload = JSON.parse(e.data)
    } catch (err) {
      return
    }
    if (payload.event === 'qrcode') {
      qrDataUrl.value = payload.data?.image_data_url || ''
      loginStatus.value = '二维码已就绪，请用小红书 App 扫码'
      loginStatusType.value = 'info'
    } else if (payload.event === 'done') {
      const ok = !!payload.data?.success
      loginStatus.value = ok ? '登录成功！' : `登录未完成：${payload.data?.message || '未知原因'}`
      loginStatusType.value = ok ? 'success' : 'error'
      loggingIn.value = false
      closeLoginStream()
      await loadAccounts()
    } else if (payload.event === 'error') {
      loginStatus.value = `登录出错：${payload.data}`
      loginStatusType.value = 'error'
      loggingIn.value = false
      closeLoginStream()
    } else if (payload.event === 'timeout') {
      loginStatus.value = '等待超时，请重新获取二维码'
      loginStatusType.value = 'warning'
      loggingIn.value = false
      closeLoginStream()
    }
  }
  eventSource.onerror = () => {
    // 连接结束时浏览器必然触发 onerror，这里只在还处于登录中时提示
    if (loggingIn.value) {
      loginStatus.value = '连接已断开，请重试'
      loginStatusType.value = 'warning'
      loggingIn.value = false
    }
    closeLoginStream()
  }
}

async function checkAccount() {
  try {
    const res = await xhsApi.checkAccount(account.value)
    if (res.code === 200) {
      const ok = res.data?.valid
      ElMessage[ok ? 'success' : 'warning'](res.data?.message || (ok ? 'cookie 有效' : 'cookie 失效'))
      await loadAccounts()
    }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

async function runSelftest(deep = false) {
  if (!account.value) {
    ElMessage.warning('请先填写账号标识')
    return
  }
  if (deep && !selftestVideo.value) {
    ElMessage.warning('深自检需要先选一个测试视频')
    return
  }
  testing.value = true
  selftest.value = null
  try {
    const res = await xhsApi.selftest(account.value, deep ? selftestVideo.value : null)
    if (res.code === 200) {
      selftest.value = res.data
      ElMessage[res.data.ok ? 'success' : 'warning'](
        `${res.data.deep ? '深' : '浅'}自检完成，命中 ${res.data.hit}/${res.data.total}`
      )
    }
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    testing.value = false
  }
}

async function loadCollections(video = null) {
  if (!account.value) {
    ElMessage.warning('请先填写账号标识')
    return
  }
  loadingCollections.value = true
  collectionMessage.value = ''
  try {
    const res = await xhsApi.listCollections(account.value, video)
    if (res.code === 200) {
      const d = res.data || {}
      collections.value = d.detail?.collections || []
      collectionMessage.value = d.message || ''
    }
  } catch (e) {
    /* 拦截器已提示（例如"还没有合集缓存"） */
  } finally {
    loadingCollections.value = false
  }
}

/** 时间戳 → 本地时间文本（素材列表里显示上传日期） */
function fmtTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 上传本地视频到素材库（el-upload 的自定义上传入口） */
async function doUpload(options) {
  const file = options?.file
  if (!file) return
  uploading.value = true
  uploadPercent.value = 0
  try {
    const fd = new FormData()
    fd.append('file', file)
    const res = await xhsApi.uploadVideo(fd, (evt) => {
      if (evt.total) uploadPercent.value = Math.round((evt.loaded / evt.total) * 100)
    })
    if (res.code === 200) {
      // 后端返回磁盘文件名（uuid_原名），素材列表用的也是这个
      form.value.video = res.data
      ElMessage.success(`已上传：${file.name}`)
      await loadMaterials()
      options.onSuccess?.(res)
    } else {
      options.onError?.(new Error(res.msg || '上传失败'))
    }
  } catch (e) {
    // 响应拦截器已提示，这里只需让 el-upload 知道失败
    options.onError?.(e)
  } finally {
    uploading.value = false
    uploadPercent.value = 0
  }
}

async function loadTasks() {
  try {
    const res = await xhsApi.listTasks()
    if (res.code === 200) {
      tasks.value = res.data || []
      // 正在执行的任务，同步刷新详情面板
      if (task.value) {
        const fresh = tasks.value.find((t) => t.id === task.value.id)
        if (fresh) task.value = fresh
      }
    }
  } catch (e) {
    /* 忽略 */
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/** 任务列表轮询：发布是后台队列，这里只负责"看"，不阻塞任何操作 */
function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(loadTasks, 2500)
}

function viewTask(row) {
  task.value = row
}

/** 打开二次确认弹窗，带上 AI 生成的文案（可改） */
function openConfirm(row) {
  confirmTaskId.value = row.id
  confirmForm.value = {
    title: row.title || '',
    desc: row.desc || '',
    tags: (row.tags || []).join(','),
    collection: row.collection || defaultCollection.value || '',
    visibility: row.visibility || '公开可见',
    schedule: row.schedule || '',
    original: !!row.original,
    dry_run: !!row.dry_run
  }
  confirmVisible.value = true
}

/**
 * 二次确认 → 后端才真正执行发布。
 * 模式固定为 'headed'（本机弹窗）。无头发布已移除：平台明确禁止"利用技术手段模拟真人"，
 * 无头是发布链路上最扎眼的行为特征。后端也会强制覆盖，这里传的值只是保持一致。
 */
async function doConfirm(mode = 'headed') {
  if (!confirmForm.value.title.trim()) {
    ElMessage.warning('标题不能为空')
    return
  }
  confirmingMode.value = mode
  try {
    const res = await xhsApi.confirmTask(confirmTaskId.value, {
      title: confirmForm.value.title,
      desc: confirmForm.value.desc,
      tags: confirmForm.value.tags
        .split(',')
        .map((t) => t.trim().replace(/^#/, ''))
        .filter(Boolean),
      collection: confirmForm.value.collection,
      visibility: confirmForm.value.visibility,
      schedule: confirmForm.value.schedule || null,
      original: confirmForm.value.original,
      dry_run: confirmForm.value.dry_run,
      // 固定有头；后端同样会强制覆盖成有头，两边一致
      headless: false
    })
    if (res.code === 200) {
      confirmVisible.value = false
      ElMessage.success('已确认，正在执行发布（本机会弹出浏览器窗口，请不要关闭它）')
      await loadTasks()
      startPolling()
    }
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    confirmingMode.value = ''
  }
}

async function doRegenerate(row) {
  try {
    const res = await xhsApi.regenerateTask(row.id)
    if (res.code === 200) {
      ElMessage.info('正在重新生成…')
      await loadTasks()
    }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

async function doCancel(row) {
  try {
    const res = await xhsApi.cancelTask(row.id)
    if (res.code === 200) {
      ElMessage.success('已放弃该任务')
      await loadTasks()
    }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

/** 发布默认值：默认合集等 */
async function loadDefaults() {
  try {
    const res = await xhsApi.getDefaults()
    if (res.code === 200) {
      defaultCollection.value = res.data?.default_collection || ''
      // 正文字数上限来自「设置」页的模型配置，用于在确认弹窗里提示
      try {
        const s = await xhsApi.getSettings()
        if (s.code === 200) descMaxChars.value = s.data?.vision?.desc_max_chars || 100
      } catch (e) {
        /* 忽略 */
      }
      if (defaultCollection.value && !form.value.collection) {
        form.value.collection = defaultCollection.value
      }
    }
  } catch (e) {
    /* 忽略 */
  }
}

/** 提交任务（不直接发布） */
async function doPublish() {
  if (!form.value.video) {
    ElMessage.warning('请先上传或选择视频素材')
    return
  }
  // 交给 AI 生成时标题可以先留空
  if (!form.value.ai_generate && !form.value.title.trim()) {
    ElMessage.warning('未开启 AI 生成时，标题必填')
    return
  }
  submitting.value = true
  const tags = tagsText.value
    .split(',')
    .map((t) => t.trim().replace(/^#/, ''))
    .filter(Boolean)

  try {
    const res = await xhsApi.publish({
      account: account.value,
      video: form.value.video,
      title: form.value.title,
      desc: form.value.desc,
      tags,
      hints: form.value.hints,
      ai_generate: form.value.ai_generate,
      collection: form.value.collection || null,
      visibility: form.value.visibility,
      schedule: form.value.schedule || null,
      original: form.value.original,
      dry_run: form.value.dry_run
    })
    if (res.code === 200) {
      const taskId = res.data?.task_id
      // 立即解锁：任务已在后端队列里
      submitting.value = false
      ElMessage.success(
        form.value.ai_generate
          ? '已入队，AI 正在生成文案；生成完请在下方任务列表点「确认发布」'
          : '已入队，请在下方任务列表点「确认发布」'
      )
      // 清掉表单，避免误以为还要再点一次
      form.value.title = ''
      form.value.desc = ''
      tagsText.value = ''
      await loadTasks()
      const created = tasks.value.find((t) => t.id === taskId)
      if (created) task.value = created
      startPolling()
    }
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadAccounts()
  loadMaterials()
  loadCollections() // 不传视频 → 走缓存，秒回
  loadDefaults() // 默认合集（风景）
  loadTasks()
  startPolling()
})

onBeforeUnmount(() => {
  closeLoginStream()
  stopPolling()
})
</script>

<style scoped>
.xhs-workbench {
  max-width: 1100px;
}
.mb16 { margin-bottom: 16px; }
.mb8 { margin-bottom: 8px; }
.mt12 { margin-top: 12px; }
.mt8 { margin-top: 8px; }
.mr8 { margin-right: 8px; }
.ml8 { margin-left: 8px; }
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
.qr-area {
  display: flex;
  gap: 24px;
  align-items: flex-start;
}
.qr-img {
  width: 220px;
  height: 220px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  object-fit: contain;
  background: #fff;
}
.qr-side {
  flex: 1;
}

.over-limit {
  color: #e6a23c;
  margin-left: 6px;
}

/* 确认弹窗里的「浏览器模式」说明块 */
.mode-box {
  background: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 10px 12px;
}
.mode-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 4px;
}
</style>
