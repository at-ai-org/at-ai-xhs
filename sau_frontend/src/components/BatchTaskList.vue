<template>
  <!--
    批量任务的逐条进度列表（at-ai-xhs 改造新增）。

    「素材管理」的批量生成文案 / 批量合成背景音乐各有一个弹窗，但里面这张清单长得
    一模一样：一条素材一行，右边一个状态标签，进行中再挂一条进度条，失败把原因写在
    下面。抽成一个组件，两边共用一份口径（状态文案、配色、空态），
    免得改了一边忘了另一边。

    组件是纯展示的：items 由父组件维护，这里不改任何数据。
  -->
  <div class="batch-task-list">
    <el-empty v-if="!items.length" :description="emptyText" :image-size="60" />

    <div v-for="item in items" :key="item.key" class="batch-task-item">
      <div class="batch-task-head">
        <span class="batch-task-name" :title="item.name">{{ item.name }}</span>
        <el-tag size="small" :type="tagType(item.state)" effect="plain">
          {{ stateText(item.state) }}
        </el-tag>
      </div>

      <!-- 只有"正在跑"的才画进度条：等待中画一条 0% 的灰条只会让人以为卡住了 -->
      <el-progress
        v-if="item.state === 'running'"
        class="batch-task-bar"
        :percentage="percentOf(item)"
        :stroke-width="12"
        :text-inside="true"
      />

      <div v-if="item.message" class="batch-task-msg" :class="{ 'is-error': item.state === 'failed' }">
        {{ item.message }}
      </div>
    </div>
  </div>
</template>

<script setup>
// 状态 → 文案 / 配色。父组件只用这六个状态，口径写在这里一处。
const STATE_TEXT = {
  pending: '等待中',
  running: '进行中',
  done: '已完成',
  failed: '失败',
  skipped: '已跳过',
}

const STATE_TAG = {
  pending: 'info',
  running: 'warning',
  done: 'success',
  failed: 'danger',
  skipped: 'info',
}

const props = defineProps({
  /** [{ key, name, state, message, progress }] */
  items: {
    type: Array,
    default: () => [],
  },
  emptyText: {
    type: String,
    default: '没有要处理的任务',
  },
})

const stateText = (state) => STATE_TEXT[state] || state || '—'

const tagType = (state) => STATE_TAG[state] || 'info'

// 没结束前不显示 100%：完成是后端说的，不是进度条凑到的（和单个合成那条口径一致）
const percentOf = (item) => Math.max(0, Math.min(99, Number(item?.progress) || 0))
</script>

<style lang="scss" scoped>
.batch-task-list {
  max-height: 46vh;
  overflow-y: auto;
  padding-right: 4px;
}

.batch-task-item {
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  margin-bottom: 10px;
  background-color: #fafafa;

  &:last-child {
    margin-bottom: 0;
  }
}

.batch-task-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.batch-task-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.batch-task-bar {
  margin-top: 8px;
}

.batch-task-msg {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  word-break: break-all;

  &.is-error {
    color: #f56c6c;
  }
}
</style>
