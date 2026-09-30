<template>
  <div class="background-music">
    <div class="page-header">
      <h1>背景音乐</h1>
      <p class="page-desc">
        上传自己的背景音乐，之后到「素材管理」点「合成背景音乐」，就能把视频的<strong>原音轨换成</strong>这首音乐
        （音乐比视频短会自动循环播放；成品文件名会带上音乐名，一眼看出用的是哪首）。
      </p>
    </div>

    <div class="music-container">
      <div class="music-toolbar">
        <el-input
          v-model="searchKeyword"
          placeholder="输入音乐名称搜索"
          prefix-icon="Search"
          clearable
          class="music-search"
        />
        <div class="action-buttons">
          <el-button type="primary" @click="openUpload">上传音乐</el-button>
          <el-button type="info" :loading="isLoading" @click="fetchMusic">
            <el-icon v-if="!isLoading"><Refresh /></el-icon>
            <span v-if="isLoading">刷新中</span>
          </el-button>
        </div>
      </div>

      <el-table :data="filteredMusic" v-loading="isLoading" style="width: 100%">
        <el-table-column prop="name" label="音乐名称" min-width="220" show-overflow-tooltip />
        <el-table-column label="时长" width="110" align="center">
          <template #default="scope">{{ formatDuration(scope.row.duration) }}</template>
        </el-table-column>
        <el-table-column label="文件大小" width="120">
          <template #default="scope">{{ scope.row.filesize }} MB</template>
        </el-table-column>
        <el-table-column prop="upload_time" label="上传时间（本地）" width="180" />
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="scope">
            <el-button size="small" @click="togglePlay(scope.row)">
              {{ playingId === scope.row.id ? '停止' : '试听' }}
            </el-button>
            <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="searchKeyword ? '没有匹配的音乐' : '还没有背景音乐，点右上角「上传音乐」'" />
        </template>
      </el-table>

      <!-- 试听共用一个 audio：每行放一个 <audio> 既费资源，又要自己处理"同时播放" -->
      <audio ref="audioRef" preload="none" style="display: none" @ended="stopPlay" @error="onAudioError" />
    </div>

    <!-- 上传对话框 -->
    <el-dialog v-model="uploadDialogVisible" title="上传背景音乐" width="40%" @close="resetUpload">
      <div class="upload-form">
        <el-form label-width="90px">
          <el-form-item label="音乐名称:">
            <el-input
              v-model="customName"
              placeholder="选填 (仅单个文件时生效，默认用文件名)"
              :disabled="fileList.length > 1"
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
              <div class="el-upload__text">将音乐文件拖到此处，或<em>点击上传</em></div>
              <template #tip>
                <div class="el-upload__tip">
                  支持 {{ ALLOWED_EXT_TEXT }}；可一次选多个。上传后会立刻校验有没有音轨。
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
                />
              </div>
            </div>
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="uploadDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="isUploading" @click="submitUpload">
            {{ isUploading ? '上传中' : '确认上传' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Refresh, Upload } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { musicApi } from '@/api/music'

// 与后端 ALLOWED_EXTS 保持一致（myUtils/background_music.py）
const ALLOWED_EXT_TEXT = 'mp3 / wav / m4a / aac / flac / ogg / opus / wma'

const musics = ref([])
const isLoading = ref(false)
const searchKeyword = ref('')

// ---- 试听 ------------------------------------------------------------------
const audioRef = ref(null)
const playingId = ref(null)

const filteredMusic = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  if (!keyword) return musics.value
  return musics.value.filter((m) => String(m.name || '').toLowerCase().includes(keyword))
})

