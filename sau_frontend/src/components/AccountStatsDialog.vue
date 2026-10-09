<template>
  <el-dialog
    v-model="visible"
    :title="dialogTitle"
    width="880px"
    top="6vh"
    class="account-stats-dialog"
    :close-on-click-modal="false"
    append-to-body
    @open="handleOpen"
  >
    <!-- 顶部：账号是谁 + 数据是什么时候的 + 刷新 -->
    <div class="stats-header">
      <!-- 这里用原生 <img> 而不是 el-avatar：**el-avatar 不接受 referrerpolicy**
           （它只把这个属性当普通 attribute 透传，不会落到内部那个 <img> 上），
           于是浏览器照样带 Referer，小红书图床照样 403。原生 img 才吃这个属性。 -->
      <img :src="avatar" alt="" class="stats-avatar" referrerpolicy="no-referrer" />
      <div class="stats-identity">
        <div class="stats-name-line">
          <span class="stats-name">{{ displayName }}</span>
          <el-tag :type="platformTagType" effect="plain" size="small">{{ platformName }}</el-tag>
          <el-tag v-if="stats && accountId" effect="plain" size="small" type="info">
            ID {{ accountId }}
          </el-tag>
        </div>
        <div class="stats-desc">{{ description || '—' }}</div>
      </div>
      <div class="stats-header-right">
        <div class="stats-fetched">
          <span v-if="headerMetaText" :class="{ 'is-stale': isStale }">{{ headerMetaText }}</span>
        </div>
        <!-- 「同步」= 明确的写库动作：拉平台实时数据覆盖本地库，不看缓存 TTL。
             「刷新」只重读本地库，用来放弃本地改动、看回已存的那份。 -->
        <el-button
          size="small"
          type="primary"
          :icon="Refresh"
          :loading="syncing"
          :disabled="loading"
          @click="handleSync"
        >
          {{ syncing ? '同步中' : '同步' }}
        </el-button>
        <el-button
          size="small"
          :icon="RefreshRight"
          :loading="loading"
          :disabled="syncing"
          title="只看本地已存的数据，不重新抓取"
          @click="load()"
        >
          刷新
        </el-button>
      </div>
    </div>

    <!-- 说明条：平台不支持 / 抓取失败 / 数据过期，三种都会在这里说清楚 -->
    <el-alert
      v-if="notice"
      class="stats-notice"
      :type="notice.type"
      :title="notice.title"
      :description="notice.description"
      :closable="false"
      show-icon
    />

    <!-- 第一次抓取：明确告诉用户在等什么，否则十几秒的白屏会让人以为卡死了 -->
    <div v-if="loading" class="stats-loading">
      <el-icon class="is-loading"><Loading /></el-icon>
      <p>正在从{{ platformName }}读取数据…</p>
      <p class="stats-loading-tip">
        第一次读取需要打开一次平台页面，通常 5~15 秒；读完会存起来，之后再打开就很快。
      </p>
    </div>

    <template v-else-if="stats">
      <!-- 概览卡片 -->
      <div class="stat-cards">
        <div v-for="card in overviewCards" :key="card.label" class="stat-card">
          <div class="stat-card-value">{{ card.value }}</div>
          <div class="stat-card-label">{{ card.label }}</div>
          <div v-if="card.hint" class="stat-card-hint">{{ card.hint }}</div>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="stats-tabs">
        <!-- ---------------- 粉丝数据 ---------------- -->
        <el-tab-pane label="粉丝数据" name="fans">
          <div v-if="!hasFansData" class="stats-empty">
            {{ platformName }}这次没有返回粉丝维度的数据。
          </div>
          <template v-else>
            <!-- 成长进度：小红书给了"当前粉丝 / 下一档门槛"，有就画出来 -->
            <div v-if="growth" class="growth-block">
              <div class="growth-head">
                <span>成长进度</span>
                <span class="growth-value">
                  {{ formatNumber(growth.fans_count) }} / {{ formatNumber(growth.max_fans_count) }}
                </span>
              </div>
              <el-progress
                :percentage="growthPercent"
                :stroke-width="10"
                :show-text="false"
                color="#ff6b81"
              />
              <div class="growth-tip">再涨 {{ formatNumber(growthRemaining) }} 粉进入下一档</div>
            </div>

            <!-- 逐日涨粉趋势 -->
            <div v-if="fanSeriesBlocks.length" class="series-grid">
              <div v-for="block in fanSeriesBlocks" :key="block.label" class="series-card">
                <div class="series-title">
                  <span>{{ block.label }}</span>
                  <span class="series-total">{{ block.total }}</span>
                </div>
                <Sparkline :data="block.data" :color="block.color" unit="个" />
              </div>
            </div>

            <!-- 各周期涨粉数字 -->
            <div v-if="fansPeriodSections.length" class="period-sections">
              <div v-for="section in fansPeriodSections" :key="section.period" class="period-section">
                <div class="period-title">{{ section.label }}</div>
                <div class="metric-grid">
                  <div v-for="row in section.rows" :key="row.field" class="metric-cell">
                    <div class="metric-label">{{ row.label }}</div>
                    <div class="metric-value" :class="row.tone">{{ row.display }}</div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </el-tab-pane>

        <!-- ---------------- 互动数据 ---------------- -->
        <el-tab-pane label="互动数据" name="engagement">
          <div v-if="!engagementPeriods.length" class="stats-empty">
            {{ platformName }}这次没有返回互动数据。
          </div>
          <template v-else>
            <div v-if="engagementPeriods.length > 1" class="period-switch">
              <el-radio-group v-model="engagementPeriod" size="small">
                <el-radio-button
                  v-for="period in engagementPeriods"
                  :key="period.key"
                  :value="period.key"
                >
                  {{ period.label }}
                </el-radio-button>
              </el-radio-group>
            </div>

            <el-table :data="engagementRows" class="metric-table" :show-header="false" size="small">
              <el-table-column label="指标" width="110">
                <template #default="scope">
                  <span class="metric-name">{{ scope.row.label }}</span>
                </template>
              </el-table-column>
              <el-table-column label="趋势" min-width="150">
                <template #default="scope">
                  <Sparkline
                    v-if="scope.row.series && scope.row.series.length > 1"
                    :data="scope.row.series"
                    :color="scope.row.color"
                    :height="40"
                  />
                  <span v-else class="no-series">—</span>
                </template>
              </el-table-column>
              <el-table-column label="数值" width="120" align="right">
                <template #default="scope">
                  <span class="metric-value">{{ scope.row.display }}</span>
                </template>
              </el-table-column>
              <el-table-column label="环比" width="110" align="right">
                <template #default="scope">
                  <span v-if="scope.row.rate !== null" class="rate" :class="scope.row.rateTone">
                    {{ formatRate(scope.row.rate) }}
                  </span>
                  <span v-else class="no-series">—</span>
                </template>
              </el-table-column>
            </el-table>

            <p v-if="engagementSummary" class="metric-summary">
              {{ engagementSummary }}
            </p>
          </template>
        </el-tab-pane>

        <!-- ---------------- 作品数据 ---------------- -->
        <el-tab-pane :label="`作品数据${mediaTotalText}`" name="media">
          <div v-if="!mediaItems.length" class="stats-empty">
            {{
              mediaSupported
                ? '这个账号还没有已发布的作品。'
                : '这次没有取到作品列表' + (mediaHint ? '：' + mediaHint : '。')
            }}
          </div>
          <template v-else>
            <el-table :data="mediaItems" class="media-table" size="small" max-height="420">
              <el-table-column label="作品" min-width="260">
                <template #default="scope">
                  <div class="media-cell">
                    <el-image
                      v-if="scope.row.cover"
                      :src="scope.row.cover"
                      fit="cover"
                      class="media-cover"
                      :preview-src-list="[scope.row.cover]"
                      preview-teleported
                      referrerpolicy="no-referrer"
                    >
                      <template #error>
                        <div class="media-cover-fallback"><el-icon><VideoCamera /></el-icon></div>
                      </template>
                    </el-image>
                    <div v-else class="media-cover media-cover-fallback">
                      <el-icon><VideoCamera /></el-icon>
                    </div>
                    <div class="media-info">
                      <div class="media-title" :title="scope.row.title">{{ scope.row.title }}</div>
                      <div class="media-meta">
                        <span>{{ scope.row.published_at || '—' }}</span>
                        <span v-if="scope.row.duration">{{ scope.row.duration }} 秒</span>
                        <a
                          v-if="scope.row.url"
                          :href="scope.row.url"
                          target="_blank"
                          rel="noopener noreferrer"
                          class="media-link"
                        >打开</a>
                      </div>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column
                v-for="col in mediaColumns"
                :key="col.field"
                :label="col.label"
                width="88"
                align="right"
              >
                <template #default="scope">
                  <span class="metric-value">{{ formatNumber(scope.row[col.field]) }}</span>
                </template>
              </el-table-column>
            </el-table>

            <p v-if="mediaFooterText" class="media-footer">{{ mediaFooterText }}</p>
          </template>
        </el-tab-pane>

        <!-- ---------------- 平台说明 ---------------- -->
        <el-tab-pane v-if="stats.notes && stats.notes.length" label="说明" name="notes">
          <ul class="notes-list">
            <li v-for="(note, index) in stats.notes" :key="index">{{ note }}</li>
          </ul>
        </el-tab-pane>
      </el-tabs>
    </template>

    <!-- 拿不到数据，而且一条缓存都没有 -->
    <div v-else class="stats-empty stats-empty-block">
      <el-empty :description="emptyDescription" />
    </div>

    <template #footer>
      <span class="dialog-footer">
        <span v-if="stats" class="footer-note">
          数据来源：{{ platformName }}{{ accountId ? ' · ' + accountId : '' }}
        </span>
        <el-button @click="visible = false">关闭</el-button>
      </span>
    </template>
  </el-dialog>
