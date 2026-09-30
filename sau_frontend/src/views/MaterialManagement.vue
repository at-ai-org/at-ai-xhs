<template>
  <div class="material-management">
    <div class="page-header">
      <h1>素材管理</h1>
    </div>
    
    <div class="material-list-container">
      <div class="material-search">
        <el-input
          v-model="searchKeyword"
          placeholder="输入文件名搜索"
          prefix-icon="Search"
          clearable
          @clear="handleSearch"
          @input="handleSearch"
        />
        <div class="action-buttons">
          <el-button type="primary" @click="handleUploadMaterial">上传素材</el-button>
          <el-button type="info" @click="handleRefresh" :loading="isRefreshing">
            <el-icon v-if="!isRefreshing"><Refresh /></el-icon>
            <span v-if="isRefreshing">刷新中</span>
          </el-button>
        </div>
      </div>
      
      <div class="material-list">
        <el-table :data="pagedMaterials" style="width: 100%">
          <el-table-column prop="filename" label="文件名" min-width="240" show-overflow-tooltip />
          <el-table-column prop="filesize" label="文件大小" width="110">
            <template #default="scope">
              {{ scope.row.filesize }} MB
            </template>
          </el-table-column>
          <el-table-column prop="upload_time" label="上传时间（本地）" width="180" />
          <el-table-column label="AI 文案" width="100" align="center">
            <template #default="scope">
              <el-tag size="small" :type="scope.row.has_copy ? 'success' : 'info'" effect="plain">
                {{ scope.row.has_copy ? '已有' : '无' }}
              </el-tag>
            </template>
          </el-table-column>
          <!-- 背景音乐状态列：紧跟 AI 文案（需求指定），三档文案见 bgmText() -->
          <el-table-column label="背景音乐" width="110" align="center">
            <template #default="scope">
              <el-tooltip :disabled="!bgmTooltip(scope.row)" :content="bgmTooltip(scope.row)" placement="top">
                <el-tag size="small" :type="bgmTagType(scope.row)" effect="plain">
                  {{ bgmText(scope.row) }}
                </el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <!-- 操作列**不固定**：四个按钮并排约需 340px，380 留一点余量就够，
               固定住反而挡住文件名，宽度给太多则会在右侧留一段空白 -->
          <el-table-column label="操作" width="380">
            <template #default="scope">
              <el-button size="small" @click="handlePreview(scope.row)">预览</el-button>
              <el-button
                v-if="scope.row.has_copy"
                size="small"
                type="primary"
                plain
                @click="openCopyPreview(scope.row)"
              >
                预览文案
              </el-button>
              <el-button
                v-else
                size="small"
                type="success"
                plain
                :loading="isGenerating(scope.row)"
                @click="handleGenerateCopy(scope.row)"
              >
                生成文案
              </el-button>
              <!-- 只有视频能换音轨，图片素材上不显示这个按钮 -->
              <el-button
                v-if="isVideoFile(scope.row)"
                size="small"
                type="warning"
                plain
                @click="openBgmDialog(scope.row)"
              >
                背景音乐
              </el-button>
              <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
            </template>
          </el-table-column>
          <!-- 用表格自带的空态槽，别再叠一个 el-empty：会出现"No Data"和自定义文案上下两截 -->
          <template #empty>
            <el-empty :description="searchKeyword ? '没有匹配的素材' : '暂无素材数据'" />
          </template>
        </el-table>

        <el-pagination
          v-if="filteredMaterials.length > 0"
          class="material-pagination"
          layout="total, sizes, prev, pager, next, jumper"
          :total="filteredMaterials.length"
          :current-page="currentPage"
          :page-size="pageSize"
          :page-sizes="[20, 50, 100]"
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </div>
    
    <!-- 上传对话框 -->
    <el-dialog
      v-model="uploadDialogVisible"
      title="上传素材"
      width="40%"
      @close="handleUploadDialogClose"
    >
      <div class="upload-form">
        <el-form label-width="80px">
          <el-form-item label="文件名称:">
            <el-input
              v-model="customFilename"
              placeholder="选填 (仅单个文件时生效)"
              :disabled="customFilenameDisabled"
              clearable
            />
          </el-form-item>
          <el-form-item label="选择文件">
            <el-upload
              class="upload-demo"
              drag
              multiple
              :auto-upload="false"
              :on-change="handleFileChange"
              :on-remove="handleFileRemove"
              :file-list="fileList"
            >
              <el-icon class="el-icon--upload"><Upload /></el-icon>
              <div class="el-upload__text">
                将文件拖到此处，或<em>点击上传</em>
              </div>
              <template #tip>
                <div class="el-upload__tip">
                  支持视频、图片等格式文件，可一次选择多个文件
                </div>
              </template>
            </el-upload>
          </el-form-item>
          <el-form-item label="上传列表" v-if="fileList.length > 0">
            <div class="upload-file-list">
              <div v-for="file in fileList" :key="file.uid" class="upload-file-item">
                <span class="file-name">{{ file.name }}</span>
                <el-progress
                  :percentage="uploadProgress[file.uid]?.percentage || 0"
                  :text-inside="true"
                  :stroke-width="20"
                  style="width: 100%; margin-top: 5px;"
                >
                  <span>{{ uploadProgress[file.uid]?.speed || '' }}</span>
                </el-progress>
              </div>
            </div>
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="uploadDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitUpload" :loading="isUploading">
            {{ isUploading ? '上传中' : '确认上传' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
    
    <!-- 预览对话框 -->
    <el-dialog
      v-model="previewDialogVisible"
      title="素材预览"
      width="50%"
      :top="'10vh'"
    >
      <div class="preview-container" v-loading="previewLoading" v-if="currentMaterial">
        <div v-if="isVideoFile(currentMaterial)" class="video-preview">
          <video
            controls
            preload="metadata"
            style="max-width: 100%; max-height: 60vh;"
            @loadeddata="onMediaLoaded"
            @error="onMediaError"
          >
            <source :src="getPreviewUrl(currentMaterial.file_path)" type="video/mp4">
            您的浏览器不支持视频播放
          </video>
        </div>
        <div v-else-if="isImageFile(currentMaterial)" class="image-preview">
          <img
            :src="getPreviewUrl(currentMaterial.file_path)"
            style="max-width: 100%; max-height: 60vh;"
            @load="onMediaLoaded"
            @error="onMediaError"
          />
        </div>
        <div v-else class="file-info">
          <p>文件名: {{ currentMaterial.filename }}</p>
          <p>文件大小: {{ currentMaterial.filesize }} MB</p>
          <p>上传时间: {{ currentMaterial.upload_time }}</p>
          <el-button type="primary" @click="downloadFile(currentMaterial)">下载文件</el-button>
        </div>

        <el-alert
          v-if="previewError"
          class="mt12"
          type="error"
          :closable="false"
          show-icon
          :title="previewError"
        />
        <div class="preview-meta">
          <span>文件名：{{ currentMaterial.filename }}</span>
          <span>大小：{{ currentMaterial.filesize }} MB</span>
          <span>上传时间：{{ currentMaterial.upload_time }}</span>
        </div>
      </div>
    </el-dialog>

    <!-- 合成背景音乐对话框 -->
    <el-dialog
      v-model="bgmDialogVisible"
      title="合成背景音乐"
      width="560px"
      :close-on-click-modal="false"
      :close-on-press-escape="!isComposing"
      @closed="onBgmDialogClosed"
    >
      <div v-if="bgmMaterial" class="bgm-compose">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="合成会去掉原视频的音轨，换成选中的背景音乐"
          description="音乐比视频短会自动循环播放；成品文件名变成「原名-音乐名.mp4」，原文件被替换掉。"
        />

        <div class="bgm-material">
          素材：<strong>{{ bgmMaterial.filename }}</strong>
        </div>

        <div class="bgm-picker">
          <el-select
            v-model="bgmMusicId"
            placeholder="选择背景音乐"
            filterable
            clearable
            :loading="isLoadingMusic"
            :disabled="isComposing"
            class="bgm-select"
          >
            <el-option v-for="item in musicList" :key="item.id" :label="item.name" :value="item.id">
              <span>{{ item.name }}</span>
              <span class="bgm-option-meta">{{ formatDuration(item.duration) }}</span>
            </el-option>
          </el-select>
          <el-button :disabled="!bgmMusicId" @click="toggleMusicPreview">
            {{ previewingMusicId === bgmMusicId ? '停止' : '试听' }}
          </el-button>
        </div>

        <div v-if="!isLoadingMusic && !musicList.length" class="bgm-hint">
          还没有背景音乐，先去
          <el-link type="primary" :underline="false" @click="goMusicPage">背景音乐</el-link>
          页上传。
        </div>

        <!-- 试听共用一个 audio（和背景音乐页同一个做法） -->
        <audio ref="bgmAudioRef" preload="none" style="display: none" @ended="stopMusicPreview" @error="stopMusicPreview" />

        <!-- 合成进度：任务一提交就显示，百分比来自后端（ffmpeg 的真实写入位置） -->
        <div v-if="composeTask" class="bgm-progress">
          <el-progress
            :percentage="composePercent"
            :status="composeProgressStatus"
            :stroke-width="16"
            :text-inside="true"
          />
          <div class="bgm-progress-text">
            <span>{{ composeTask.message || '正在合成…' }}</span>
            <span v-if="composeTask.output">→ {{ composeTask.output }}</span>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="dialog-footer">
          <el-button :disabled="isComposing" @click="bgmDialogVisible = false">关闭</el-button>
          <el-button v-if="isComposing" type="danger" plain @click="cancelCompose">中断合成</el-button>
          <el-button v-else type="primary" :disabled="!bgmMusicId" @click="startCompose">
            开始合成
          </el-button>
        </div>
      </template>
    </el-dialog>

    <!-- AI 文案预览对话框 -->
    <el-dialog
      v-model="copyDialogVisible"
      title="AI 文案预览"
      width="620px"
      :top="'8vh'"
    >
      <div v-if="copyMaterial" class="copy-preview">
        <div class="copy-field">
          <div class="copy-label">标题</div>
          <div class="copy-value copy-title">{{ copyMaterial.title || '—' }}</div>
        </div>
        <div class="copy-field">
          <div class="copy-label">正文</div>
          <div class="copy-value copy-desc">{{ copyMaterial.desc || '—' }}</div>
        </div>
        <div class="copy-field">
          <div class="copy-label">话题</div>
          <div class="copy-value">
            <template v-if="(copyMaterial.tags || []).length">
              <el-tag
                v-for="tag in copyMaterial.tags"
                :key="tag"
                size="small"
                effect="plain"
                class="copy-tag"
              >
                #{{ tag }}
              </el-tag>
            </template>
            <span v-else class="copy-empty">—</span>
          </div>
        </div>
        <div class="copy-meta">
          <span>素材：{{ copyMaterial.filename }}</span>
          <span>生成时间：{{ copyMaterial.copy_updated_at || '—' }}</span>
        </div>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="copyDialogVisible = false">关闭</el-button>
          <el-button
            type="primary"
            :loading="isGenerating(copyMaterial || {})"
            @click="handleGenerateCopy(copyMaterial, { fromDialog: true })"
          >
            重新生成
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { Refresh, Upload } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import { materialApi } from '@/api/material'
import { musicApi } from '@/api/music'
import { xhsApi } from '@/api/xhs'
import { useAppStore } from '@/stores/app'

// 获取应用状态管理
const appStore = useAppStore()
const router = useRouter()

// 搜索和状态控制
const searchKeyword = ref('')
const isRefreshing = ref(false)
const isUploading = ref(false)

// 分页：一页 20 条，按上传时间倒序
const currentPage = ref(1)
const pageSize = ref(20)

// 对话框控制
const uploadDialogVisible = ref(false)
const previewDialogVisible = ref(false)
const currentMaterial = ref(null)
const previewLoading = ref(false)
const previewError = ref('')

// AI 文案
const copyDialogVisible = ref(false)
const copyMaterial = ref(null)
// 正在生成文案的素材：{ [磁盘文件名]: true }，用来给按钮单独转圈
const generatingCopy = ref({})

// 文件上传
const fileList = ref([])
const customFilename = ref('')
const customFilenameDisabled = computed(() => fileList.value.length > 1)
const uploadProgress = ref({}); // { [uid]: { percentage: 0, speed: '' } }


watch(fileList, (newList) => {
  if (newList.length <= 1) {
    // If you want to clear the custom name when going back to single file, uncomment below
    // customFilename.value = ''
  }
});


// 获取素材列表。silent=true 时不弹「刷新成功」——挂载时的自动拉取不该弹提示。
const fetchMaterials = async (silent = false) => {
  isRefreshing.value = true
  try {
    const response = await materialApi.getAllMaterials()

    if (response.code === 200) {
      appStore.setMaterials(response.data)
      if (!silent) ElMessage.success('刷新成功')
      // 拉完就知道有没有素材在合成中：有就开轮询，没有就停掉
      ensureStatusPolling()
    }
  } catch (error) {
    // 失败提示由请求拦截器统一弹出（会带后端的具体原因）
    console.error('获取素材列表出错:', error)
  } finally {
    isRefreshing.value = false
  }
}

const handleRefresh = () => fetchMaterials(false)

// 排序：按上传时间倒序（最新的在最前）。
// 时间字符串是 "YYYY-MM-DD HH:MM:SS"，直接比字符串就是比时间；
// 时间相同（同一批上传）再按 id 倒序，保证顺序稳定、翻页不跳条。
const sortedMaterials = computed(() => {
  return [...appStore.materials].sort((a, b) => {
    const ta = String(a.upload_time || '')
    const tb = String(b.upload_time || '')
    if (ta !== tb) return tb.localeCompare(ta)
    return (b.id || 0) - (a.id || 0)
  })
})

// 过滤素材
const filteredMaterials = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  if (!keyword) return sortedMaterials.value

  return sortedMaterials.value.filter(material =>
    String(material.filename || '').toLowerCase().includes(keyword)
  )
})

