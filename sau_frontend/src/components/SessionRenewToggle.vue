<template>
  <div class="session-renew" :class="{ 'with-times': showTimes }">
    <!--
      「定时续期」开关（at-ai-xhs 改造新增）。
      同一个组件被放在两处：账号管理页「添加账号」旁边，以及设置页的「账号会话续期」卡片。
      状态是共享的（composables/useSessionRenew.js），所以一处点了另一处跟着变。
    -->
    <el-tooltip :content="renewSummary" placement="top">
      <el-button
        :type="state.enabled ? 'success' : 'info'"
        :plain="!state.enabled"
        :icon="Timer"
        :loading="state.saving || state.loading"
        @click="onToggle"
      >
        定时续期：{{ state.enabled ? '开启' : '关闭' }}
      </el-button>
    </el-tooltip>

    <div v-if="showTimes" class="renew-body">
      <div class="renew-line">
        <span class="line-label">每天续期时间点</span>
        <el-tag
          v-for="time in draft"
          :key="time"
          class="time-tag"
          closable
          @close="removeTime(time)"
        >
          {{ time }}
        </el-tag>
        <span v-if="!draft.length" class="empty-tip">还没有时间点，续期不会执行</span>
      </div>

      <div class="renew-line">
        <el-time-picker
          v-model="newTime"
          format="HH:mm"
          value-format="HH:mm"
          placeholder="选择时间"
          :clearable="false"
          style="width: 140px"
        />
        <el-button :disabled="!canAdd" @click="addTime">添加</el-button>
        <el-button type="primary" :loading="state.saving" :disabled="!dirty" @click="saveTimes">
          保存时间点
        </el-button>
        <el-button text @click="restoreDefault">恢复默认</el-button>
        <span v-if="dirty" class="dirty-tip">有改动还没保存</span>
      </div>

      <div class="renew-note">
        到点后后端会依次给「账号管理」里的每个账号跑一次登录态校验（顺带把平台轮换下来的
        新票据写回 cookie 文件），<strong>并顺手把这个账号的粉丝 / 互动 / 作品数据同步一份到本地库</strong>
        （账号详情页看到的就是它）。一个账号几十秒，整轮通常几分钟。某个账号那一刻正在
        发布 / 校验中会自动跳过，10 分钟后再补一次。要停就关掉开关——别把时间点删空
        （空时间点等于开关显示"开启"却一个点都不跑）。
        <template v-if="lastRunText">
          <br>
          上次续期：{{ lastRunText }}
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { Timer } from '@element-plus/icons-vue'
import { useSessionRenew } from '@/composables/useSessionRenew'

// 模板里用 showTimes 控制"只显示开关"还是"连时间点编辑一起显示"
const props = defineProps({
  /** 是否显示时间点编辑（账号管理页只要那个开关，设置页要全套） */
  showTimes: { type: Boolean, default: false }
})

const { state, loadSessionRenew, setSessionRenewEnabled, saveSessionRenewTimes, renewSummary, lastRunText } =
  useSessionRenew()

// 时间点是"改完再保存"的（不像开关点了就生效）：删一个点就立刻落盘太毛躁，
// 而且后端每个点都要开一轮浏览器，值得让人确认一下。
const DEFAULT_TIMES = ['00:00', '07:00', '14:00', '20:00']
const draft = ref([])
const newTime = ref('')
// 用户动过 draft 没有。用它（而不是"draft 和 state 是否相同"）来决定要不要接收后端的新值：
// 两者判等的话，**首次拉取**（draft 还是空、state 已经有 4 个点）会被误判成"用户改过"，
// 结果草稿永远是空的、界面显示"还没有时间点"。
const touched = ref(false)

const dirty = computed(
  () => [...draft.value].sort().join(',') !== [...state.times].sort().join(',')
)
const canAdd = computed(() => !!newTime.value && !draft.value.includes(newTime.value))

watch(
  () => state.times,
  (times) => {
    // 用户正在编的列表别被覆盖（比如另一个页面保存了一次）
    if (!touched.value) draft.value = [...times]
  },
  { immediate: true, deep: true }
)

function addTime() {
  if (!canAdd.value) return
  draft.value = [...draft.value, newTime.value].sort()
  newTime.value = ''
  touched.value = true
}

function removeTime(time) {
  draft.value = draft.value.filter((t) => t !== time)
  touched.value = true
}

function restoreDefault() {
  draft.value = [...DEFAULT_TIMES]
  touched.value = true
}

async function saveTimes() {
  if (!draft.value.length) return
  // 保存成功后 state.times 会变成刚存的值，先松开"用户改过"的挡板，
  // 后面的 watch 才能把草稿同步成后端归一化后的结果（例如 7:00 → 07:00）。
  touched.value = false
  const ok = await saveSessionRenewTimes(draft.value)
  if (!ok) touched.value = true
}

async function onToggle() {
  await setSessionRenewEnabled(!state.enabled)
}

// 进页面时读一次状态。
// 设置页（showTimes）**每次都重新拉**：那里会显示"上次续期"的结果，隔了很久再打开
// 该看到最新的。账号管理页不强制——它一个页面里有 5 份这个按钮，共享状态只拉一次就够了。
loadSessionRenew(props.showTimes)
</script>

<style scoped>
.session-renew {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 设置页里是"卡片里的一个区块"，竖着排；账号管理页里就是工具栏上的一个按钮 */
.session-renew.with-times {
  display: block;
}

.renew-body {
  margin-top: 16px;
}

.renew-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.line-label {
  font-size: 13px;
  color: #606266;
}

.time-tag {
  font-variant-numeric: tabular-nums;
}

.empty-tip,
.dirty-tip {
  font-size: 12px;
  color: #e6a23c;
}

.renew-note {
  font-size: 12px;
  line-height: 1.7;
  color: #909399;
}
</style>