</template>

<script setup>
/**
 * 账号详情弹窗（at-ai-xhs 改造新增）。
 *
 * 「账号管理」页操作栏的「详情」按钮打开它，展示**平台侧的粉丝 / 互动 / 作品数据**
 * （抓取实现见后端 `myUtils/account_stats/`，接口见 `sau_backend_stats.py`）。
 *
 * 几个刻意的设计决定：
 *
 * 1. **不做成表格里的新列，而是一个弹窗。** 这四个指标（粉丝 / 关注 / 获赞 / 作品）
 *    塞进账号表会让表宽到需要横向滚动，而用户真正想看的是"这个账号最近怎么样"，
 *    那是一屏的信息量，不是一列。
 * 2. **数据不新也照常显示**，只在上方标一行"数据是 X 前的"。理由：抓一次要开浏览器，
 *    拿不到最新的就把旧数据藏起来，用户只会以为工具坏了。想更新点「同步」。
 *    本地库的内容由三处维护（会话续期定时任务、「同步」按钮、账号刚加进来那次），
 *    见 `myUtils/account_stats/sync.py`。
 * 3. **只画平台真的给了的指标。** 快手没有 30 天口径、没有作品级分享数，视频号可能
 *    连粉丝数都没有——这些情况下**少显示一行**，而不是显示 0。0 和"平台没给"
 *    在读者眼里完全是两回事，混起来会让人误判账号数据。
 * 4. **必须写 `append-to-body`（这不是可选项）。** Element Plus 2.14 只在
 *    `appendToBody` 为真时才把弹窗 teleport 到 `body`，否则**原地渲染**。
 *    而这个组件的挂载点（`.account-stats-trigger`）在一个 `<td>` 里，而
 *    Element Plus 的表格单元格是 `position: relative; z-index: 1`——
 *    于是 `.el-overlay`（z-index 2015）被关进那个单元格的层叠上下文里，
 *    **盖不住后面的单元格**，表现就是"列表按钮浮在弹窗上面、表格内容透过蒙层露出来"
 *    （实测踩过，而且只有表格里的弹窗会这样）。`App.vue` 里那个弹窗同样带了这个属性，
 *    原因一样。
 * 5. **远程图片要带 `referrerpolicy="no-referrer"`。** 小红书的图床
 *    （`img.xiaohongshu.com` / `sns-*.xhscdn.com`）**带 Referer 就回 403**，不带才给图
 *    （实测：带 Referer 403、不带 200）。默认情况下浏览器会带上本页地址，于是头像和
 *    作品封面全是裂图。头像走 `el-avatar` 的 `referrerpolicy`，封面走 `el-image` 的
 *    同名属性（它会透传到内部的 `<img>` 上）。
 */
