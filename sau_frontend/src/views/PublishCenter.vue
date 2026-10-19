<template>
  <div class="publish-center">
    <div class="page-header">
      <h1>发布中心</h1>
      <span class="hint">选素材 → 核对文案 → 勾平台 → 一键发；提交后在这里看每个账号、每条素材的实时进度</span>
    </div>

    <!-- ① 选择素材 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <div class="card-header">
          <span>① 选择素材</span>
          <div>
            <el-button type="primary" @click="materialDialogVisible = true">
              从素材库选择
            </el-button>
            <el-upload
              class="inline-upload"
              :show-file-list="false"
              :http-request="uploadLocal"
              accept="video/*"
              :disabled="uploading"
            >
              <el-button :loading="uploading">上传本地视频</el-button>
            </el-upload>
          </div>
        </div>
      </template>

      <el-alert
        v-if="!items.length"
        type="info"
        :closable="false"
        show-icon
        title="还没有选择素材"
        description="点右上角「从素材库选择」，或直接上传本地视频。选中后会按素材自带的 AI 文案自动带出标题、正文和话题。"
      />

      <!-- 已选素材：每条可单独改文案。素材多的时候整页全是表单，选完只想核对平台/账号，
           所以每条素材折成一栏（标题：素材名 · 文案状态），点开才编辑 -->
      <el-collapse v-model="expandedItems" class="item-collapse">
        <el-collapse-item v-for="(item, idx) in items" :key="item.name" :name="item.name">
          <template #title>
            <div class="item-head">
              <div class="item-title">
                <el-tag size="small" type="info">素材 {{ idx + 1 }}</el-tag>
                <span class="name">{{ item.name }}</span>
                <span class="meta">{{ item.sizeText }} · {{ item.mtimeText }}</span>
              </div>
              <el-tag v-if="item.hasCopy" size="small" type="success" effect="plain">已带出文案</el-tag>
              <el-tag v-else size="small" type="warning" effect="plain">文案待补</el-tag>
            </div>
          </template>

          <!-- 操作按钮放在面板内容顶部：折叠栏里只留状态标签，标题一行的信息密度不挤 -->
          <div class="item-toolbar">
            <el-button
              size="small"
              type="primary"
              plain
              :loading="item.analyzing"
              @click="generateCopy(item)"
            >
              {{ item.hasCopy ? '重新生成' : 'AI 生成文案' }}
            </el-button>
            <el-button size="small" type="danger" plain @click="removeItem(idx)">移除</el-button>
          </div>

          <el-form label-width="64px" class="item-form">
            <el-form-item label="标题">
              <el-input v-model="item.title" maxlength="20" show-word-limit placeholder="不超过 20 字" />
            </el-form-item>
            <el-form-item label="正文">
              <el-input
                v-model="item.desc"
                type="textarea"
                :rows="4"
                maxlength="1000"
                show-word-limit
                placeholder="留空也可以，各平台受限不同"
              />
            </el-form-item>
            <el-form-item label="话题">
              <el-input v-model="item.tagsText" placeholder="逗号分隔，不带 # 号" />
            </el-form-item>
          </el-form>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- ② 平台与账号 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <div class="card-header">
          <span>② 发布到哪些平台</span>
          <span class="hint">勾选平台后，再选该平台下要发布的账号（可多选）</span>
        </div>
      </template>

      <div v-for="p in PLATFORMS" :key="p.key" class="platform-row">
        <div class="platform-head">
          <el-checkbox
            :model-value="selectedPlatforms.includes(p.key)"
            @change="(v) => togglePlatform(p.key, v)"
          >
            <span class="platform-name">{{ p.name }}</span>
          </el-checkbox>
          <el-tag v-if="!accountsOf(p.key).length" size="small" type="warning" effect="plain">
            没有账号，请先到「账号管理」添加
          </el-tag>
        </div>

        <!-- 平台风险提示。小红书这条是用户明确要求加在发布选项上的：
             它的风控在几个平台里最严（对"模拟真人"本身就有治理规则），
             工作台那条路是有头模式 + 逐字段人工确认，出问题的概率明显更低。 -->
        <div v-if="PLATFORM_NOTICE[p.key]" class="platform-notice">
          <span class="platform-notice-icon">⚠️</span>
          <span>{{ PLATFORM_NOTICE[p.key] }}</span>
        </div>

        <div v-if="selectedPlatforms.includes(p.key)" class="platform-body">
          <el-select
            v-model="platformAccounts[p.key]"
            multiple
            collapse-tags
            collapse-tags-tooltip
            placeholder="选择账号"
            style="width: 340px"
            :disabled="!accountsOf(p.key).length"
          >
            <el-option
              v-for="a in accountsOf(p.key)"
              :key="a.id"
              :label="`${a.name}${a.status === '正常' ? '' : '（' + a.status + '）'}`"
              :value="a.id"
            />
          </el-select>
          <el-checkbox v-if="p.key === 2" v-model="isDraft" class="ml12">
            仅保存草稿（用手机发布）
          </el-checkbox>
        </div>
      </div>
    </el-card>

    <!-- ③ 选项 -->
    <el-card shadow="never" class="mb16">
      <template #header><span>③ 发布选项</span></template>
      <div class="option-row">
        <el-checkbox v-model="isOriginal">声明原创</el-checkbox>
        <span class="hint ml8">仅在内容确实原创时勾选</span>
      </div>
      <div class="option-row">
        <el-checkbox v-model="skipPublished">跳过已发布过的素材</el-checkbox>
        <span class="hint ml8">
          按<strong>平台</strong>判：这条素材在某个平台已经发成功过，就不再往那个平台发
          （同平台的多个账号一起跳过），别的平台照发；取消勾选则全部照发。
        </span>
      </div>
      <div class="option-row">
        <el-checkbox v-model="scheduleEnabled">定时发布</el-checkbox>
        <template v-if="scheduleEnabled">
          <span class="ml12">每天</span>
          <el-input-number v-model="videosPerDay" :min="1" :max="10" size="small" class="ml8" />
          <span class="ml8">条，从</span>
          <el-select v-model="startDays" size="small" style="width: 100px" class="ml8">
            <el-option label="明天" :value="0" />
            <el-option label="后天" :value="1" />
            <el-option label="大后天" :value="2" />
          </el-select>
          <span class="ml8">开始，时间点</span>
          <el-select
            v-model="dailyTimes"
            multiple
            allow-create
            filterable
            default-first-option
            size="small"
            style="width: 240px"
            class="ml8"
            placeholder="如 10:00"
          >
            <el-option v-for="t in ['09:00', '12:00', '18:00', '20:00']" :key="t" :label="t" :value="t" />
          </el-select>
        </template>
      </div>

      <el-collapse v-if="selectedPlatforms.includes(3)" class="mt12">
        <el-collapse-item title="抖音商品链接（可选）" name="dy">
          <el-input v-model="productTitle" placeholder="商品名称" class="mb8" />
          <el-input v-model="productLink" placeholder="商品链接" />
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- ④ 发布 + 实时进度 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>④ 发布</span>
          <span class="hint">
            将按「素材 × 平台 × 账号」拆成 {{ taskCount }} 个发布任务，每个账号一条通道并行跑；
            同一个账号已经发过的素材，提交前会再确认一次
          </span>
        </div>
      </template>

      <div class="publish-actions">
        <el-button
          type="primary"
          size="large"
          :loading="submitting"
          :disabled="!canPublish || publishing"
          @click="doPublish"
        >
          开始发布
        </el-button>
        <el-button v-if="publishing" @click="cancelPending">取消未开始的任务</el-button>
        <el-button v-if="publishing" type="danger" plain @click="cancelAllRunning">
          中断全部未完成
        </el-button>
        <el-button v-if="jobId" @click="goRecords">查看发布记录</el-button>
        <span class="hint ml12">
          {{
            publishing
              ? `每个平台/账号各跑各的，同时最多 ${maxConcurrent} 个浏览器；同一账号内排队`
              : blockReason
          }}
        </span>
      </div>

      <template v-if="tasks.length">
        <el-divider>实时进度</el-divider>

        <div class="overall">
          <el-progress
            :percentage="summary.percent || 0"
            :status="progressStatus"
            :stroke-width="14"
          />
          <div class="stats">
            <el-tag size="small" effect="plain">共 {{ summary.total }} 个任务</el-tag>
            <el-tag size="small" type="info" effect="plain">排队 {{ summary.queued }}</el-tag>
            <el-tag size="small" type="primary" effect="plain">发布中 {{ summary.running }}</el-tag>
            <el-tag v-if="activeGroups > 1" size="small" type="success" effect="plain">
              {{ activeGroups }} 个账号并行中
            </el-tag>
            <el-tag size="small" type="success" effect="plain">成功 {{ summary.success }}</el-tag>
            <el-tag size="small" type="danger" effect="plain">失败 {{ summary.failed }}</el-tag>
            <el-tag v-if="summary.cancelled" size="small" type="warning" effect="plain">
              已取消 {{ summary.cancelled }}
            </el-tag>
          </div>
        </div>

        <!-- 每个「平台 · 账号」一块，块内是这一账号要发的每条素材 -->
        <div v-for="g in groups" :key="g.key" class="group" :class="{ 'is-active': g.active }">
          <div class="group-head">
            <div class="group-title">
              <el-tag size="small" :type="g.active ? 'primary' : 'info'" effect="dark">
                {{ g.platform_name }}
              </el-tag>
              <span class="account">{{ g.account_name }}</span>
              <el-icon v-if="g.active" class="is-loading spin"><Loading /></el-icon>
            </div>
            <span class="hint">完成 {{ g.done }} / {{ g.tasks.length }}</span>
          </div>

          <el-table :data="g.tasks" size="small" row-key="task_id" @expand-change="onExpandChange">
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="task-detail">
                  <div class="detail-line">
                    <span class="detail-label">说明</span>
                    <span>{{ row.message || row.action || '—' }}</span>
                  </div>
                  <template v-if="details[row.task_id]?.steps?.length">
                    <div class="detail-label mt8">执行步骤</div>
                    <div
                      v-for="(s, i) in details[row.task_id].steps"
                      :key="i"
                      class="step-line"
                    >
                      <el-tag size="small" :type="stepTagType(s.status)" effect="plain">
                        {{ s.step }}
                      </el-tag>
                      <span>{{ s.message }}</span>
                    </div>
                  </template>
                  <template v-if="details[row.task_id]?.log?.length">
                    <div class="detail-label mt8">执行日志</div>
                    <pre class="log">{{ formatLog(details[row.task_id].log) }}</pre>
                  </template>
                  <div v-else-if="!details[row.task_id]" class="hint">加载中…</div>
                  <div v-else class="hint">这个任务还没有日志输出</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="素材" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="material">{{ row.material_title || row.material }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="statusMeta(row.status).type">
                  {{ statusMeta(row.status).text }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="当前动作" min-width="240" show-overflow-tooltip>
              <template #default="{ row }">
                <span :class="{ 'action-running': row.status === 'running' }">
                  <template v-if="row.status === 'queued'">
                    {{ row.queue_position ? `排队中（同账号前面还有 ${row.queue_position - 1} 个）` : '排队中' }}
                  </template>
                  <template v-else-if="row.status === 'running'">
                    <el-tag v-if="row.prompt" size="small" type="warning" effect="dark" class="mr4">
                      等人输入
                    </el-tag>
                    {{ row.stage_text }}：{{ row.action || '…' }}
                  </template>
                  <template v-else>{{ row.message || row.action || '—' }}</template>
                </span>
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="80">
              <template #default="{ row }">{{ formatDuration(row) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <!-- 有任务卡着不想等就点这里：排队中的、正在跑的都能中断 -->
                <el-button
                  v-if="row.cancellable"
                  size="small"
                  type="danger"
                  plain
                  :loading="cancelling[row.task_id]"
                  @click="interruptTask(row)"
                >
                  中断
                </el-button>
                <span v-else class="hint">—</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </template>
    </el-card>

    <!-- 素材库选择弹窗 -->
    <el-dialog v-model="materialDialogVisible" title="从素材库选择" width="820px" top="8vh">
      <el-input v-model="materialKeyword" placeholder="搜索素材名" clearable class="mb12" />
      <el-table
        ref="materialTableRef"
        :data="filteredMaterials"
        height="420"
        class="material-pick-table"
        @selection-change="onMaterialSelectionChange"
        @row-click="onMaterialRowClick"
      >
        <el-table-column type="selection" width="46" />
        <el-table-column prop="name" label="素材" show-overflow-tooltip min-width="220" />
        <el-table-column prop="size_mb" label="大小" width="90">
          <template #default="{ row }">{{ row.size_mb }} MB</template>
        </el-table-column>
        <el-table-column prop="mtime_text" label="上传时间" width="150" />
        <el-table-column label="AI 文案" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.has_copy ? 'success' : 'info'" effect="plain">
              {{ row.has_copy ? '已有' : '无' }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <span class="hint">已选 {{ pendingMaterials.length }} 个</span>
        <el-button @click="materialDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmMaterials">加入发布列表</el-button>
      </template>
    </el-dialog>

    <!-- 同一个账号已经发过的素材：二次确认（默认不显示，点「开始发布」时查到重复才弹） -->
    <el-dialog
      v-model="repeatDialogVisible"
      title="这些内容这个账号已经发过了"
      width="720px"
      top="10vh"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="同一个账号再发一次同一条素材，平台上就会多出一条重复内容"
        description="可能是素材还留在发布列表里、又点了一遍；也可能你确实想重发。核对无误再继续。"
      />
      <el-table :data="repeatConflicts" size="small" max-height="320" class="mt12">
        <el-table-column label="素材" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.material_title || row.material }}</template>
        </el-table-column>
        <el-table-column label="平台 / 账号" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.platform_name }} · {{ row.account_name }}</template>
        </el-table-column>
        <el-table-column label="上次发布" min-width="230" show-overflow-tooltip>
          <template #default="{ row }">{{ row.message }}</template>
        </el-table-column>
        <el-table-column label="来源" width="110">
          <template #default="{ row }">{{ row.last_source_text || '—' }}</template>
        </el-table-column>
      </el-table>
      <div class="hint mt8">
        这一批共 {{ repeatTaskCount }} 条任务（素材 × 账号），其中 {{ repeatConflicts.length }} 条此前已经发过。
        确认后这次提交会在任务日志里记一句"已确认重发"，事后翻发布记录能分清。
      </div>
      <template #footer>
        <el-button @click="repeatDialogVisible = false">我再看看</el-button>
        <el-button type="danger" :loading="submitting" @click="confirmRepeatAndPublish">
          确认，继续发布
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { accountApi } from '@/api/account'
import { xhsApi } from '@/api/xhs'
import { PLATFORMS, publishApi, statusMeta } from '@/api/publish'
import { useAccountStore } from '@/stores/account'

