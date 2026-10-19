<template>
  <div class="dashboard">
    <div class="page-header">
      <h1>自媒体自动化运营系统</h1>
      <span class="hint">各维度的运营数据一览；发布、账号、素材的明细去对应页面看</span>
    </div>

    <div class="dashboard-content">
      <!-- 第一行：四个核心指标 -->
      <el-row :gutter="16">
        <el-col :span="6">
          <el-card class="stat-card" shadow="never">
            <div class="stat-top">
              <div class="stat-icon">
                <el-icon><User /></el-icon>
              </div>
              <div class="stat-info">
                <div class="stat-label">账号总数</div>
                <div class="stat-value">{{ accountStats.total }}</div>
              </div>
            </div>
            <div class="stat-tags">
              <el-tag size="small" type="success" effect="plain">正常 {{ accountStats.normal }}</el-tag>
              <el-tag v-if="accountStats.abnormal" size="small" type="danger" effect="plain">
                异常 {{ accountStats.abnormal }}
              </el-tag>
            </div>
          </el-card>
        </el-col>

        <el-col :span="6">
          <el-card class="stat-card" shadow="never">
            <div class="stat-top">
              <div class="stat-icon platform-icon">
                <el-icon><Platform /></el-icon>
              </div>
              <div class="stat-info">
                <div class="stat-label">已接入平台</div>
                <div class="stat-value">{{ platformStats.total }}</div>
              </div>
            </div>
            <div class="stat-tags">
              <el-tag size="small" effect="plain" class="tag-ks">快手 {{ platformStats.kuaishou }}</el-tag>
              <el-tag size="small" effect="plain" class="tag-dy">抖音 {{ platformStats.douyin }}</el-tag>
              <el-tag size="small" effect="plain" class="tag-tx">视频号 {{ platformStats.channels }}</el-tag>
              <el-tag size="small" effect="plain" class="tag-xhs">小红书 {{ platformStats.xiaohongshu }}</el-tag>
              <el-tag size="small" effect="plain" class="tag-pdd">拼多多 {{ platformStats.pdd }}</el-tag>
            </div>
          </el-card>
        </el-col>

        <el-col :span="6">
          <el-card class="stat-card" shadow="never">
            <div class="stat-top">
              <div class="stat-icon content-icon">
                <el-icon><Document /></el-icon>
              </div>
              <div class="stat-info">
                <div class="stat-label">素材总数</div>
                <div class="stat-value">{{ contentStats.total }}</div>
              </div>
            </div>
            <div class="stat-tags">
              <el-tag size="small" type="success" effect="plain">视频 {{ contentStats.videos }}</el-tag>
              <el-tag size="small" type="warning" effect="plain">图片 {{ contentStats.images }}</el-tag>
              <el-tag v-if="contentStats.others" size="small" type="info" effect="plain">
                其他 {{ contentStats.others }}
              </el-tag>
            </div>
          </el-card>
        </el-col>

        <el-col :span="6">
          <el-card class="stat-card" shadow="never">
            <div class="stat-top">
              <div class="stat-icon publish-icon">
                <el-icon><Timer /></el-icon>
              </div>
              <div class="stat-info">
                <div class="stat-label">累计发布</div>
                <div class="stat-value">{{ publishTotals.total }}</div>
              </div>
            </div>
            <div class="stat-tags">
              <el-tag size="small" type="success" effect="plain">成功 {{ publishTotals.success }}</el-tag>
              <el-tag size="small" type="danger" effect="plain">失败 {{ publishTotals.failed }}</el-tag>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 第二行：发布趋势（近 14 天） + 平台分布 -->
      <el-row :gutter="16" class="panel-row mt16">
        <el-col :span="12">
          <el-card shadow="never" class="panel-card">
            <template #header>
              <div class="card-header">
                <span>发布趋势（近 14 天）</span>
                <span class="hint">绿 = 成功 · 红 = 失败（悬停看每日明细）</span>
              </div>
            </template>
            <div v-if="byDay.length" class="trend-chart">
              <div
                v-for="d in byDay"
                :key="d.day"
                class="trend-col"
                :title="`${d.day}：总 ${d.total}，成功 ${d.success}，失败 ${d.failed}`"
              >
                <div class="trend-bars">
                  <div
                    v-if="d.failed"
                    class="bar bar-failed"
                    :style="{ height: barHeight(d.failed) }"
                  ></div>
                  <div
                    v-if="d.success"
                    class="bar bar-success"
                    :style="{ height: barHeight(d.success) }"
                  ></div>
                </div>
                <div class="trend-day">{{ d.day.slice(5) }}</div>
              </div>
            </div>
            <el-empty v-else description="近 14 天还没有发布记录" :image-size="60" />
          </el-card>
        </el-col>

        <el-col :span="12">
          <el-card shadow="never" class="panel-card">
            <template #header><span>平台发布分布</span></template>
            <div v-if="byPlatform.length" class="platform-list">
              <div v-for="p in byPlatform" :key="p.platform" class="platform-row">
                <span class="platform-name">{{ p.name }}</span>
                <span class="platform-count">{{ p.total }} 条</span>
                <el-progress
                  type="line"
                  :percentage="successRate(p)"
                  :stroke-width="8"
                  :color="rateColor(successRate(p))"
                  class="platform-bar"
                />
              </div>
            </div>
            <el-empty v-else description="还没有发布记录" :image-size="60" />
          </el-card>
        </el-col>
      </el-row>

      <!-- 第三行：账号发布排行 + 来源与素材覆盖 -->
      <el-row :gutter="16" class="mt16">
        <el-col :span="12">
          <el-card shadow="never" class="panel-card">
            <template #header><span>账号发布排行（Top 8）</span></template>
            <div v-if="byAccount.length" class="rank-list">
              <div v-for="(a, i) in byAccount" :key="`${a.platform}-${a.name}`" class="rank-row">
                <span class="rank-no" :class="{ top: i < 3 }">{{ i + 1 }}</span>
                <span class="rank-name">
                  <span class="rank-platform">{{ a.platform }}</span>
                  {{ a.name }}
                </span>
                <span class="rank-ok">{{ a.success }} 成功</span>
                <span class="rank-total">/ {{ a.total }} 条</span>
              </div>
            </div>
            <el-empty v-else description="还没有账号发过内容" :image-size="60" />
          </el-card>
        </el-col>

        <el-col :span="12">
          <el-card shadow="never" class="panel-card">
            <template #header><span>发布来源 & 素材文案覆盖</span></template>
            <div class="source-block">
              <div v-for="s in bySource" :key="s.source" class="source-row">
                <span class="source-label">{{ s.label }}</span>
                <el-progress
                  :percentage="sourcePercent(s.count)"
                  :stroke-width="12"
                  :show-text="false"
                  class="source-bar"
                />
                <span class="source-count">{{ s.count }} 条</span>
              </div>
              <el-empty v-if="!bySource.length" description="还没有发布记录" :image-size="50" />
            </div>
            <el-divider />
            <div class="copy-block">
              <el-progress
                type="circle"
                :width="72"
                :percentage="copyCoverage"
                :stroke-width="8"
                :color="rateColor(copyCoverage)"
              />
              <div class="copy-note">
                <div class="copy-title">素材已带 AI 文案：{{ copyCoverage }}%</div>
                {{ publishTotals.materials_with_copy }} / {{ publishTotals.materials }} 个素材。
                带文案的素材发布时自动带出标题正文，不用现写。
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { User, Platform, Document, Timer } from '@element-plus/icons-vue'
import { accountApi } from '@/api/account'
import { materialApi } from '@/api/material'
import { publishApi } from '@/api/publish'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'