import { computed, ref, watch } from 'vue'
import { Loading, Refresh, RefreshRight, VideoCamera } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import Sparkline from './Sparkline.vue'
import { accountStatsApi } from '@/api/accountStats'

const props = defineProps({
  /** `{ id, name, platform, platformType }`，来自账号列表那一行 */
  account: { type: Object, default: null },
  modelValue: { type: Boolean, default: false }
})

const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const stats = ref(null)
const payload = ref(null)
const loading = ref(false)
const syncing = ref(false)
const activeTab = ref('fans')
const engagementPeriod = ref('7d')

const platformName = computed(
  () => (props.account && props.account.platform) || (stats.value && stats.value.account.platform_name) || '平台'
)

const platformTagType = computed(() => {
  const map = { 小红书: 'info', 视频号: 'warning', 抖音: 'danger', 快手: 'success', 拼多多视频: 'primary' }
  return map[platformName.value] || 'info'
})

const displayName = computed(
  () =>
    (stats.value && stats.value.account && (stats.value.account.nickname || stats.value.account.name)) ||
    (props.account && props.account.name) ||
    '账号详情'
)

const avatar = computed(() => {
  const fromStats = stats.value && stats.value.account && stats.value.account.avatar
  if (fromStats) return fromStats
  const name = (props.account && props.account.name) || ''
  return `https://ui-avatars.com/api/?name=${encodeURIComponent(name)}&background=random`
})