const router = useRouter()
const accountStore = useAccountStore()

const materials = ref([])
const materialKeyword = ref('')
const materialDialogVisible = ref(false)
const materialTableRef = ref(null)
const pendingMaterials = ref([])
const uploading = ref(false)

// 待发布项：每个素材一份文案
const items = ref([])
// 展开编辑文案的素材（按素材名）。选完素材默认全折叠——一次选十来条时全展开表单
// 根本看不过来，要点哪条改文案再点开哪条。
const expandedItems = ref([])

// 平台级提示语：发布中心里跟在平台名下面显示。
// 目前只有小红书一条（用户要求）：它的风控最严，建议改走「小红书工作台」的有头模式。
const PLATFORM_NOTICE = {
  1: '小红书风控较严格，建议走工作台有头模式发布作品',
}

const selectedPlatforms = ref([])
// 每个平台选中的账号。**从 PLATFORMS 派生，不要手写 1~4**：
// 原来这里写死 `{ 1: [], 2: [], 3: [], 4: [] }`，加第 5 个平台（拼多多视频）时就漏了——
// `platformAccounts[5]` 恒为 undefined，表现是"这个平台的下拉框永远空着、每次都要手选"，
// 而且任何自动选账号的代码走到 `platformAccounts[key].length` 都会抛 TypeError 被静默吞掉。
const platformAccounts = reactive(
  Object.fromEntries(PLATFORMS.map((p) => [p.key, []]))
)

