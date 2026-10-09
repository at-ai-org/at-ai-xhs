<template>
  <span class="account-stats-trigger">
    <el-button size="small" type="success" :icon="DataAnalysis" @click="open">
      详情
    </el-button>
    <AccountStatsDialog v-model="visible" :account="account" />
  </span>
</template>

<script setup>
/**
 * 「详情」按钮 + 账号数据弹窗（at-ai-xhs 改造新增）。
 *
 * **为什么单独做成一个组件**：`AccountManagement.vue` 里「全部 / 快手 / 抖音 / 视频号 /
 * 小红书」五个 tab 各有一张自己的账号表（那是上游就有的重复结构），如果按钮和弹窗
 * 直接写在模板里，同一段逻辑要抄五遍——改一处漏四处几乎必然发生。这里把"按钮 + 弹窗"
 * 收成一个整体，五个 tab 各放一个 `<AccountStatsButton :account="scope.row" />` 就行，
 * 弹窗状态也自然跟着每一行独立。
 *
 * 弹窗组件是**懒加载**的（`defineAsyncComponent`）：它只在用户真的点开时才需要，
 * 而账号管理页本身已经很重（五个 tab 的表格）。不做懒加载的话，首屏会多下载一份
 * 用不到几百行的组件。
 */
import { defineAsyncComponent, ref } from 'vue'
import { DataAnalysis } from '@element-plus/icons-vue'

const AccountStatsDialog = defineAsyncComponent(() => import('./AccountStatsDialog.vue'))

const props = defineProps({
  /** 账号列表那一行：`{ id, name, platform, type, ... }` */
  account: { type: Object, required: true }
})

const visible = ref(false)

function open() {
  visible.value = true
}
</script>

<style scoped>
.account-stats-trigger {
  display: inline-block;
}
</style>