const accountId = computed(() => stats.value && stats.value.account && stats.value.account.account_id)

const dialogTitle = computed(() => '账号数据 · 查看详情')

// --------------------------------------------------------------------- //
// 打开 / 加载
// --------------------------------------------------------------------- //

// `immediate` **不能开**：Vue 在 immediate 那次会把 oldValue 传成 `undefined`，
// 而这里要对它做数组解构（`[wasOpen, prevId]`），直接就是
// "undefined is not iterable" —— 组件一渲染整个页面就崩（实测踩过）。
// 所以首屏那一次加载走 `@open`（见 handleOpen），这个 watch 只管"打开状态/账号变了"。
watch(
  () => [props.modelValue, props.account && props.account.id],
  (next, prev) => {
    const [open, id] = next || []
    const [wasOpen = false, prevId = null] = prev || []
    if (!open) return
    // 换了账号就必须重取：同一个弹窗会被不同行复用，不重取会显示上一行的数据。
    if (id !== prevId || !wasOpen) {
      stats.value = null
      payload.value = null
      activeTab.value = 'fans'
      load()
    }
  }
)

async function handleOpen() {
  // 弹窗第一次打开时 watch 可能还没跑（异步组件挂载晚于 visible 变化），
  // 这里兜一次；已经有数据或正在加载就不重复请求。
  if (!stats.value && !loading.value) await load()
}

async function load(options = {}) {
  const id = props.account && props.account.id
  if (!id || loading.value) return
  loading.value = true
  try {
    const response = await accountStatsApi.getStats(id, options)
    applyPayload(response.data || {})
  } catch (error) {
    // 拦截器已经弹过提示了，这里只需要让弹窗显示成"没有数据"
    console.error('获取账号数据失败:', error)
    payload.value = { error: '读取账号数据失败，请稍后重试。' }
    stats.value = null
  } finally {
    loading.value = false
  }
}

/** 把接口返回的信封塞进组件状态（详情和同步两个接口的信封形状是一样的）。 */
function applyPayload(data) {
  payload.value = data
  stats.value = data.stats || null
  // 默认周期：优先 30 天（区间长、数字更有参考性），没有就退回 7 天
  const periods = (data.stats && data.stats.engagement && data.stats.engagement.periods) || {}
  engagementPeriod.value = '30d' in periods ? '30d' : ('7d' in periods ? '7d' : '7d')
}

/**
 * 「同步」：拉平台实时数据**覆盖本地库**。
 *
 * 和「刷新」分开，是因为这两件事对用户的含义完全不同：
 *   * 同步 = 去平台拿最新的，会覆盖本地（慢，几秒到十几秒）；
 *   * 刷新 = 只看本地已存的那份（瞬间）。
 * 之前只有一个「刷新」，语义上分不清这两种，用户点了之后不知道到底动没动平台数据。
 *
 * 提示的语气按 `sync.status` 分三档，别把"平台没登录"说成"同步失败"——
 * 前者用户自己扫码就能解决，后者才需要查日志。
 */
