<script setup>
import { onMounted, ref } from 'vue'
import { api, localTime } from './api'
const rows = ref([]), total = ref(0), page = ref(1), loading = ref(false), loadError = ref('')
const actions = { login: '登录', 'user.create': '创建账号', 'user.update': '修改账号权限', 'catalog.create': '新增资料', 'catalog.update': '修改资料', 'opening.post': '期初入账', 'demo.user.create': '初始化演示账号', 'demo.catalog.create': '导入模拟资料' }
Object.assign(actions, { 'catalog.image': '维护演示配图', 'purchase.create': '创建采购草稿', 'purchase.confirm': '确认采购', 'purchase.cancel': '取消未收余量', 'purchase.receipt': '生成采购入库单', 'purchase.receive': '登记实际收货', 'issue.create': '创建领用草稿', 'issue.post': '领用出库过账', 'issue.cancel': '取消领用草稿', 'transfer.create': '创建调拨草稿', 'transfer.send': '发出调拨', 'transfer.receive': '调拨收货', 'transfer.cancel': '取消调拨草稿' })
const entities = { users: '账号', parts: '备件', warehouses: '仓库', suppliers: '供应商', opening_documents: '期初单', documents: '业务单据' }
let version = 0
async function load() {
  const current = ++version
  loading.value = true
  try {
    const result = await api(`/audit?page=${page.value}&page_size=10`)
    if (current === version) { rows.value = result.items; total.value = result.total; loadError.value = '' }
  }
  catch (error) { if (current === version) { rows.value = []; total.value = 0; loadError.value = error.message } }
  finally { if (current === version) loading.value = false }
}
onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="toolbar"><p class="muted">追溯登录与已成功提交的业务变更。</p><el-button :loading="loading" @click="load">刷新记录</el-button></div>
    <div v-if="loadError" class="error-banner" role="alert">审计查询失败：{{ loadError }}。旧结果已清除。<el-button link :loading="loading" @click="load">重试查询</el-button></div>
    <el-table :data="rows" v-loading="loading" row-key="id" :empty-text="loadError ? '记录加载失败，请重试' : '暂无操作记录'">
      <el-table-column prop="id" label="记录" width="85" />
      <el-table-column prop="operator" label="操作人" min-width="150" />
      <el-table-column label="动作" min-width="160"><template #default="{ row }">{{ actions[row.action.split(':')[0]] || row.action }}</template></el-table-column>
      <el-table-column label="业务对象" min-width="140"><template #default="{ row }">{{ entities[row.entity] || row.entity }} #{{ row.entity_id }}</template></el-table-column>
      <el-table-column label="结果" width="100"><template #default><el-tag type="success">成功</el-tag></template></el-table-column>
      <el-table-column label="时间" min-width="190"><template #default="{ row }">{{ localTime(row.created_at) }}</template></el-table-column>
    </el-table>
    <div class="table-footer"><span>共 {{ total }} 条记录</span><el-pagination v-model:current-page="page" :page-size="10" :total="total" layout="prev, pager, next" @current-change="load" /></div>
    <p class="table-note">本阶段记录成功操作，失败请求整体回滚；尚未建设独立的安全事件日志。时间显示为本机时区。</p>
  </section>
</template>