const formatDuration = (seconds) => {
  const total = Math.round(Number(seconds) || 0)
  if (!total) return '—'
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

const stopPlay = () => {
  const el = audioRef.value
  if (el) {
    el.pause()
    el.removeAttribute('src')
    el.load()
  }
  playingId.value = null
}

const togglePlay = (row) => {
  if (playingId.value === row.id) {
    stopPlay()
    return
  }
  const el = audioRef.value
  if (!el) return
  stopPlay()
  el.src = musicApi.fileUrl(row.id)
  playingId.value = row.id
  // play() 返回 Promise：浏览器拦截自动播放时会 reject，要接住，否则控制台一片红
  const played = el.play()
  if (played && typeof played.catch === 'function') {
    played.catch(() => {
      playingId.value = null
      ElMessage.warning('浏览器没能开始播放，请再点一次「试听」')
    })
  }
}

const onAudioError = () => {
  playingId.value = null
  ElMessage.error('试听失败：音乐文件可能已被删除，或格式不被浏览器支持')
}

// ---- 列表 ------------------------------------------------------------------
const fetchMusic = async () => {
  isLoading.value = true
  try {
    const res = await musicApi.list()
    if (res.code === 200) musics.value = res.data || []
  } catch (error) {
    console.error('获取背景音乐列表出错:', error)
  } finally {
    isLoading.value = false
  }
}

const handleDelete = (row) => {
  ElMessageBox.confirm(
    `确定要删除背景音乐「${row.name}」吗？（已经合成过的素材不受影响）`,
    '警告',
    { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' }
  )
    .then(async () => {
      if (playingId.value === row.id) stopPlay()
      const res = await musicApi.remove(row.id)
      if (res.code === 200) {
        ElMessage.success('已删除')
        await fetchMusic()
      }
    })
    .catch(() => {
      /* 取消删除 */
    })
}

// ---- 上传 ------------------------------------------------------------------
const uploadDialogVisible = ref(false)
const fileList = ref([])
const customName = ref('')
const uploadProgress = ref({})
const isUploading = ref(false)

const openUpload = () => {
  resetUpload()
  uploadDialogVisible.value = true
}

function resetUpload() {
  fileList.value = []
  customName.value = ''
  uploadProgress.value = {}
}

const handleFileChange = (file, uploadFileList) => {
  fileList.value = uploadFileList
  const next = {}
  for (const f of uploadFileList) {
    next[f.uid] = { percentage: 0 }
  }
  uploadProgress.value = next
}

const handleFileRemove = (file, uploadFileList) => {
  fileList.value = uploadFileList
  const next = { ...uploadProgress.value }
  delete next[file.uid]
  uploadProgress.value = next
}

const submitUpload = async () => {
  if (!fileList.value.length) {
    ElMessage.warning('请选择要上传的音乐文件')
    return
  }
  // 双保险：上传中再点一次不该再传一遍（按钮 loading 时本来就点不动）
  if (isUploading.value) return
  isUploading.value = true

  const failedFiles = []
  let successCount = 0

  for (const file of fileList.value) {
    try {
      if (!file || !file.raw) {
        ElMessage.warning(`文件 ${file.name} 对象无效，已跳过`)
        continue
      }
      const formData = new FormData()
      formData.append('file', file.raw)
      // 自定义名称只在单个文件时生效（多文件时不知道对应哪一个）
      if (fileList.value.length === 1 && customName.value.trim()) {
        formData.append('name', customName.value.trim())
      }

      const res = await musicApi.upload(formData, (progressEvent) => {
        const progressData = uploadProgress.value[file.uid]
        if (!progressData || !progressEvent.total) return
        progressData.percentage = Math.round((progressEvent.loaded * 100) / progressEvent.total)
      })

      if (res.code === 200) successCount += 1
      else failedFiles.push(file)
    } catch (error) {
      // 具体原因（格式不支持、没有音轨…）由请求拦截器提示
      console.error(`上传音乐 ${file.name} 出错:`, error)
      failedFiles.push(file)
    }
  }

  isUploading.value = false

  if (!failedFiles.length) {
    ElMessage.success(`已上传 ${successCount} 首音乐`)
    uploadDialogVisible.value = false
  } else {
    // 失败的留在列表里方便重试，成功的摘掉（否则重试会把已传过的再传一遍）
    fileList.value = failedFiles
    const keep = {}
    for (const f of failedFiles) keep[f.uid] = { percentage: 0 }
    uploadProgress.value = keep
    ElMessage.warning(`${successCount} 首成功，${failedFiles.length} 首失败；失败的可直接重试`)
  }

  await fetchMusic()
}

onMounted(() => {
  fetchMusic()
})

onBeforeUnmount(() => {
  stopPlay()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.background-music {
  .page-header {
    margin-bottom: 20px;

    h1 {
      font-size: 24px;
      font-weight: 500;
      color: $text-primary;
      margin: 0 0 8px;
    }

    .page-desc {
      margin: 0;
      font-size: 13px;
      line-height: 1.7;
      color: $text-secondary;
      max-width: 900px;
    }
  }

  .music-container {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
    padding: 20px;

    .music-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 20px;

      .music-search {
        width: 300px;
      }

      .action-buttons {
        display: flex;
        gap: 10px;
      }
    }
  }
}

.upload-form {
  padding: 0 20px;

  .upload-demo {
    width: 100%;
  }
}

.upload-file-list {
  width: 100%;
}

.upload-file-item {
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  padding: 12px;
  margin-bottom: 12px;
  background-color: #fafafa;

  .file-name {
    font-size: 14px;
    color: #303133;
    font-weight: 500;
    display: block;
    margin-bottom: 8px;
  }
}

.dialog-footer {
  padding: 0 20px;
  display: flex;
  justify-content: flex-end;
}

:deep(.el-dialog__body) {
  padding: 20px 0;
}

:deep(.el-progress__text) {
  color: #303133 !important;
  font-size: 12px;
}
</style>