async function handleSync() {
  const id = props.account && props.account.id
  if (!id || syncing.value) return
  syncing.value = true
  try {
    const response = await accountStatsApi.sync(id)
    const data = response.data || {}
    applyPayload(data)
    const sync = data.sync || {}
    if (sync.status === 'ok') {
      ElMessage.success(sync.message || '数据已同步')
    } else if (sync.status === 'invalid') {
      ElMessage.warning(sync.message || '平台暂时拿不到数据')
    } else if (sync.status === 'busy') {
      ElMessage.info(sync.message || '账号正忙，稍后再试')
    } else {
      ElMessage.error(sync.message || '同步失败')
    }
  } catch (error) {
    // 400/401 之类由拦截器提示；这里保证按钮能恢复
    console.error('同步账号数据失败:', error)
  } finally {
    syncing.value = false
  }
}

// --------------------------------------------------------------------- //
// 顶部状态
// --------------------------------------------------------------------- //

const fetchedAt = computed(() => payload.value && payload.value.fetched_at)
const ageSeconds = computed(() => payload.value && payload.value.age_seconds)
const isStale = computed(() => Boolean(payload.value && payload.value.stale))

const headerMetaText = computed(() => {
  if (!fetchedAt.value) return ''
  const age = ageSeconds.value
  if (age === null || age === undefined) return `数据时间：${fetchedAt.value}`
  if (age < 60) return '刚刚更新'
  if (age < 3600) return `${Math.round(age / 60)} 分钟前更新`
  if (age < 86400) return `${Math.round(age / 3600)} 小时前更新`
  return `${Math.round(age / 86400)} 天前更新`
})

const notice = computed(() => {
  const data = payload.value
  if (!data) return null
  if (data.unsupported) {
    return {
      type: 'info',
      title: '这个平台暂时拿不到数据',
      description: data.error || '平台没有提供可用的数据接口。'
    }
  }
  if (data.error && data.stats) {
    return {
      type: 'warning',
      title: '这次没能更新',
      description: `${data.error}下面显示的是上次读取到的数据。`
    }
  }
  if (data.error && !data.stats) {
    return { type: 'error', title: '没有取到数据', description: data.error }
  }
  if (isStale.value) {
    return {
      type: 'info',
      title: '数据不是最新的',
      description: `这是 ${fetchedAt.value || '上次'} 同步到的数据，点右上角「同步」可以重新拉取最新的。`
    }
  }
  return null
})

const emptyDescription = computed(() => {
  const data = payload.value
  if (!data) return '暂无数据'
  if (data.error) return data.error
  return `${platformName.value}没有返回这个账号的数据`
})

// --------------------------------------------------------------------- //
// 概览卡片
// --------------------------------------------------------------------- //

const fans = computed(() => (stats.value && stats.value.fans) || {})
const media = computed(() => (stats.value && stats.value.media) || {})
const extra = computed(() => (stats.value && stats.value.extra) || {})
const description = computed(() => extra.value.desc || '')

const overviewCards = computed(() => {
  if (!stats.value) return []
  const cards = []
  const push = (label, value, hint) => {
    if (value === null || value === undefined) return
    cards.push({ label, value: formatNumber(value), hint })
  }
  push('粉丝数', fans.value.count)
  push('关注数', fans.value.following_count)
  push('获赞与收藏', fans.value.likes_total)
  // 作品数：抖音有独立的 `aweme_count`；其它平台用作品列表的总数兜底
  push('作品数', extra.value.aweme_count !== undefined ? extra.value.aweme_count : media.value.total)
  return cards
})

// --------------------------------------------------------------------- //
// 粉丝数据
// --------------------------------------------------------------------- //

const periodLabels = { '7d': '近 7 天', '30d': '近 30 天' }
const fanPeriods = computed(() => fans.value.periods || {})

const hasFansData = computed(
  () => Object.keys(fanPeriods.value).length > 0 || fans.value.count !== undefined
)

const growth = computed(() => {
  const info = fans.value.growth
  if (!info || info.max_fans_count === null || info.max_fans_count === undefined) return null
  return info
})

const growthPercent = computed(() => {
  const info = growth.value
  if (!info) return 0
  const max = Number(info.max_fans_count) || 0
  if (max <= 0) return 0
  return Math.max(0, Math.min(100, Math.round((Number(info.fans_count) || 0) / max * 100)))
})

const growthRemaining = computed(() => {
  const info = growth.value
  if (!info) return 0
  return Math.max(0, (Number(info.max_fans_count) || 0) - (Number(info.fans_count) || 0))
})