// 当前页要展示的素材
const pagedMaterials = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredMaterials.value.slice(start, start + pageSize.value)
})

// 搜索处理：结果变了要回到第一页，否则可能停在一个空白页上
const handleSearch = () => {
  currentPage.value = 1
}

const handlePageChange = (value) => {
  currentPage.value = value
}

const handleSizeChange = (value) => {
  pageSize.value = value
  currentPage.value = 1
}

// 数据变少（删除、搜索、刷新）后把页码夹回有效范围
watch(
  () => filteredMaterials.value.length,
  (total) => {
    const maxPage = Math.max(1, Math.ceil(total / pageSize.value))
    if (currentPage.value > maxPage) currentPage.value = maxPage
  }
)

// --------------------------------------------------------------------------- //
// AI 文案（预览 / 生成，规则与发布中心一致：抽首尾帧 → 视觉模型）
// --------------------------------------------------------------------------- //

const diskNameOf = (material) => String(material?.file_path || '').split('/').pop()

const isGenerating = (material) => !!generatingCopy.value[diskNameOf(material)]

/** 把生成/接口返回的文案写回列表里的这一行，不用整表重新拉 */
const applyCopyToRow = (material, copy) => {
  material.title = copy?.title || ''
  material.desc = copy?.desc || ''
  material.tags = copy?.tags || []
  material.has_copy = !!copy?.desc
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  material.copy_updated_at =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
}