const router = useRouter()
const accountStore = useAccountStore()
const appStore = useAppStore()
const loading = ref(false)

// 发布统计（/publish/stats/overview 一次拉全；失败时全 0，不挡页面）
const publishTotals = ref({ total: 0, success: 0, failed: 0, materials: 0, materials_with_copy: 0 })
const byPlatform = ref([])
const byAccount = ref([])
const byDay = ref([])
const bySource = ref([])

// 账号统计数据 - 从真实数据计算
const accountStats = computed(() => {
  const accounts = accountStore.accounts
  const normal = accounts.filter(a => a.status === '正常').length
  const abnormal = accounts.filter(a => a.status !== '正常' && a.status !== '验证中').length
  return {
    total: accounts.length,
    normal,
    abnormal
  }
})

// 平台统计数据 - 从真实数据计算
const platformStats = computed(() => {
  const accounts = accountStore.accounts
  const kuaishou = accounts.filter(a => a.platform === '快手').length
  const douyin = accounts.filter(a => a.platform === '抖音').length
  const channels = accounts.filter(a => a.platform === '视频号').length
  const xiaohongshu = accounts.filter(a => a.platform === '小红书').length
  const pdd = accounts.filter(a => a.platform === '拼多多视频').length
  // 统计有账号的平台数量
  const total = [kuaishou, douyin, channels, xiaohongshu, pdd].filter(n => n > 0).length
  return { total, kuaishou, douyin, channels, xiaohongshu, pdd }
})

// 素材统计数据 - 从真实数据计算
const videoExtensions = ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv']
const imageExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']