/** 逐日涨粉趋势。优先用 30 天（更长更有信息量），没有就退回 7 天。 */
const fanSeriesBlocks = computed(() => {
  const source = fanPeriods.value['30d'] || fanPeriods.value['7d']
  if (!source) return []
  const blocks = [
    { label: '每日净涨粉', key: 'net_series', color: '#67c23a' },
    { label: '每日新增粉丝', key: 'rise_series', color: '#409eff' },
    { label: '每日流失粉丝', key: 'loss_series', color: '#f56c6c' }
  ]
  return blocks
    .map((block) => {
      const data = source[block.key] || []
      // 全 0 的序列（新账号常见）画出来是一条贴底的直线，没有信息量，不如不画
      const total = data.reduce((sum, row) => sum + (Number(row.count) || 0), 0)
      if (data.length < 2 || total === 0) return null
      return {
        label: block.label,
        color: block.color,
        data,
        total: `共 ${formatNumber(total)} 个`
      }
    })
    .filter(Boolean)
})

/** 各周期的涨粉数字 */
const fansPeriodSections = computed(() => {
  const sections = []
  Object.keys(fanPeriods.value).forEach((period) => {
    const block = fanPeriods.value[period] || {}
    const rows = []
    const addRow = (field, label, tone, display) => {
      if (display === undefined) return
      rows.push({ field, label, tone, display })
    }
    addRow('net', '净涨粉', 'is-positive', formatDelta(block.net))
    addRow('gained', '新增粉丝', 'is-positive', formatDelta(block.gained))
    addRow('lost', '流失粉丝', block.lost ? 'is-negative' : 'is-muted', formatDelta(block.lost))
    if (rows.length) {
      sections.push({ period, label: periodLabels[period] || period, rows })
    }
  })
  return sections
})

// --------------------------------------------------------------------- //
// 互动数据
// --------------------------------------------------------------------- //

const engagement = computed(() => (stats.value && stats.value.engagement) || {})
const engagementPeriodsMap = computed(() => engagement.value.periods || {})

const engagementPeriods = computed(() =>
  Object.keys(engagementPeriodsMap.value).map((key) => ({
    key,
    label: periodLabels[key] || key
  }))
)

/**
 * 指标的展示顺序与文案。
 *
 * 顺序按"用户最关心"排：先看播放，再看点赞收藏，最后是发布数量这类过程指标。
 * 小红书和抖音给了一个平台侧的结论 `summary`（带 `<font>` 标签），单独渲染。
 */
const METRIC_META = {
  view_count: { label: '播放量', color: '#409eff' },
  like_count: { label: '点赞', color: '#f56c6c' },
  collect_count: { label: '收藏', color: '#e6a23c' },
  comment_count: { label: '评论', color: '#909399' },
  share_count: { label: '分享', color: '#67c23a' },
  rise_fans_count: { label: '涨粉', color: '#67c23a' },
  loss_fans_count: { label: '掉粉', color: '#f56c6c' },
  net_rise_fans_count: { label: '净涨粉', color: '#67c23a' },
  profile_view_count: { label: '主页访问', color: '#909399' },
  search_view_count: { label: '搜索带来播放', color: '#909399' },
  impl_count: { label: '曝光量', color: '#a8abb2' },
  home_view_count: { label: '首页推荐曝光', color: '#a8abb2' },
  danmaku_count: { label: '弹幕', color: '#a8abb2' },
  publish_note_num: { label: '发布笔记数', color: '#a8abb2' },
  publish_video_note_num: { label: '发布视频数', color: '#a8abb2' },
  avg_view_time: { label: '人均观看时长', color: '#909399', suffix: ' 秒' },
  video_full_view_rate: { label: '完播率', color: '#e6a23c', percent: true },
  cover_click_rate: { label: '封面点击率', color: '#e6a23c', percent: true }
}

const currentEngagement = computed(() => engagementPeriodsMap.value[engagementPeriod.value] || {})