const openCopyPreview = (material) => {
  copyMaterial.value = material
  copyDialogVisible.value = true
}

/** 生成文案：首帧 + 尾帧交给大模型，结果存到素材上（下次发布可直接带出） */
const handleGenerateCopy = async (material, options = {}) => {
  if (!material) return
  const disk = diskNameOf(material)
  if (!disk) {
    ElMessage.error('这条素材没有文件名，无法生成文案')
    return
  }

  let renamedTo = ''
  generatingCopy.value = { ...generatingCopy.value, [disk]: true }
  try {
    const res = await xhsApi.analyze(disk, '', true)
    if (res.code === 200) {
      const data = res.data || {}
      renamedTo = data.video_name || ''
      // 后端会把素材改名成标题（文件名只留中文和数字），所以这里不能只改文案字段：
      // 整表拉一次拿到新的 file_path/filename，否则这一行还指着旧文件名，
      // 预览、删除、再次生成都会找不到文件。
      await fetchMaterials(true)
      const fresh =
        appStore.materials.find((m) => m.file_path === renamedTo) ||
        appStore.materials.find((m) => diskNameOf(m) === disk)
      const row = fresh || material
      if (!fresh) {
        // 理论上不会走到这里；真发生了就地把这一行改对，至少不指向旧文件
        if (renamedTo && renamedTo !== disk) material.file_path = renamedTo
        applyCopyToRow(material, data)
      }
      if (data.rename_note) ElMessage.info(data.rename_note)
      ElMessage.success(options.fromDialog ? '文案已重新生成' : '文案已生成')
      // 生成完顺手把结果摆出来，省一次点击
      if (options.fromDialog) copyMaterial.value = row
      else openCopyPreview(row)
    }
  } catch (error) {
    // 具体原因（没配模型、抽帧失败、视频不存在…）由请求拦截器提示
    console.error('生成 AI 文案失败:', error)
  } finally {
    const next = { ...generatingCopy.value }
    delete next[disk]
    // 改名后按钮的 loading key 也变了，两个都清掉
    if (renamedTo) delete next[renamedTo]
    generatingCopy.value = next
  }
}

