<template>
  <div class="publish-records">
    <div class="page-header">
      <h1>发布记录</h1>
      <span class="hint">每一次发布都会留一条记录：账号、平台、时间、状态、标题、素材</span>
    </div>

    <!-- 筛选 -->
    <el-card shadow="never" class="mb16">
      <div class="filters">
        <el-select v-model="filters.source" placeholder="全部来源" clearable style="width: 120px">
          <el-option v-for="s in sourceOptions" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
        <el-select v-model="filters.platform" placeholder="全部平台" clearable style="width: 110px">
          <el-option v-for="p in PLATFORMS" :key="p.key" :label="p.name" :value="p.key" />
        </el-select>
        <el-select v-model="filters.account" placeholder="全部账号" clearable filterable style="width: 140px">
          <el-option
            v-for="a in accountOptions"
            :key="`${a.platform}-${a.name}`"
            :label="a.name"
            :value="a.name"
          />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable style="width: 110px">
          <el-option v-for="s in STATUS_OPTIONS" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          style="width: 240px; --el-date-editor-daterange-width: 240px"
        />
        <el-input
          v-model="filters.keyword"
          placeholder="搜标题 / 素材 / 账号"
          clearable
          style="width: 170px"
          @keyup.enter="reload"
        />
        <el-button type="primary" @click="reload">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
        <div class="filters-right">
          <el-checkbox v-model="autoRefresh" label="自动刷新" />
          <el-button :icon="Refresh" circle @click="load" />
        </div>
      </div>

      <div class="stats">
        <el-tag size="small" effect="plain">共 {{ total }} 条</el-tag>
        <el-tag size="small" type="success" effect="plain">成功 {{ stats.success || 0 }}</el-tag>
        <el-tag size="small" type="danger" effect="plain">失败 {{ stats.failed || 0 }}</el-tag>
        <el-tag v-if="stats.running" size="small" type="primary" effect="plain">发布中 {{ stats.running }}</el-tag>
        <el-tag v-if="stats.queued" size="small" type="info" effect="plain">排队中 {{ stats.queued }}</el-tag>
        <el-tag v-if="stats.cancelled" size="small" type="warning" effect="plain">已取消 {{ stats.cancelled }}</el-tag>
        <el-tag v-if="stats.interrupted" size="small" type="warning" effect="plain">已中断 {{ stats.interrupted }}</el-tag>
      </div>
    </el-card>

    <!-- 记录表 -->
    <el-card shadow="never">
      <el-table v-loading="loading" :data="records" size="small" @row-click="openDetail">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">
            <div>{{ row.created_at }}</div>
            <div class="sub">{{ row.finished_at ? `完成 ${row.finished_at}` : '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="平台" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.platform_name || platformName(row.platform) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="110">
          <template #default="{ row }">
            <el-tag size="small" type="info" effect="plain">
              {{ row.source_text || SOURCE_TEXT[row.source] || '发布中心' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="account_name" label="账号" width="120" show-overflow-tooltip />
        <el-table-column label="标题" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.title || '—' }}</template>
        </el-table-column>
        <el-table-column label="素材" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="ellipsis">{{ row.material_title || row.material }}</div>
            <div v-if="row.material_title && row.material_title !== row.material" class="sub ellipsis">
              {{ row.material }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="statusMeta(row.status).type">
              {{ statusMeta(row.status).text }}
            </el-tag>
            <el-tag v-if="row.scheduled" size="small" type="info" effect="plain" class="ml4">定时</el-tag>
            <el-tag v-if="row.is_draft" size="small" type="info" effect="plain" class="ml4">草稿</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.message || row.action || '—' }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="80">
          <template #default="{ row }">{{ row.duration != null ? `${row.duration}s` : '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
        <!-- 用表格自带的空态槽，别再叠一个 el-empty：会出现"No Data"和自定义文案上下两截 -->
        <template #empty>
          <el-empty :description="hasFilter ? '没有符合条件的记录' : '还没有发布记录'" />
        </template>
      </el-table>

      <el-pagination
        v-if="total > 0"
        class="pager"
        layout="total, sizes, prev, pager, next"
        :total="total"
        :current-page="page"
        :page-size="pageSize"
        :page-sizes="[20, 50, 100]"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="发布详情" size="620px">
      <div v-if="detail" class="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="statusMeta(detail.status).type">
              {{ statusMeta(detail.status).text }}
            </el-tag>
            <span v-if="detail.schedule_at" class="ml8">定时 {{ detail.schedule_at }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="平台">{{ detail.platform_name || platformName(detail.platform) }}</el-descriptions-item>
          <el-descriptions-item label="来源">
            {{ detail.source_text || SOURCE_TEXT[detail.source] || '发布中心' }}
          </el-descriptions-item>
          <el-descriptions-item label="账号">{{ detail.account_name }}</el-descriptions-item>
          <el-descriptions-item label="标题">{{ detail.title || '—' }}</el-descriptions-item>
          <el-descriptions-item label="正文">
            <div class="multiline">{{ detail.desc || '—' }}</div>
          </el-descriptions-item>
          <el-descriptions-item label="话题">
            <el-tag v-for="t in detail.tags || []" :key="t" size="small" effect="plain" class="mr4">
              #{{ t }}
            </el-tag>
            <span v-if="!(detail.tags || []).length">—</span>
          </el-descriptions-item>
          <el-descriptions-item label="素材">
            <div>{{ detail.material_title || detail.material }}</div>
            <div v-if="detail.material_title && detail.material_title !== detail.material" class="sub">
              磁盘名：{{ detail.material }}
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="时间">
            <div>提交 {{ detail.created_at || '—' }}</div>
            <div>开始 {{ detail.started_at || '—' }} · 完成 {{ detail.finished_at || '—' }}</div>
            <div v-if="detail.duration != null">耗时 {{ detail.duration }} 秒</div>
          </el-descriptions-item>
          <el-descriptions-item label="说明">{{ detail.message || '—' }}</el-descriptions-item>
        </el-descriptions>

        <template v-if="(detail.steps || []).length">
          <el-divider>执行步骤</el-divider>
          <div v-for="(s, i) in detail.steps" :key="i" class="step-line">
            <el-tag size="small" :type="stepTagType(s.status)" effect="plain">{{ s.step }}</el-tag>
            <span>{{ s.message }}</span>
          </div>
        </template>

        <template v-if="(detail.log || []).length">
          <el-divider>执行日志</el-divider>
          <pre class="log">{{ formatLog(detail.log) }}</pre>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { PLATFORMS, SOURCE_TEXT, platformName, publishApi, statusMeta } from '@/api/publish'

const STATUS_OPTIONS = [
  { label: '成功', value: 'success' },
  { label: '失败', value: 'failed' },
  { label: '发布中', value: 'running' },
  { label: '排队中', value: 'queued' },
  { label: '已取消', value: 'cancelled' },
  { label: '已中断', value: 'interrupted' }
]

const loading = ref(false)
const records = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const stats = ref({})
const accountOptions = ref([])
const autoRefresh = ref(false)

const filters = reactive({
  source: '',
  platform: '',
  account: '',
  status: '',
  keyword: ''
})
const dateRange = ref([])
const sourceOptions = ref([])

const detailVisible = ref(false)
const detail = ref(null)

const hasFilter = computed(
  () =>
    !!(filters.source || filters.platform || filters.account || filters.status || filters.keyword.trim()) ||
    (dateRange.value?.length || 0) > 0
)

let timer = null

function stepTagType(status) {
  if (status === 'ok') return 'success'
  if (status === 'failed') return 'danger'
  return 'warning'
}

function formatLog(lines) {
  return (lines || []).map((line) => `${line.t || ''}  ${line.text}`).join('\n')
}

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.source) params.source = filters.source
    if (filters.platform) params.platform = filters.platform
    if (filters.account) params.account = filters.account
    if (filters.status) params.status = filters.status
    if (filters.keyword.trim()) params.keyword = filters.keyword.trim()
    if (dateRange.value?.length === 2) {
      params.start = dateRange.value[0]
      params.end = dateRange.value[1]
    }
    const res = await publishApi.listRecords(params)
    if (res.code === 200) {
      records.value = res.data.items || []
      total.value = res.data.total || 0
      stats.value = res.data.stats || {}
      if ((res.data.accounts || []).length) accountOptions.value = res.data.accounts
      if ((res.data.sources || []).length) sourceOptions.value = res.data.sources
    }
  } catch (e) {
    /* 拦截器已提示 */
  } finally {
    loading.value = false
  }
}