const engagementRows = computed(() => {
  const block = currentEngagement.value
  const rates = block.rates || {}
  const seriesMap = block.series || {}
  const rows = []
  Object.keys(METRIC_META).forEach((field) => {
    if (block[field] === undefined || block[field] === null) return
    const meta = METRIC_META[field]
    const rate = rates[field] === undefined || rates[field] === null ? null : Number(rates[field])
    const rawSeries = seriesMap[field] || []
    // 30 天的序列有 30 个点，塞进这一列会挤成一根实心条，只画最近 14 天
    const series = rawSeries.length > 14 ? rawSeries.slice(-14) : rawSeries
    rows.push({
      field,
      label: meta.label,
      color: meta.color,
      display: meta.percent
        ? `${formatCompact(block[field])}%`
        : `${formatNumber(block[field])}${meta.suffix || ''}`,
      series,
      rate,
      rateTone: rate === null ? '' : rate > 0 ? 'is-positive' : rate < 0 ? 'is-negative' : 'is-muted'
    })
  })
  return rows
})

const engagementSummary = computed(() => {
  const summary = currentEngagement.value.summary
  if (!summary) return ''
  // 平台给的结论文案里带 `<font color="red">` 这类标签，当纯文本渲染前先剥掉——
  // 用 v-html 渲染平台返回的 HTML 是有风险的，而这点样式本来也不值得冒这个险。
  return String(summary)
    .replace(/<[^>]+>/g, '')
    .replace(/\s+/g, ' ')
    .trim()
})

// --------------------------------------------------------------------- //
// 作品数据
// --------------------------------------------------------------------- //

const mediaSupported = computed(() => Boolean(media.value.supported))
const mediaItems = computed(() => media.value.items || [])
const mediaHint = computed(() => media.value.error || '')

const mediaTotalText = computed(() => {
  const total = media.value.total
  if (!total) return ''
  return `（${total}）`
})

/**
 * 作品表格的列。
 *
 * **只显示平台真的给了的列**：快手没有作品级分享数（实测翻遍响应都没有这个字段），
 * 硬画一列会全是空的；反过来，快手和抖音有"这条作品带来几个粉丝"、小红书没有，
 * 那就只在有的平台上显示「吸粉」。
 */
const mediaColumns = computed(() => {
  const has = (field) => mediaItems.value.some((row) => row[field] !== null && row[field] !== undefined)
  const columns = [
    { field: 'views', label: '播放' },
    { field: 'likes', label: '点赞' },
    { field: 'collects', label: '收藏' },
    { field: 'comments', label: '评论' },
    { field: 'shares', label: '分享' },
    { field: 'rise_fans', label: '吸粉' },
    { field: 'full_view_rate', label: '完播率' }
  ]
  return columns.filter((column) => has(column.field))
})

const mediaFooterText = computed(() => {
  if (!mediaItems.value.length) return ''
  const total = media.value.total
  if (media.value.has_more && total) {
    return `共 ${total} 条作品，这里显示最近 ${mediaItems.value.length} 条。`
  }
  const notes = (stats.value && stats.value.notes) || []
  return notes.find((note) => note.includes('作品')) || ''
})

// --------------------------------------------------------------------- //
// 格式化
// --------------------------------------------------------------------- //

function formatNumber(value) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  if (Number.isInteger(number)) return number.toLocaleString('zh-CN')
  return number.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