// --------------------------------------------------------------------------- //
// 背景音乐合成（选音乐 → 后台 ffmpeg 换掉原音轨 → 原地替换素材）
//
// 合成是异步的：`POST /music/compose` 只回任务号，进度靠轮询 `/music/task/<id>`
// （百分比是 ffmpeg 的真实写入位置，不是估算）。完成后素材会被改名成
// 「原名-音乐名.mp4」，所以必须整表重拉一次，不能只改这一行。
// --------------------------------------------------------------------------- //

const bgmDialogVisible = ref(false)
const bgmMaterial = ref(null)
const bgmMusicId = ref(null)
const musicList = ref([])
const isLoadingMusic = ref(false)
const isComposing = ref(false)
const composeTask = ref(null)
const previewingMusicId = ref(null)
const bgmAudioRef = ref(null)

// 弹窗里那个任务的轮询、以及列表「背景音乐」列的轮询
let composeTimer = null
let statusTimer = null

const composePercent = computed(() => {
  const task = composeTask.value
  if (!task) return 0
  if (task.state === 'done') return 100
  // 没结束前不显示 100%：完成是后端说的，不是进度条凑到的
  return Math.max(0, Math.min(99, Number(task.progress) || 0))
})

const composeProgressStatus = computed(() => {
  const state = composeTask.value?.state
  if (state === 'done') return 'success'
  if (state === 'failed' || state === 'cancelled') return 'exception'
  return undefined
})