const isOriginal = ref(false)
const isDraft = ref(false)
// 跳过"在对应平台已经发成功过"的素材（默认勾上）。
// 素材发完还留在发布列表里是很常见的，再点一次「开始发布」就会在平台上多出一条重复内容；
// 判据在后端按**平台**算（同平台有任一账号发成功过就算发过），见 publish_records.find_published_materials。
const skipPublished = ref(true)
const scheduleEnabled = ref(false)
const videosPerDay = ref(1)
const dailyTimes = ref(['10:00'])
const startDays = ref(0)
const productTitle = ref('')
const productLink = ref('')

// ---- 发布批次与实时进度 ----
const submitting = ref(false)
const jobId = ref('')
const jobStatus = ref('')
const summary = ref({ total: 0, queued: 0, running: 0, success: 0, failed: 0, cancelled: 0, interrupted: 0, done: 0, percent: 0 })
const tasks = ref([])
const details = ref({})
//: 正在中断的任务（按钮转圈用）。按 task_id 记，避免整表一起转
const cancelling = ref({})
//: 整机同时最多开几个浏览器（后端 SAU_MAX_CONCURRENT_BROWSERS），界面上要说清楚，
//: 否则用户看到"明明选了 4 个平台却只跑了 2 个"，会以为是坏了
const maxConcurrent = ref(4)
//: 「这个账号已经发过这条素材」的组合。点「开始发布」时后端查出来的，
//: 有它就弹窗让人确认——发重了平台上就是两条重复内容
const repeatConflicts = ref([])
//: 这一批一共要发多少条（素材 × 账号），弹窗里好说"N 条里有 M 条发过"
const repeatTaskCount = ref(0)
const repeatDialogVisible = ref(false)
let pollTimer = null

