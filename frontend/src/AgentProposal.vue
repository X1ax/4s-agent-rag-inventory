<script setup>
import { computed, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api, session } from './api'
import { proposalRows } from './proposalRows.js'
const props = defineProps({ proposal: Object, threadId: String, disabled: Boolean })
const emit = defineEmits(['changed', 'open-document', 'choose-action'])
const busy = ref(false), error = ref(''), issue = ref(null)
const verbs = { create: '创建草稿', confirm: '确认采购', post: '领用过账', send: '发出调拨', receive: '调拨收货', cancel: '取消未执行部分', receipt: '实际收货入库' }
const kinds = { purchase: '采购单', issue: '领用单', transfer: '调拨单', receipt: '入库单' }
const statuses = { pending: '待你核对确认', executed: '已执行', rejected: '已拒绝', superseded: '已由新提案替代' }
const rows = computed(() => proposalRows(props.proposal))
const canConfirm = computed(() => props.proposal.status === 'pending' && ['admin','storekeeper'].includes(session.user.role) && !props.disabled && !busy.value)
async function decide(approved) {
  if (approved) {
    try { await ElMessageBox.confirm(`请核对下面的仓库、备件、数量、供应商、金额和业务日期。本次仅执行“${verbs[props.proposal.action]}”，不会自动办理后续步骤。`, '确认具体业务动作', { confirmButtonText: '确认执行', cancelButtonText: '返回检查', type: 'warning' }) }
    catch { return }
  }
  busy.value = true; error.value = ''
  try {
    await api(`/assistant/threads/${props.threadId}/proposals/${props.proposal.id}/decision`, { method: 'POST', timeout: 30000,
      body: JSON.stringify({ version: props.proposal.version, approved }) })
    emit('changed')
  } catch (e) { issue.value = e.businessIssue || null; error.value = issue.value ? e.message : `${e.message}。可刷新会话核对状态；结果不明时重试此提案，系统会防止重复执行。` }
  finally { busy.value = false }
}
</script>
<template>
  <section class="proposal-card">
    <div class="proposal-title"><strong>{{ verbs[proposal.action] }} · {{ kinds[proposal.summary.kind] }}</strong><el-tag :type="proposal.status === 'executed' ? 'success' : 'info'">{{ statuses[proposal.status] || proposal.status }}</el-tag></div>
    <p>业务日期：{{ proposal.payload.business_date }}<span v-if="proposal.payload.document_id"> · 来源单据 #{{ proposal.payload.document_id }}</span></p>
    <p>仓库：{{ proposal.summary.warehouses.find(w => w.id === proposal.payload.warehouse_id)?.name || proposal.summary.warehouses[0]?.name }}<span v-if="proposal.payload.target_warehouse_id"> → {{ proposal.summary.warehouses.find(w => w.id === proposal.payload.target_warehouse_id)?.name }}</span></p>
    <el-table :data="rows" size="small"><el-table-column prop="name" label="备件" min-width="130" /><el-table-column prop="sku" label="SKU" min-width="100" /><el-table-column prop="quantity" label="数量/件" width="85" /><el-table-column v-if="proposal.summary.kind === 'purchase'" prop="supplier" label="供应商" min-width="130" /><el-table-column v-if="proposal.summary.kind === 'purchase'" prop="unit_price" label="单价/元" width="95" /></el-table>
    <p v-if="proposal.payload.note">用途/备注：{{ proposal.payload.note }}</p>
    <p class="muted">{{ proposal.action === 'create' ? '仅创建草稿，不改变库存；采购确认、实收、过账、调拨发出及收货分别审核。' : '确认的是该项具体状态变化；实际到货、验收和交接事实需由你核对。' }} 提案有效期30分钟；参数修改请在聊天中提出，新提案会使旧提案失效。</p>
    <div v-if="error" class="error-banner" role="alert">{{ error }}</div>
    <el-button v-for="option in issue?.options || []" :key="option.label" @click="emit('choose-action', option.prompt)">{{ option.label }}</el-button>
    <div class="proposal-actions"><template v-if="proposal.status === 'pending'"><el-button type="primary" :disabled="!canConfirm" :loading="busy" @click="decide(true)">核对并确认</el-button><el-button :disabled="!canConfirm" @click="decide(false)">拒绝此提案</el-button></template><el-button v-if="proposal.result" link type="primary" @click="emit('open-document', { kind: proposal.action === 'receipt' ? 'receipt' : proposal.summary.kind, id: proposal.result.id })">查看业务单据 #{{ proposal.result.id }}</el-button></div>
  </section>
</template>
<style scoped>
.proposal-card { margin-top: 20px; border: 1px solid #dbe5f2; border-radius: 14px; padding: 20px; background: #fbfdff; }
.proposal-title, .proposal-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.proposal-card p { line-height: 1.8; font-size: 13px; }
</style>