const formatDuration = (seconds) => {
  const total = Math.round(Number(seconds) || 0)
  if (!total) return '—'
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

// ---- 状态列的显示 ----------------------------------------------------------

const bgmText = (row) => row?.bgm_status_text || '待合成'

const bgmTagType = (row) => {
  const map = { done: 'success', running: 'warning', failed: 'danger' }
  return map[row?.bgm_status] || 'info'
}

/** 悬停提示：合成中给百分比、已合成给音乐名、失败给原因（失败时列里显示的仍是「待合成」）。*/
const bgmTooltip = (row) => {
  const status = row?.bgm_status
  if (status === 'running') {
    return `正在合成${row.bgm_music ? `：${row.bgm_music}` : ''}（${row.bgm_progress || 0}%）`
  }
  if (status === 'done') {
    return row.bgm_music ? `已用「${row.bgm_music}」替换原音轨` : '已合成'
  }
  return row?.bgm_message || ''
}

/** 就地改某一行的合成状态（列表对象就是 store 里的对象，改完表格立刻刷新）。*/
const applyBgmToRow = (disk, patch) => {
  const row = appStore.materials.find((m) => diskNameOf(m) === disk)
  if (!row) return
  if (patch.status) row.bgm_status = patch.status
  if (patch.text) row.bgm_status_text = patch.text
  row.bgm_progress = Number(patch.progress) || 0
  if (patch.message !== undefined) row.bgm_message = patch.message || ''
}

// ---- 列表那一列的轮询（刷新页面后仍能看到别人的合成进度） -------------------

/** 只在"确实有素材在合成中"时才开轮询，空闲时一次多余请求都不发。*/
function ensureStatusPolling() {
  const anyRunning = appStore.materials.some((m) => m.bgm_status === 'running')
  if (anyRunning && !statusTimer) {
    statusTimer = setInterval(refreshBgmStatuses, 3000)
  } else if (!anyRunning && statusTimer) {
    clearInterval(statusTimer)
    statusTimer = null
  }
}

async function refreshBgmStatuses() {
  try {
    const res = await musicApi.statuses()
    const map = res.data || {}
    let renamed = false
    for (const material of appStore.materials) {
      const disk = diskNameOf(material)
      const status = map[disk]
      if (status) {
        material.bgm_status = status.status
        material.bgm_status_text = status.status_text
        material.bgm_music = status.music_name
        material.bgm_progress = status.progress
        material.bgm_message = status.message
        continue
      }
      // 记录没了、而这行本来在合成中 → 合成成功后素材改名了，整表重拉
      if (material.bgm_status === 'running') renamed = true
    }
    if (renamed) await fetchMaterials(true)
  } catch (error) {
    /* 网络抖动/后端重启：下一轮再试，不打扰用户 */
  } finally {
    ensureStatusPolling()
  }
}

// ---- 弹窗 ------------------------------------------------------------------

const fetchMusicList = async () => {
  isLoadingMusic.value = true
  try {
    const res = await musicApi.list()
    if (res.code === 200) {
      musicList.value = res.data || []
      // 只有一首就默认选上，省一次点击
      if (musicList.value.length === 1) bgmMusicId.value = musicList.value[0].id
    }
  } catch (error) {
    /* 拦截器已提示 */
  } finally {
    isLoadingMusic.value = false
  }
}

const openBgmDialog = async (material) => {
  bgmMaterial.value = material
  bgmMusicId.value = null
  composeTask.value = null
  bgmDialogVisible.value = true
  stopMusicPreview()
  await fetchMusicList()
}

const onBgmDialogClosed = () => {
  stopMusicPreview()
  composeTask.value = null
  bgmMaterial.value = null
}

const stopMusicPreview = () => {
  const el = bgmAudioRef.value
  if (el) {
    el.pause()
    el.removeAttribute('src')
    el.load()
  }
  previewingMusicId.value = null
}

const toggleMusicPreview = () => {
  if (!bgmMusicId.value) return
  if (previewingMusicId.value === bgmMusicId.value) {
    stopMusicPreview()
    return
  }
  const el = bgmAudioRef.value
  if (!el) return
  stopMusicPreview()
  el.src = musicApi.fileUrl(bgmMusicId.value)
  previewingMusicId.value = bgmMusicId.value
  const played = el.play()
  if (played && typeof played.catch === 'function') {
    played.catch(() => {
      previewingMusicId.value = null
      ElMessage.warning('浏览器没能开始播放，请再点一次「试听」')
    })
  }
}

const goMusicPage = () => {
  bgmDialogVisible.value = false
  router.push('/background-music')
}

const startCompose = async () => {
  const material = bgmMaterial.value
  const musicId = bgmMusicId.value
  if (!material || !musicId || isComposing.value) return

  const disk = diskNameOf(material)
  if (!disk) {
    ElMessage.error('这条素材没有文件名，无法合成背景音乐')
    return
  }

  stopMusicPreview()
  isComposing.value = true
  composeTask.value = { state: 'queued', progress: 0, message: '正在提交合成任务…', material: disk }
  applyBgmToRow(disk, { status: 'running', text: '合成中', progress: 0, message: '' })

  try {
    const res = await musicApi.compose(disk, musicId)
    const taskId = res.data?.task_id
    if (!taskId) throw new Error('后端没有返回任务号')
    startComposePolling(taskId)
  } catch (error) {
    // 具体原因（素材不存在、没有视频轨、正在合成中…）由请求拦截器提示
    isComposing.value = false
    composeTask.value = null
    await fetchMaterials(true)
  }
}

function startComposePolling(taskId) {
  stopComposePolling()
  composeTimer = setInterval(async () => {
    let task = null
    try {
      const res = await musicApi.getTask(taskId)
      task = res.data || {}
    } catch (error) {
      // 任务查不到（后端重启 / 被清理）：停下轮询，别每 800ms 弹一次错
      stopComposePolling()
      isComposing.value = false
      ElMessage.warning('查不到合成进度了（后端可能重启过），请刷新列表确认结果')
      return
    }

    composeTask.value = task
    if (task.state === 'queued' || task.state === 'running') {
      applyBgmToRow(task.material, {
        status: 'running',
        text: '合成中',
        progress: task.progress,
        message: ''
      })
      return
    }

    stopComposePolling()
    await finishCompose(task)
  }, 800)
}

function stopComposePolling() {
  if (composeTimer) {
    clearInterval(composeTimer)
    composeTimer = null
  }
}

async function finishCompose(task) {
  isComposing.value = false
  await fetchMaterials(true)

  if (task.state === 'done') {
    ElMessage.success(`合成完成：${task.output || ''}`)
    // 素材已经改名，弹窗里拿的还是旧引用，关掉免得再点一次「开始合成」报"文件不存在"
    bgmDialogVisible.value = false
  } else if (task.state === 'cancelled') {
    ElMessage.info('已中断合成')
  } else {
    ElMessage.error(task.message || '合成失败')
  }
}

const cancelCompose = async () => {
  const taskId = composeTask.value?.task_id
  if (!taskId) return
  try {
    await musicApi.cancelTask(taskId)
  } catch (error) {
    /* 任务已经结束：让它自然收敛，不用额外提示 */
  }
}

// 上传素材
const handleUploadMaterial = () => {  // 清空变量
  fileList.value = []
  customFilename.value = ''
  uploadProgress.value = {};
  uploadDialogVisible.value = true
}

// 关闭上传对话框时清空变量
const handleUploadDialogClose = () => {
  fileList.value = []
  customFilename.value = ''
  uploadProgress.value = {};
}

// 文件选择变更
const handleFileChange = (file, uploadFileList) => {
  fileList.value = uploadFileList;
  const newProgress = {};
  for (const f of uploadFileList) {
    newProgress[f.uid] = { percentage: 0, speed: '' };
  }
  uploadProgress.value = newProgress;
}

const handleFileRemove = (file, uploadFileList) => {
  fileList.value = uploadFileList;
  const newProgress = { ...uploadProgress.value };
  delete newProgress[file.uid];
  uploadProgress.value = newProgress;
}

// 提交上传
const submitUpload = async () => {
  if (fileList.value.length === 0) {
    ElMessage.warning('请选择要上传的文件')
    return
  }
  // 双保险：上传中再点一次不该再传一遍（按钮在 loading 时本来就点不动）
  if (isUploading.value) return

  isUploading.value = true

  const failedFiles = []   // 传失败的文件，留在列表里方便重试
  let successCount = 0

  for (const file of fileList.value) {
    try {
      // 确保文件对象存在
      if (!file || !file.raw) {
        ElMessage.warning(`文件 ${file.name} 对象无效，已跳过`)
        continue
      }
      
      const formData = new FormData()
      formData.append('file', file.raw)
      
      // 只有当只有一个文件时，自定义文件名才生效
      if (fileList.value.length === 1 && customFilename.value.trim()) {
        formData.append('filename', customFilename.value.trim())
      }
      
      let lastLoaded = 0;
      let lastTime = Date.now();

      const response = await materialApi.uploadMaterial(formData, (progressEvent) => {
        const progressData = uploadProgress.value[file.uid];
        if (!progressData) return;

        const progress = Math.round((progressEvent.loaded * 100) / progressEvent.total)
        progressData.percentage = progress;

        const currentTime = Date.now();
        const timeDiff = (currentTime - lastTime) / 1000; // in seconds
        const loadedDiff = progressEvent.loaded - lastLoaded;

        if (timeDiff > 0.5) { // Update speed every 0.5 seconds
          const speed = loadedDiff / timeDiff; // bytes per second
          if (speed > 1024 * 1024) {
            progressData.speed = (speed / (1024 * 1024)).toFixed(2) + ' MB/s';
          } else {
            progressData.speed = (speed / 1024).toFixed(2) + ' KB/s';
          }
          lastLoaded = progressEvent.loaded;
          lastTime = currentTime;
        }
      })
      
      if (response.code === 200) {
        successCount += 1
        const progressData = uploadProgress.value[file.uid];
        if(progressData) progressData.speed = '完成';
      } else {
        failedFiles.push(file)
        ElMessage.error(`文件 ${file.name} 上传失败: ${response.msg || '未知错误'}`)
      }
    } catch (error) {
      console.error(`上传文件 ${file.name} 出错:`, error)
      failedFiles.push(file)
      ElMessage.error(`文件 ${file.name} 上传失败: ${error.message || '未知错误'}`)
    }
  }
  
  isUploading.value = false

  if (failedFiles.length === 0) {
    // 全部成功就关掉窗口：留着窗口+文件列表，用户再点一次「确认上传」会把同一批文件传第二遍
    ElMessage.success(`已上传 ${successCount} 个文件`)
    uploadDialogVisible.value = false
  } else {
    // 有失败的就把成功的从列表里摘掉，重试时不会把已传过的再传一遍
    fileList.value = failedFiles
    const keepProgress = {}
    for (const f of failedFiles) {
      keepProgress[f.uid] = { percentage: 0, speed: '' }
    }
    uploadProgress.value = keepProgress
    ElMessage.warning(`${successCount} 个成功，${failedFiles.length} 个失败；失败的文件已保留，可直接重试`)
  }

  // 逐个文件的成功/失败提示已经弹过了，这里静默刷新列表
  await fetchMaterials(true)
}

// 预览素材
const handlePreview = (material) => {
  currentMaterial.value = null
  previewError.value = ''
  previewLoading.value = true
  previewDialogVisible.value = true
  // 等对话框真正挂载后再塞数据，否则 <video> 首次渲染时拿不到 src
  nextTick(() => {
    currentMaterial.value = material
  })
}

const onMediaLoaded = () => {
  previewLoading.value = false
  previewError.value = ''
}

const onMediaError = () => {
  previewLoading.value = false
  previewError.value = '媒体加载失败：文件可能已被移动或删除，也可能格式不被浏览器支持。可点下方「下载文件」确认。'
}

// 删除素材
const handleDelete = (material) => {
  ElMessageBox.confirm(
    `确定要删除素材 ${material.filename} 吗？`,
    '警告',
    {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    }
  )
    .then(async () => {
      try {
        const response = await materialApi.deleteMaterial(material.id)

        if (response.code === 200) {
          appStore.removeMaterial(material.id)
          // 删掉的如果是当前页最后一条，页码会由 watch 自动夹回有效页
          ElMessage.success('删除成功')
        }
      } catch (error) {
        // 失败原因由请求拦截器统一提示
        console.error('删除素材出错:', error)
      }
    })
    .catch(() => {
      // 取消删除
    })
}

// 获取预览URL
const getPreviewUrl = (filePath) => {
  const filename = filePath.split('/').pop()
  return materialApi.getMaterialPreviewUrl(filename)
}

// 下载文件
const downloadFile = (material) => {
  const url = materialApi.downloadMaterial(material.file_path)
  window.open(url, '_blank')
}

// 判断文件类型。
// 优先用 file_path（磁盘名，一定带扩展名）——filename 是展示名，
// 用户可能把它改成不带后缀的标题，用 filename 判断会误判成"未知类型"。
const nameOf = (material) =>
  String(material?.file_path || material?.filename || material || '').toLowerCase()

const isVideoFile = (material) => {
  const videoExtensions = ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.m4v', '.webm']
  return videoExtensions.some(ext => nameOf(material).endsWith(ext))
}

const isImageFile = (material) => {
  const imageExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
  return imageExtensions.some(ext => nameOf(material).endsWith(ext))
}

// 组件挂载时获取素材列表
onMounted(() => {
  // 每次都拉一次：AI 文案可能在别的页面（发布中心）刚生成过，
  // 直接复用 store 里的旧数据会让「AI 文案」列显示成过期的"无"。
  // 静默刷新，不弹「刷新成功」。
  fetchMaterials(true)
})

onBeforeUnmount(() => {
  // 定时器不跟着组件一起走的话，离开页面后还会一直请求后端
  stopComposePolling()
  if (statusTimer) {
    clearInterval(statusTimer)
    statusTimer = null
  }
  stopMusicPreview()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.material-management {
  
  .page-header {
    margin-bottom: 20px;
    
    h1 {
      font-size: 24px;
      font-weight: 500;
      color: $text-primary;
      margin: 0;
    }
  }
  
  .material-list-container {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
    padding: 20px;
    
    .material-search {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 20px;
      
      .el-input {
        width: 300px;
      }
      
      .action-buttons {
        display: flex;
        gap: 10px;
      }
    }
    
    .material-list {
      margin-top: 20px;
    }

    .material-pagination {
      margin-top: 16px;
      display: flex;
      justify-content: flex-end;
    }

    .empty-data {
      padding: 40px 0;
    }
  }
  
  .material-upload {
    width: 100%;
  }
  
  .preview-container {
    display: flex;
    justify-content: center;
    align-items: center;
    flex-direction: column;
    padding: 0 20px;
    
    .file-info {
      text-align: center;
      margin-top: 20px;
    }
  }
}

/* AI 文案预览 */
.copy-preview {
  padding: 0 4px;

  .copy-field {
    display: flex;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px dashed #ebeef5;

    &:last-of-type {
      border-bottom: none;
    }
  }

  .copy-label {
    flex: none;
    width: 64px;
    color: #909399;
    font-size: 13px;
    line-height: 22px;
  }

  .copy-value {
    flex: 1;
    min-width: 0;
    color: $text-primary;
    font-size: 14px;
    line-height: 22px;
  }

  .copy-title {
    font-weight: 600;
  }

  /* 正文是唯一可能很长的字段，交给换行，不要撑破对话框 */
  .copy-desc {
    white-space: pre-wrap;
    word-break: break-word;
  }

  .copy-tag {
    margin: 0 6px 6px 0;
  }

  .copy-empty {
    color: #c0c4cc;
  }

  .copy-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid #ebeef5;
    color: #909399;
    font-size: 12px;
  }
}

.upload-form {
  padding: 0 20px;
  
  .form-tip {
    font-size: 12px;
    color: #909399;
    margin-top: 5px;
  }
  
  .upload-demo {
    width: 100%;
  }
}

.dialog-footer {
  padding: 0 20px;
  display: flex;
  justify-content: flex-end;
}

.upload-file-list {
  width: 100%;
}

.upload-file-item {
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  padding: 10px;
  margin-bottom: 10px;
}

.upload-file-item .file-name {
  font-size: 14px;
  color: #606266;
  margin-bottom: 5px;
  display: block;
}

/* 覆盖Element Plus对话框样式 */
:deep(.el-dialog__body) {
  padding: 20px 0;
}

:deep(.el-dialog__header) {
  padding-left: 20px;
  padding-right: 20px;
  margin-right: 0;
}

:deep(.el-dialog__footer) {
  padding-top: 10px;
  padding-bottom: 15px;
}

/* 修改上传进度条样式 */
:deep(.el-progress__text) {
  color: #303133 !important; /* 深灰色字体，确保在各种背景上都可见 */
  font-size: 12px;
}

:deep(.el-progress--line) {
  margin-bottom: 10px;
}

.upload-file-item {
  border: 1px solid #dcdfe6;
  border-radius: 6px; /* 增加圆角 */
  padding: 12px; /* 增加内边距 */
  margin-bottom: 12px; /* 增加外边距 */
  background-color: #fafafa; /* 轻微背景色 */
  transition: box-shadow 0.3s; /* 添加过渡效果 */
}

.upload-file-item:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1); /* 悬停效果 */
}

.upload-file-item .file-name {
  font-size: 14px;
  color: #303133; /* 深灰色字体 */
  margin-bottom: 8px; /* 增加底部间距 */
  display: block;
  font-weight: 500;
}

.preview-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid #ebeef5;
  color: #909399;
  font-size: 12px;
}

/* 合成背景音乐 */
.bgm-compose {
  padding: 0 4px;

  .bgm-material {
    margin: 14px 0 10px;
    font-size: 13px;
    color: $text-regular;
    word-break: break-all;
  }

  .bgm-picker {
    display: flex;
    gap: 10px;
    align-items: center;

    .bgm-select {
      flex: 1;
    }
  }

  /* 下拉里把时长放到右侧，选的时候能看出音乐长短 */
  .bgm-option-meta {
    float: right;
    color: #909399;
    font-size: 12px;
  }

  .bgm-hint {
    margin-top: 10px;
    font-size: 12px;
    color: #909399;
  }

  .bgm-progress {
    margin-top: 18px;

    .bgm-progress-text {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-top: 8px;
      font-size: 12px;
      color: #909399;
      word-break: break-all;
    }
  }
}

.mt12 {
  margin-top: 12px;
}
</style>