const filteredMaterials = computed(() => {
  const kw = materialKeyword.value.trim().toLowerCase()
  if (!kw) return materials.value
  return materials.value.filter((m) => m.name.toLowerCase().includes(kw))
})

const accountsOf = (key) => accountStore.accounts.filter((a) => a.type === key)

/** 有任务在跑（含排队）：此时不允许再提交一批，避免排队排到天边 */
const publishing = computed(() => summary.value.running > 0 || summary.value.queued > 0)

const progressStatus = computed(() => {
  if (publishing.value) return undefined
  if (!tasks.value.length) return undefined
  return summary.value.failed > 0 ? 'exception' : 'success'
})

/** 按「平台 · 账号」把任务分组，这就是"每个平台每个账号"的进度视图 */
const groups = computed(() => {
  const map = new Map()
  for (const task of tasks.value) {
    const key = `${task.platform}·${task.account_id}`
    if (!map.has(key)) {
      map.set(key, {
        key,
        platform: task.platform,
        platform_name: task.platform_name,
        account_name: task.account_name,
        tasks: [],
        done: 0,
        active: false
      })
    }
    const group = map.get(key)
    group.tasks.push(task)
    if (['success', 'failed', 'cancelled', 'interrupted'].includes(task.status)) group.done += 1
    if (task.status === 'running') group.active = true
  }
  return [...map.values()]
})

/** 同时有任务在跑的通道数。>1 就说明确实在并行，不是一个个排队。 */
const activeGroups = computed(() => groups.value.filter((g) => g.active).length)