/** 大数字压缩成"1.2万"，用于卡片这种位置有限的地方 */
function formatCompact(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`
  return formatNumber(number)
}

/** 涨粉这类数字要带正负号——"+16" 和 "16" 在"涨粉"语境下意思不同 */
function formatDelta(value) {
  if (value === null || value === undefined) return undefined
  const number = Number(value)
  if (!Number.isFinite(number)) return undefined
  const sign = number > 0 ? '+' : ''
  return `${sign}${formatNumber(number)}`
}

function formatRate(rate) {
  if (rate === null || rate === undefined) return '—'
  const number = Number(rate)
  if (!Number.isFinite(number)) return '—'
  const sign = number > 0 ? '+' : ''
  return `${sign}${number.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}%`
}
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.stats-header {
  display: flex;
  align-items: center;
  gap: 14px;
  padding-bottom: 14px;
  border-bottom: 1px solid $border-lighter;
}

.stats-avatar {
  flex-shrink: 0;
  width: 52px;
  height: 52px;
  border-radius: 50%;
  object-fit: cover;
  background: #f5f7fa;
  border: 1px solid $border-lighter;
}

.stats-identity {
  flex: 1;
  min-width: 0;
}

.stats-name-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.stats-name {
  font-size: 17px;
  font-weight: 600;
  color: $text-primary;
}

.stats-desc {
  margin-top: 4px;
  font-size: 12px;
  color: $text-secondary;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stats-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.stats-fetched {
  font-size: 12px;
  color: $text-secondary;

  .is-stale {
    color: $warning-color;
  }
}

.stats-notice {
  margin-top: 12px;
}

.stats-loading {
  padding: 48px 0;
  text-align: center;
  color: $text-secondary;

  .el-icon {
    font-size: 30px;
    color: $primary-color;
  }

  p {
    margin: 12px 0 0;
    font-size: 14px;
  }

  .stats-loading-tip {
    font-size: 12px;
    color: $text-placeholder;
    max-width: 460px;
    margin: 6px auto 0;
    line-height: 1.6;
  }
}

.stat-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin-top: 16px;
}

.stat-card {
  background: linear-gradient(180deg, #fafbfc 0%, #f5f7fa 100%);
  border: 1px solid $border-lighter;
  border-radius: 6px;
  padding: 12px 14px;
  text-align: center;
}

.stat-card-value {
  font-size: 22px;
  font-weight: 600;
  color: $text-primary;
  line-height: 1.3;
}

.stat-card-label {
  margin-top: 2px;
  font-size: 12px;
  color: $text-secondary;
}

.stat-card-hint {
  margin-top: 2px;
  font-size: 11px;
  color: $text-placeholder;
}

.stats-tabs {
  margin-top: 8px;
}

.growth-block {
  background: #fff8f9;
  border: 1px solid #ffe0e5;
  border-radius: 6px;
  padding: 12px 14px;
  margin-bottom: 16px;
}

.growth-head {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: $text-regular;
  margin-bottom: 8px;
}

.growth-value {
  font-weight: 600;
  color: #ff6b81;
}

.growth-tip {
  margin-top: 6px;
  font-size: 12px;
  color: $text-secondary;
}

.series-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.series-card {
  border: 1px solid $border-lighter;
  border-radius: 6px;
  padding: 10px 12px;
}

.series-title {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: $text-regular;
  margin-bottom: 4px;
}

.series-total {
  color: $text-secondary;
}

.period-sections {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.period-title {
  font-size: 13px;
  font-weight: 600;
  color: $text-regular;
  margin-bottom: 8px;
  padding-left: 8px;
  border-left: 3px solid $primary-color;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px;
}

.metric-cell {
  border: 1px solid $border-lighter;
  border-radius: 6px;
  padding: 10px 12px;
}

.metric-label {
  font-size: 12px;
  color: $text-secondary;
}

.metric-value {
  font-size: 16px;
  font-weight: 600;
  color: $text-primary;
  margin-top: 2px;

  &.is-positive {
    color: $success-color;
  }

  &.is-negative {
    color: $danger-color;
  }

  &.is-muted {
    color: $text-secondary;
  }
}

.period-switch {
  margin-bottom: 10px;
}

.metric-table {
  :deep(.el-table__row) {
    td {
      padding: 4px 0;
    }
  }
}

.metric-name {
  font-size: 13px;
  color: $text-regular;
}

.no-series {
  color: $text-placeholder;
}

.rate {
  font-size: 13px;

  &.is-positive {
    color: $success-color;
  }

  &.is-negative {
    color: $danger-color;
  }

  &.is-muted {
    color: $text-secondary;
  }
}

.metric-summary {
  margin: 12px 0 0;
  padding: 10px 12px;
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.7;
  color: $text-regular;
}

.media-cell {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 2px 0;
}

.media-cover {
  width: 48px;
  height: 64px;
  border-radius: 4px;
  flex-shrink: 0;
  background: #f5f7fa;
}

.media-cover-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  color: $text-placeholder;
}

.media-info {
  min-width: 0;
  flex: 1;
}

.media-title {
  font-size: 13px;
  color: $text-primary;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.media-meta {
  margin-top: 4px;
  font-size: 11px;
  color: $text-secondary;
  display: flex;
  gap: 10px;
  align-items: center;
}

.media-link {
  color: $primary-color;
  text-decoration: none;

  &:hover {
    text-decoration: underline;
  }
}

.media-footer {
  margin: 10px 0 0;
  font-size: 12px;
  color: $text-secondary;
}

.notes-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
  color: $text-regular;
}

.stats-empty {
  padding: 24px 0;
  text-align: center;
  font-size: 13px;
  color: $text-secondary;
}

.stats-empty-block {
  padding: 8px 0 20px;
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.footer-note {
  font-size: 12px;
  color: $text-placeholder;
}
</style>