const contentStats = computed(() => {
  const materials = appStore.materials
  const videos = materials.filter(m => videoExtensions.some(ext => m.filename.toLowerCase().endsWith(ext))).length
  const images = materials.filter(m => imageExtensions.some(ext => m.filename.toLowerCase().endsWith(ext))).length
  return {
    total: materials.length,
    videos,
    images,
    others: materials.length - videos - images
  }
})

/** 素材文案覆盖率（%）；没有素材时显示 0 */
const copyCoverage = computed(() => {
  const n = publishTotals.value.materials || 0
  if (!n) return 0
  return Math.round(((publishTotals.value.materials_with_copy || 0) / n) * 100)
})

/** 平台成功率（%）；没发过按 0 算，进度条颜色另行区分 */
const successRate = (row) => (row.total ? Math.round((row.success / row.total) * 100) : 0)

const rateColor = (pct) => {
  if (pct >= 90) return '#67c23a'
  if (pct >= 60) return '#e6a23c'
  return '#f56c6c'
}

/** 来源占比（%） */
const sourcePercent = (count) => {
  const total = bySource.value.reduce((s, i) => s + i.count, 0)
  return total ? Math.round((count / total) * 100) : 0
}

/** 趋势柱高：以近 14 天最大单日量为满高 */
const barHeight = (n) => {
  const max = Math.max(...byDay.value.map((d) => d.total), 1)
  return `${Math.max(4, Math.round((n / max) * 120))}px`
}