/** 按「素材 × 平台 × 账号」算任务数，好让用户知道一次点下去会发多少条 */
const taskCount = computed(() => {
  let n = 0
  for (const item of items.value) {
    if (!item.title.trim() && !item.desc.trim()) continue
    for (const key of selectedPlatforms.value) {
      const ids = platformAccounts[key] || []
      // 每个账号都要发一遍，所以是账号数而不是 1
      if (ids.length) n += ids.length
    }
  }
  return n
})

const blockReason = computed(() => {
  if (!items.value.length) return '请先选择素材'
  if (!selectedPlatforms.value.length) return '请至少勾选一个平台'
  const missing = selectedPlatforms.value.filter((k) => !platformAccounts[k]?.length)
  if (missing.length) {
    const names = missing.map((k) => PLATFORMS.find((p) => p.key === k)?.name).join('、')
    return `${names} 还没选账号`
  }
  const noCopy = items.value.filter((i) => !i.title.trim())
  if (noCopy.length) return `有 ${noCopy.length} 个素材还没有标题`
  if (scheduleEnabled.value) {
    if (!dailyTimes.value.length) return '定时发布至少要选一个时间点'
    if (videosPerDay.value > dailyTimes.value.length) return '每天发布条数不能超过时间点个数'
  }
  return ''
})

const canPublish = computed(() => taskCount.value > 0 && !blockReason.value)

async function loadMaterials() {
  try {
    const res = await xhsApi.listMaterials()
    if (res.code === 200) materials.value = res.data || []
  } catch (e) {
    /* 拦截器已提示 */
  }
}

