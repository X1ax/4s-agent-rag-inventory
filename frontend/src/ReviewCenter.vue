<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessageBox, ElTabs, ElTabPane } from 'element-plus'
import { api, localTime, session } from './api'
import KnowledgeSubmissions from './KnowledgeSubmissions.vue'

const section = ref('purchase'), status = ref(session.user.role === 'storekeeper' ? 'not_submitted' : 'pending'), page = ref(1), total = ref(0), rows = ref([])
const loading = ref(false), busy = ref(false), error = ref(''), notice = ref(''), selected = ref(null), note = ref('')
const labels = { not_submitted: '待提交', pending: '待审核', approved: '审核通过', rejected: '已驳回', legacy: '历史业务' }
const states = { draft: '草稿', confirmed: '已确认', partial: '部分收货', completed: '已完成', sent: '已发出', received: '已收货', cancelled: '已取消' }
const kindLabel = computed(() => section.value === 'transfer' ? '调拨单' : '采购单')
const reviewer = computed(() => ['admin','manager'].includes(session.user.role))
const canSubmit = row => row.status === 'draft' && row.operator_id === session.user.id && (row.review_status || row.review?.status) === 'not_submitted'
const canReview = row => reviewer.value && row.status === 'draft' && row.operator_id !== session.user.id && (row.review_status || row.review?.status) === 'pending'
const permissions = [
  { name: '库存查询、统计与智能助手', admin: '可使用', storekeeper: '可使用', manager: '可使用' },
  { name: '采购/调拨制单、提交审核、入库及收发', admin: '可办理本人单据提交及业务登记', storekeeper: '可办理本人单据提交及业务登记', manager: '查看，不办理库存登记' },
  { name: '采购单、调拨单审核', admin: '可审核他人提交的单据', storekeeper: '不可审核', manager: '可审核他人提交的单据' },
  { name: '上传知识资料', admin: '可上传', storekeeper: '可上传', manager: '可上传' },
  { name: '知识资料审核与入库', admin: '可审核、驳回及重试索引', storekeeper: '仅查看本人上传记录', manager: '仅查看本人上传记录' },
  { name: '备件/供应商/仓库维护、账号权限与操作审计', admin: '可管理', storekeeper: '基础资料只读，无账号/审计权限', manager: '基础资料只读，无账号/审计权限' },
]
let revision = 0
async function load() {
  if (!['purchase','transfer'].includes(section.value)) return
  const current = ++revision
  loading.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ kind: section.value, status: status.value || '', page: page.value })
    const result = await api(`/reviews/documents?${params}`)
    if (current === revision) { rows.value = result.items; total.value = result.total }
  } catch (e) { if (current === revision) { rows.value = []; total.value = 0; error.value = e.message } }
  finally { if (current === revision) loading.value = false }
}
async function inspect(row) {
  error.value = ''; note.value = ''; selected.value = null
  try { selected.value = { ...await api(`/documents/${row.id}`), warehouse: row.warehouse, target_warehouse: row.target_warehouse, operator: row.operator } }
  catch (e) { error.value = e.message }
}
async function submit(row) {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await api(`/reviews/documents/${row.id}/submit`, { method: 'POST' })
    notice.value = '已提交审核。通过后才能确认采购或发出调拨。'
    selected.value = null; await load()
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
async function decide(approved) {
  if (busy.value || !selected.value) return
  if (!approved && !note.value.trim()) { error.value = '驳回时请填写具体审核意见'; return }
  try { await ElMessageBox.confirm(`${approved ? '通过' : '驳回'}${kindLabel.value} #${selected.value.id}？审核不会自动确认采购或执行调拨收发。`, '核对审核结论', { confirmButtonText: '确认审核', cancelButtonText: '返回核对' }) }
  catch { return }
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await api(`/reviews/documents/${selected.value.id}/decision`, { method: 'POST', body: JSON.stringify({ approved, note: note.value.trim() }) })
    notice.value = approved ? '审核通过，等待经办人员继续办理。' : '已驳回。创建人可取消草稿并按意见重新制单。'
    selected.value = null; await load()
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
watch(section, () => { revision++; rows.value = []; selected.value = null; page.value = 1; notice.value = ''; load() })
onMounted(load)
</script>

<template>
  <section class="panel review-center">
    <p class="muted">采购与调拨由其他管理员或经理审核；知识资料由管理员审核。业务审核与单据执行分开，审核不改变库存。</p>
    <el-tabs v-model="section">
      <el-tab-pane label="采购单审核" name="purchase" /><el-tab-pane label="调拨单审核" name="transfer" />
      <el-tab-pane label="知识资料审核" name="knowledge" /><el-tab-pane label="角色权限" name="permissions" />
    </el-tabs>
    <template v-if="['purchase','transfer'].includes(section)">
      <div v-if="error" class="error-banner" role="alert">{{ error }}<el-button link @click="load">刷新状态</el-button></div>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
      <form class="filters" @submit.prevent="page = 1; load()"><el-select v-model="status" clearable :value-on-clear="''" aria-label="审核状态" placeholder="全部审核状态"><el-option v-for="(label, value) in labels" v-show="value !== 'legacy'" :key="value" :value="value" :label="label" /></el-select><el-button native-type="submit" :loading="loading">查询</el-button></form>
      <p class="muted">{{ reviewer ? '可查看业务审核记录；本人创建的单据须由其他审核人员处理。' : '仅显示本人创建的单据，可提交审核、查看结论；没有审核权限。' }}</p>
      <el-table :data="rows" v-loading="loading" empty-text="当前筛选下没有审核事项">
        <el-table-column label="单据" width="105"><template #default="{ row }"><el-button link type="primary" :disabled="busy" @click="inspect(row)">#{{ row.id }} 查看</el-button></template></el-table-column>
        <el-table-column prop="warehouse" label="业务仓库" min-width="130" /><el-table-column v-if="section === 'transfer'" prop="target_warehouse" label="目标仓库" min-width="130" />
        <el-table-column prop="operator" label="创建人" min-width="110" /><el-table-column prop="business_date" label="业务日期" width="120" />
        <el-table-column label="业务状态" width="110"><template #default="{ row }">{{ states[row.status] }}</template></el-table-column>
        <el-table-column label="审核状态" width="115"><template #default="{ row }"><el-tag :type="row.review_status === 'approved' ? 'success' : row.review_status === 'rejected' ? 'danger' : 'info'">{{ labels[row.review_status] }}</el-tag></template></el-table-column>
        <el-table-column prop="reviewer" label="审核人" min-width="100" /><el-table-column prop="review_note" label="审核意见" min-width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="130"><template #default="{ row }"><el-button v-if="canSubmit(row)" link type="primary" :loading="busy" @click="submit(row)">提交审核</el-button><el-button v-else-if="canReview(row)" link type="primary" @click="inspect(row)">核对与审核</el-button><span v-else class="muted">查看记录</span></template></el-table-column>
      </el-table>
      <div class="table-footer"><span>共 {{ total }} 条</span><el-pagination v-model:current-page="page" :page-size="10" :total="total" layout="prev, pager, next" @current-change="load" /></div>
      <p class="table-note">未提交的历史草稿同样需要审核。已有生效业务不补造审批记录；取消后的单据不可继续审核。驳回后保留原意见，取消并重新制单。</p>
    </template>
    <KnowledgeSubmissions v-else-if="section === 'knowledge'" review-mode />
    <template v-else><p class="muted">当前角色：{{ {admin:'管理员',storekeeper:'库管',manager:'经理'}[session.user.role] }}。下列权限由服务端校验，智能助手不能代替审核人员作出审核结论。</p><el-table :data="permissions"><el-table-column prop="name" label="事项" min-width="210" /><el-table-column prop="admin" label="管理员" min-width="190" /><el-table-column prop="storekeeper" label="库管" min-width="180" /><el-table-column prop="manager" label="经理" min-width="180" /></el-table></template>
    <el-dialog :model-value="!!selected" :title="selected ? `${kindLabel} #${selected.id} · 审核核对` : ''" width="min(900px, 94vw)" :close-on-click-modal="false" :before-close="done => { if (!busy) { selected = null; done() } }" @update:model-value="value => { if (!value && !busy) selected = null }">
      <template v-if="selected"><p>业务日期：{{ selected.business_date }} · 审核状态：{{ labels[selected.review.status] }} · 业务状态：{{ states[selected.status] }}</p>
        <p>创建人：{{ selected.operator }} · 业务仓库：{{ selected.warehouse }}<span v-if="section === 'transfer'"> → 目标仓库：{{ selected.target_warehouse }}</span></p>
        <el-table :data="selected.lines"><el-table-column prop="sku" label="SKU" min-width="105" /><el-table-column prop="name" label="备件" min-width="150" /><el-table-column prop="supplier" label="供应商" min-width="140" v-if="section === 'purchase'" /><el-table-column prop="quantity" label="数量/件" width="100" /><el-table-column v-if="section === 'purchase'" prop="unit_price" label="单价/元" width="110" /></el-table>
        <p>备注：{{ selected.note || '无' }}</p><p v-if="section === 'purchase'">采购合计：{{ selected.total }} 元</p>
        <p v-if="selected.review.reviewed_at" class="muted">审核时间：{{ localTime(selected.review.reviewed_at) }}；意见：{{ selected.review.note || '无' }}</p>
        <el-input v-if="canReview(selected)" v-model="note" type="textarea" :rows="3" maxlength="500" show-word-limit placeholder="审核意见（驳回时必填）" :disabled="busy" />
        <div v-if="error" class="error-banner" role="alert">{{ error }}</div>
      </template>
      <template #footer><el-button :disabled="busy" @click="selected = null">关闭</el-button><el-button v-if="selected && canSubmit(selected)" type="primary" :loading="busy" @click="submit(selected)">提交审核</el-button><template v-if="selected && canReview(selected)"><el-button type="danger" plain :disabled="busy" @click="decide(false)">驳回</el-button><el-button type="primary" :loading="busy" @click="decide(true)">审核通过</el-button></template></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.notice { color: #22754b; }.filters { margin: 20px 0 12px; }.filters .el-select { width: 200px; }
</style>