function reload() {
  page.value = 1
  load()
}

function resetFilters() {
  filters.source = ''
  filters.platform = ''
  filters.account = ''
  filters.status = ''
  filters.keyword = ''
  dateRange.value = []
  reload()
}

function onPageChange(value) {
  page.value = value
  load()
}

function onSizeChange(value) {
  pageSize.value = value
  page.value = 1
  load()
}

async function openDetail(row) {
  try {
    const res = await publishApi.getRecord(row.id)
    if (res.code === 200) {
      detail.value = res.data
      detailVisible.value = true
    }
  } catch (e) {
    /* 拦截器已提示 */
  }
}

/** 记录页是只读的，但发布中的记录状态会自己往前走，开个轻量自动刷新更顺手 */
const refreshInterval = computed(() => (autoRefresh.value ? 3000 : 0))

watch(refreshInterval, (value) => {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  if (value) timer = setInterval(load, value)
})

onMounted(load)
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.publish-records {
  max-width: 1400px;
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
.hint {
  color: #909399;
  font-size: 12px;
}
.sub {
  color: #909399;
  font-size: 12px;
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mb16 {
  margin-bottom: 16px;
}
.ml4 {
  margin-left: 4px;
}
.ml8 {
  margin-left: 8px;
}
.mr4 {
  margin-right: 4px;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.filters-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.stats {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
.detail {
  font-size: 13px;
}
.multiline {
  white-space: pre-wrap;
  word-break: break-word;
}
.step-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 12px;
}
.log {
  max-height: 320px;
  overflow: auto;
  background: #1e1e1e;
  color: #d4d4d4;
  border-radius: 4px;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