async function loadAccounts() {
  try {
    const res = await accountApi.getAccounts()
    if (res.code === 200) {
      accountStore.setAccounts(res.data || [])
      // 账号是异步来的：如果用户在加载完成**之前**就勾了平台，
      // 那时 `accountsOf(key)` 还是空的，全选没填上。这里在账号到位后补一次，
      // 只补"还没选过账号"的平台——已经选过（哪怕只选了一个）的保持原样。
      selectedPlatforms.value.forEach((key) => {
        const list = accountsOf(key)
        if (list.length && !(platformAccounts[key] || []).length) {
          platformAccounts[key] = list.map((a) => a.id)
        }
      })
    }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

function fmtSize(mb) {
  return mb >= 1 ? `${mb} MB` : `${Math.round(mb * 1024)} KB`
}

function formatDuration(row) {
  if (row.duration != null) return `${row.duration}s`
  if (row.status === 'running' && row.started_at) return `${Math.round(Date.now() / 1000 - row.started_at)}s`
  return '—'
}

function stepTagType(status) {
  if (status === 'ok') return 'success'
  if (status === 'failed') return 'danger'
  return 'warning'
}

function formatLog(lines) {
  return (lines || [])
    .map((line) => `${line.t || ''}  ${line.text}`)
    .join('\n')
}

function onMaterialSelectionChange(rows) {
  pendingMaterials.value = rows
}

/**
 * 点整行 = 勾选/取消勾选这条素材（不用非得瞄准那个小方框）。
 *
 * 勾选框那一格要**跳过去**：点它时勾选框自己已经把状态翻了，这里再 toggle 一次
 * 正好抵消，表现就是"点勾选框没反应"。`column.type === 'selection'` 就是那一格。
 */
function onMaterialRowClick(row, column) {
  if (column?.type === 'selection') return
  materialTableRef.value?.toggleRowSelection(row)
}

function confirmMaterials() {
  if (!pendingMaterials.value.length) {
    ElMessage.warning('请先勾选素材')
    return
  }
  for (const m of pendingMaterials.value) {
    if (items.value.some((i) => i.name === m.name)) continue
    items.value.push({
      name: m.name,
      sizeText: fmtSize(m.size_mb || 0),
      mtimeText: m.mtime_text || '',
      // 自动带出素材上的 AI 文案
      title: m.title || '',
      desc: m.desc || '',
      tagsText: (m.tags || []).join(','),
      hasCopy: !!m.has_copy,
      analyzing: false
    })
    // 新加进来的素材：有文案的直接折叠（不用看），没文案的展开提醒补
    if (!m.has_copy) expandedItems.value.push(m.name)
  }
  materialDialogVisible.value = false
  pendingMaterials.value = []
  materialTableRef.value?.clearSelection()
  ElMessage.success('已加入发布列表，文案已按素材自动带出')
}

function removeItem(idx) {
  items.value.splice(idx, 1)
}

/** 对某个素材单独生成文案，并存回素材（下次选中可直接带出） */
async function generateCopy(item) {
  item.analyzing = true
  try {
    const res = await xhsApi.analyze(item.name, '', true)
    if (res.code === 200) {
      item.title = res.data.title || ''
      item.desc = res.data.desc || ''
      item.tagsText = (res.data.tags || []).join(',')
      item.hasCopy = true
      // 后端会在生成文案时把素材改名成标题（文件名只留中文和数字）。
      // 必须跟着换成新文件名，否则提交发布时还指着旧文件，会报"视频文件不存在"。
      if (res.data.video_name) item.name = res.data.video_name
      if (res.data.rename_note) ElMessage.info(res.data.rename_note)
      ElMessage.success('文案已生成并保存到素材')
      await loadMaterials()
    }
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    item.analyzing = false
  }
}

/** 上传本地视频，上传完直接加入列表并生成文案 */
async function uploadLocal(options) {
  const file = options?.file
  if (!file) return
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    const res = await xhsApi.uploadVideo(fd)
    if (res.code === 200) {
      await loadMaterials()
      const m = materials.value.find((x) => x.name === res.data)
      if (m) {
        items.value.push({
          name: m.name,
          sizeText: fmtSize(m.size_mb || 0),
          mtimeText: m.mtime_text || '',
          title: m.title || '',
          desc: m.desc || '',
          tagsText: (m.tags || []).join(','),
          hasCopy: !!m.has_copy,
          analyzing: false
        })
      }
      ElMessage.success('上传完成，可点「AI 生成文案」')
    }
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    uploading.value = false
  }
}

function togglePlatform(key, checked) {
  if (checked) {
    if (!selectedPlatforms.value.includes(key)) selectedPlatforms.value.push(key)
    // 勾上平台就把该平台下的账号**全选**，省掉一次点击。
    //
    // 为什么改成"全选"而不是原来的"只有一个账号时才自动选"：多账号的人每次都得手动
    // 再勾一遍，而这里的语义本来就是"这个平台我都要发"——真要挑账号，展开后取消掉
    // 那几个即可（取消是显式动作，不会被这个默认值覆盖回来）。
    // 只在用户还没选过（空）时填：已经选过的保持原样，别把人的选择冲掉。
    const list = accountsOf(key)
    if (list.length && !(platformAccounts[key] || []).length) {
      platformAccounts[key] = list.map((a) => a.id)
    }
  } else {
    selectedPlatforms.value = selectedPlatforms.value.filter((k) => k !== key)
  }
}

// --------------------------------------------------------------------------- //
// 提交 + 轮询进度
// --------------------------------------------------------------------------- //

function buildPayload(confirmedRepeat = false) {
  return {
    items: publishItems(),
    targets: publishTargets(),
    // 放在顶层而不是 options 里：它是"建不建这条任务"的决定，不是发布方式，
    // 不该跟着任务落到上传器那边去（见 sau_backend_publish.publish_submit）。
    skip_published: skipPublished.value,
    options: {
      isOriginal: isOriginal.value,
      isDraft: isDraft.value,
      enableTimer: scheduleEnabled.value,
      videosPerDay: videosPerDay.value,
      dailyTimes: dailyTimes.value,
      startDays: startDays.value,
      productLink: productLink.value,
      productTitle: productTitle.value,
      thumbnail: '',
      // 用户已经在「这个账号发过了」的弹窗里点过确认；后端据此把重复发布写进任务日志
      confirmRepublish: !!confirmedRepeat
    }
  }
}

/** 要发的素材（跟 buildPayload 同一套筛选：没有标题也没有正文的不算一条） */
function publishItems() {
  return items.value
    .filter((item) => item.title.trim() || item.desc.trim())
    .map((item) => ({
      material: item.name,
      title: item.title.trim(),
      desc: item.desc.trim(),
      tags: item.tagsText
        .split(',')
        .map((t) => t.trim().replace(/^#/, ''))
        .filter(Boolean)
    }))
}

/** 要发的「平台 + 账号」组合 */
function publishTargets() {
  return selectedPlatforms.value
    .filter((key) => platformAccounts[key]?.length)
    .map((key) => ({ platform: key, account_ids: [...platformAccounts[key]] }))
}

/**
 * 查这批里有没有「同一个账号已经发过的素材」，返回后端那坨数据（没有则 null）。
 *
 * 查不了就当没有（老后端没这个接口、网络抖一下都算）：重复发布最坏是多一条重复内容，
 * 而卡住发布是更糟的体验——所以这里任何异常都不挡发布。
 */
async function fetchRepeatCheck() {
  try {
    const res = await publishApi.checkPublished({
      items: publishItems(),
      targets: publishTargets()
    })
    if (res.code !== 200) return null
    return res.data || null
  } catch (e) {
    return null
  }
}

async function doPublish() {
  if (repeatDialogVisible.value) return
  submitting.value = true
  try {
    // 先问一次"这些素材这个账号是不是发过了"：发重了平台上就是两条重复内容，
    // 得让用户明确知道再发（确认结果会跟着 options.confirmRepublish 一起提交）
    const check = await fetchRepeatCheck()
    let conflicts = check?.conflicts || []
    // 勾着「跳过已发布过的素材」时，"已经发成功过"的那些任务后端根本不会建，
    // 再弹窗问"这个账号发过了，还要发吗"就是白问一次——只留"还在路上"的那种冲突
    // （在飞的不算"已发布成功"，不会被跳过，确实值得提醒）。
    if (skipPublished.value) conflicts = conflicts.filter((c) => !c.published)
    if (conflicts.length) {
      repeatConflicts.value = conflicts
      repeatTaskCount.value = check.task_count || 0
      repeatDialogVisible.value = true
      return
    }
    await submitPublish(false)
  } finally {
    submitting.value = false
  }
}

/** 弹窗里点了「确认，继续发布」：带着确认标记再提交一次 */
async function confirmRepeatAndPublish() {
  repeatDialogVisible.value = false
  submitting.value = true
  try {
    await submitPublish(true)
  } finally {
    submitting.value = false
  }
}

/** 真正提交批次。`confirmedRepeat` = 用户已经看过「这个账号发过了」的提示 */
async function submitPublish(confirmedRepeat) {
  try {
    const res = await publishApi.submit(buildPayload(confirmedRepeat))
    if (res.code !== 200) return
    const data = res.data || {}
    const skipped = data.skipped || []

    // 全被跳过：后端没建批次（job_id 为空），别去轮询一个不存在的批次
    if (!data.task_count) {
      ElMessage.info(res.msg || '这一批素材在对应平台都已经发布过，没有需要发布的任务')
      return
    }

    jobId.value = data.job_id
    details.value = {}
    ElMessage.success(res.msg || '已提交发布任务')
    if (skipped.length) {
      // 说清楚"为什么条数比你数出来的少"，否则用户会以为是漏发了
      const where = [...new Set(skipped.map((s) => s.platform_name).filter(Boolean))].join('、')
      ElMessage.info(
        `已跳过 ${skipped.length} 条任务：${where || '对应平台'}上这些素材已经成功发布过` +
          `（不想跳过就取消勾选「跳过已发布过的素材」）`
      )
    }
    await refreshJob()
    startPolling()
  } catch (e) {
    /* 拦截器已提示 */
  }
}

async function refreshJob() {
  if (!jobId.value) return
  try {
    const res = await publishApi.getJob(jobId.value)
    if (res.code !== 200) return
    summary.value = res.data.summary
    tasks.value = res.data.tasks
    jobStatus.value = res.data.status
    if (res.data.max_concurrent_browsers) maxConcurrent.value = res.data.max_concurrent_browsers
    // 展开着的那条正在跑的任务，顺便把日志刷新一下
    const running = res.data.tasks.find((t) => t.status === 'running')
    if (running && details.value[running.task_id]) await loadDetail(running.task_id)
    // 全部任务都结束了就停轮询。除了看批次的 status，也直接看统计——
    // 万一 status 因为什么原因没翻过来，用户也不会看到一个永远转的进度条。
    const s = res.data.summary
    if (res.data.status === 'done' || (s.total > 0 && s.done === s.total)) {
      stopPolling()
    }
  } catch (e) {
    stopPolling()
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(refreshJob, 1500)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function loadDetail(taskId) {
  try {
    const res = await publishApi.getTask(taskId)
    if (res.code === 200) details.value = { ...details.value, [taskId]: res.data }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

function onExpandChange(row, expandedRows) {
  const expanded = (expandedRows || []).some((r) => r.task_id === row.task_id)
  if (expanded && !details.value[row.task_id]) loadDetail(row.task_id)
}

async function cancelPending() {
  if (!jobId.value) return
  try {
    await ElMessageBox.confirm(
      '只会取消还没开始的任务；正在执行的那一条不受影响（想连它一起停，用「中断全部未完成」或表格里的「中断」）。',
      '取消未开始的任务',
      { type: 'warning', confirmButtonText: '确认取消', cancelButtonText: '再等等' }
    )
  } catch (e) {
    return
  }
  try {
    const res = await publishApi.cancelJob(jobId.value)
    if (res.code === 200) {
      ElMessage.success(res.msg || '已取消')
      await refreshJob()
    }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

/** 中断单个任务（排队中的 / 正在跑的都能中断）。 */
async function interruptTask(row) {
  const running = row.status === 'running'
  try {
    await ElMessageBox.confirm(
      running
        ? `正在中断「${row.material_title || row.material}」的发布。\n\n` +
          '它可能已经把内容提交到平台、也可能只发了一半，请中断后到平台确认一次。' +
          '底层是浏览器自动化，线程未必马上停下，但这个任务不会一直卡着，' +
          '同一账号后面排队的任务会立刻往下走。'
        : `取消「${row.material_title || row.material}」这条还没开始的任务？`,
      running ? '中断正在跑的发布' : '取消未开始的任务',
      {
        type: 'warning',
        confirmButtonText: running ? '确认中断' : '确认取消',
        cancelButtonText: '再等等'
      }
    )
  } catch (e) {
    return
  }
  cancelling.value = { ...cancelling.value, [row.task_id]: true }
  try {
    const res = await publishApi.cancelTask(row.task_id)
    if (res.code === 200) {
      ElMessage.success(res.msg || '已中断')
      await refreshJob()
    }
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    const next = { ...cancelling.value }
    delete next[row.task_id]
    cancelling.value = next
  }
}

/** 中断这一批里所有还没结束的任务（含正在跑的）。 */
async function cancelAllRunning() {
  if (!jobId.value) return
  try {
    await ElMessageBox.confirm(
      `会中断这一批里所有还没结束的任务，包括正在发布的 ${summary.value.running} 个。\n\n` +
        '正在跑的已经可能把内容交到平台了，请中断后到平台确认一次。',
      '中断全部未完成的任务',
      { type: 'warning', confirmButtonText: '确认中断', cancelButtonText: '再等等' }
    )
  } catch (e) {
    return
  }
  try {
    const res = await publishApi.cancelJob(jobId.value, { running: true })
    if (res.code === 200) {
      ElMessage.success(res.msg || '已中断')
      await refreshJob()
    }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

function goRecords() {
  router.push('/publish-records')
}

/** 刷新页面后如果还有批次在跑，自动接管进度视图（后端内存态，重启就没了） */
async function resumeActiveJob() {
  try {
    const res = await publishApi.listJobs({ active: 1 })
    if (res.code !== 200) return
    const job = (res.data || [])[0]
    if (!job) return
    jobId.value = job.job_id
    await refreshJob()
    startPolling()
    ElMessage.info('已恢复上次未跑完的发布进度')
  } catch (e) {
    /* 拦截器已提示 */
  }
}

onMounted(() => {
  loadMaterials()
  loadAccounts()
  resumeActiveJob()
})

onBeforeUnmount(stopPolling)
</script>

<style scoped>
/* 素材选择表：整行可点（点一下 = 勾选这条素材），所以行要给"能点"的样子，
   否则用户不知道除了小方框还能点别处 */
.material-pick-table {
  :deep(.el-table__row) {
    cursor: pointer;
  }

  /* 勾选框那一格点上去就是翻勾选，指针别跟着变成"点击整行"的样子 */
  :deep(.el-table__row .el-table-column--selection) {
    cursor: default;
  }
}

.publish-center {
  max-width: 1100px;
}
.page-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 16px;
}
.page-header h1 {
  font-size: 20px;
  margin: 0;
}
.mb16 {
  margin-bottom: 16px;
}
.mb12 {
  margin-bottom: 12px;
}
.mb8 {
  margin-bottom: 8px;
}
.mt12 {
  margin-top: 12px;
}
.mt8 {
  margin-top: 8px;
}
.ml8 {
  margin-left: 8px;
}
.mr4 {
  margin-right: 4px;
}
.ml12 {
  margin-left: 12px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.inline-upload {
  display: inline-block;
  margin-left: 8px;
}
.hint {
  color: #909399;
  font-size: 12px;
}
.item-collapse {
  border-top: none;
}
.item-collapse :deep(.el-collapse-item__header) {
  height: auto;
  min-height: 48px;
  padding: 8px 0;
  line-height: 24px;
  align-items: center;
}
.item-collapse :deep(.el-collapse-item__content) {
  padding-bottom: 8px;
}
.item-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex: 1;
  margin-right: 12px;
  gap: 12px;
  flex-wrap: wrap;
}
.item-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}
.item-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0;
  min-width: 0;
}
.item-title .name {
  font-weight: 600;
  margin: 0 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
.item-title .meta {
  color: #909399;
  font-size: 12px;
}
.item-form {
  margin-bottom: -8px;
  padding: 4px 8px 0;
}
.platform-row {
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  margin-bottom: 10px;
}
.platform-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.platform-name {
  font-weight: 600;
  font-size: 14px;
}
/* 平台风险提示：跟在平台名下面一行，克制一点，别把"没有账号"那种 tag 挤变形 */
.platform-notice {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 6px;
  padding: 6px 10px;
  border-radius: 4px;
  background: #fdf6ec;
  border: 1px solid #faecd8;
  color: #b88230;
  font-size: 12px;
  line-height: 1.5;
}
.platform-notice-icon {
  flex: 0 0 auto;
}
.platform-body {
  margin-top: 10px;
  padding-left: 24px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.option-row {
  display: flex;
  align-items: center;
  margin-bottom: 12px;
}
.publish-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.overall {
  margin-bottom: 14px;
}
.stats {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}
.group {
  border: 1px solid #ebeef5;
  border-left: 3px solid #dcdfe6;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.group.is-active {
  border-left-color: #409eff;
  background: #f7fbff;
}
.group-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.group-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.group-title .account {
  font-weight: 600;
}
.spin {
  animation: spin 1.2s linear infinite;
}
@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
.task-detail {
  padding: 8px 12px 4px;
  font-size: 12px;
  color: #606266;
}
.detail-line {
  display: flex;
  gap: 8px;
}
.detail-label {
  color: #909399;
  flex: 0 0 56px;
}
.step-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}
.log {
  max-height: 220px;
  overflow: auto;
  background: #1e1e1e;
  color: #d4d4d4;
  border-radius: 4px;
  padding: 8px 10px;
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
.material {
  font-size: 13px;
}
.action-running {
  color: #409eff;
}
</style>