// 加载数据
const fetchDashboardData = async () => {
  loading.value = true
  try {
    // 并行获取账号、素材和发布统计；allSettled 让一个失败不拖垮其它
    const [accountRes, materialRes, statsRes] = await Promise.allSettled([
      accountApi.getAccounts(),
      materialApi.getAllMaterials(),
      publishApi.statsOverview()
    ])

    if (accountRes.status === 'fulfilled' && accountRes.value.code === 200) {
      accountStore.setAccounts(accountRes.value.data)
    }
    if (materialRes.status === 'fulfilled' && materialRes.value.code === 200) {
      appStore.setMaterials(materialRes.value.data)
    }
    if (statsRes.status === 'fulfilled' && statsRes.value.code === 200) {
      const d = statsRes.value.data || {}
      publishTotals.value = { ...publishTotals.value, ...(d.totals || {}) }
      byPlatform.value = d.by_platform || []
      byAccount.value = d.by_account || []
      byDay.value = d.by_day || []
      bySource.value = d.by_source || []
    }
  } catch (error) {
    console.error('获取仪表盘数据失败:', error)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchDashboardData()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.dashboard {
  .page-header {
    margin-bottom: 20px;
    display: flex;
    align-items: baseline;
    gap: 12px;

    h1 {
      font-size: 22px;
      color: $text-primary;
      margin: 0;
    }

    .hint {
      color: $text-secondary;
      font-size: 13px;
    }
  }

  .mt16 { margin-top: 16px; }

  /* 同一行的卡片：等宽 + 等高。
     等宽靠 el-col 的 span（全部对称取整），等高靠把 el-row 声明成 flex 行、
     卡片 height: 100% —— 否则哪张卡内容多哪张就高，一行里参差不齐。 */
  .dashboard-content {
    .el-row {
      display: flex;
      flex-wrap: wrap;
      align-items: stretch;
    }

    .el-col {
      display: flex;
    }
  }

  /* 分析面板内部区域：撑满剩余高度，empty 状态垂直居中，等高的两张卡观感一致 */
  .panel-card :deep(.el-card__body) {
    display: flex;
    flex-direction: column;
    height: calc(100% - 45px); /* 减掉 header 的高度 */
    box-sizing: border-box;
  }

  .trend-chart,
  .platform-list,
  .rank-list,
  .source-block,
  .copy-block,
  .panel-card :deep(.el-empty) {
    flex: 1;
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;

    .hint {
      color: $text-secondary;
      font-size: 12px;
    }
  }

  /* 分析面板卡：整行等高，头部与主体统一节奏 */
  .panel-card {
    width: 100%;
    border-radius: 8px;

    :deep(.el-card__header) {
      padding: 12px 16px;
      font-weight: 600;
      color: $text-primary;
    }
  }

  .dashboard-content {
    /* ---- 核心指标卡：等宽等高（跟随所在列），内容自适应、绝不出滚动条 ---- */
    .stat-card {
      width: 100%;
      margin-bottom: 16px;
      border-radius: 8px;
      transition: box-shadow 0.25s, transform 0.25s;

      &:hover {
        transform: translateY(-3px);
        box-shadow: 0 6px 16px rgba(0, 0, 0, 0.08);
      }

      :deep(.el-card__body) {
        padding: 16px;
      }

      .stat-top {
        display: flex;
        align-items: center;
        gap: 14px;

        .stat-icon {
          width: 48px;
          height: 48px;
          flex-shrink: 0;
          border-radius: 10px;
          background-color: rgba($primary-color, 0.1);
          display: flex;
          justify-content: center;
          align-items: center;

          .el-icon {
            font-size: 24px;
            color: $primary-color;
          }

          &.platform-icon {
            background-color: rgba($success-color, 0.12);

            .el-icon { color: $success-color; }
          }

          &.content-icon {
            background-color: rgba($info-color, 0.12);

            .el-icon { color: $info-color; }
          }

          &.publish-icon {
            background-color: rgba($warning-color, 0.12);

            .el-icon { color: $warning-color; }
          }
        }

        .stat-info {
          min-width: 0;

          .stat-label {
            font-size: 13px;
            color: $text-secondary;
          }

          .stat-value {
            font-size: 26px;
            font-weight: 700;
            color: $text-primary;
            line-height: 1.3;
          }
        }
      }

      /* 明细标签：允许换行、不设固定高度——五个平台标签也放得下，不出滚动条 */
      .stat-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 12px;
        padding-top: 10px;
        border-top: 1px solid $border-lighter;

        .el-tag {
          margin: 0;
          border-radius: 10px;
        }

        /* 各平台标签底色（浅色系，和各平台品牌色呼应） */
        .tag-ks { background: #f0f9eb; color: #5daf34; border-color: #e1f3d8; }
        .tag-dy { background: #fef0f0; color: #c45656; border-color: #fde2e2; }
        .tag-tx { background: #fdf6ec; color: #b88230; border-color: #faecd8; }
        .tag-xhs { background: #fef0f0; color: #d05c77; border-color: #fde2e2; }
        .tag-pdd { background: #ecf5ff; color: #4a7fc1; border-color: #d9ecff; }
      }
    }

    /* ---- 近 14 天趋势：纯 CSS 柱状图（不引图表库，保持构建轻） ---- */
    .trend-chart {
      display: flex;
      align-items: flex-end;
      gap: 6px;
      height: 180px;
      padding: 8px 4px 0;
      overflow: hidden;

      .trend-col {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: flex-end;
        height: 100%;
      }

      .trend-bars {
        display: flex;
        align-items: flex-end;
        gap: 3px;
      }

      .bar {
        width: 10px;
        border-radius: 3px 3px 0 0;
        transition: height 0.3s;
      }

      .bar-success { background: #67c23a; }
      .bar-failed { background: #f56c6c; }

      .trend-day {
        margin-top: 6px;
        font-size: 11px;
        color: $text-secondary;
        white-space: nowrap;
      }
    }

    /* ---- 平台分布：行式进度条，比小表格更紧凑 ---- */
    .platform-list {
      .platform-row {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 7px 0;

        & + .platform-row { border-top: 1px dashed $border-lighter; }

        .platform-name {
          width: 72px;
          font-size: 13px;
          color: $text-primary;
        }

        .platform-count {
          width: 52px;
          text-align: right;
          font-size: 12px;
          color: $text-secondary;
        }

        .platform-bar { flex: 1; }
      }
    }

    /* ---- 账号排行：名次 + 账号 + 成绩 ---- */
    .rank-list {
      .rank-row {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 0;

        & + .rank-row { border-top: 1px dashed $border-lighter; }

        .rank-no {
          width: 22px;
          height: 22px;
          border-radius: 6px;
          background: #f0f2f5;
          color: $text-secondary;
          font-size: 12px;
          display: inline-flex;
          justify-content: center;
          align-items: center;
          flex-shrink: 0;

          &.top {
            background: rgba($primary-color, 0.12);
            color: $primary-color;
            font-weight: 700;
          }
        }

        .rank-name {
          flex: 1;
          min-width: 0;
          font-size: 13px;
          color: $text-primary;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;

          .rank-platform {
            font-size: 12px;
            color: $text-secondary;
            margin-right: 6px;
          }
        }

        .rank-ok {
          font-size: 13px;
          color: $success-color;
          font-weight: 600;
        }

        .rank-total {
          font-size: 12px;
          color: $text-secondary;
        }
      }
    }

    .source-block {
      .source-row {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 10px;

        .source-label {
          width: 90px;
          font-size: 13px;
          color: $text-primary;
        }

        .source-bar { flex: 1; }

        .source-count {
          width: 64px;
          text-align: right;
          font-size: 13px;
          color: $text-secondary;
        }
      }
    }

    .copy-block {
      display: flex;
      align-items: center;
      gap: 16px;

      .copy-note {
        flex: 1;
        font-size: 12px;
        color: $text-secondary;
        line-height: 1.7;

        .copy-title {
          font-size: 13px;
          font-weight: 600;
          color: $text-primary;
          margin-bottom: 2px;
        }
      }
    }
  }
}
</style>
