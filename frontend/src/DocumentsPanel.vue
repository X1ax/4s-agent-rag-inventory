<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, localTime, session } from './api'
import { imageFor } from './media'
import { purchaseLineCents, formatPurchaseAmount } from './purchaseAmounts'
import { activeQuote, documentLines, moneyText } from './pricing'

const props = defineProps({ kind: String, parts: Array, suppliers: Array, warehouses: Array, focusDocument: Number })
const emit = defineEmits(['open-reviews'])
const reviewLabels = { not_submitted: '待提交审核', pending: '待审核', approved: '审核通过', rejected: '已驳回', legacy: '历史业务 · 未补造审核记录' }
const labels = { purchase: '采购单', receipt: '入库单', issue: '领用出库单', transfer: '调拨单' }
const states = { draft: '草稿', confirmed: '已确认', partial: '部分收货', completed: '已完成', posted: '已过账', sent: '已发出 / 在途', received: '已收货', cancelled: '已取消余量 / 草稿' }
const verbs = { confirm: '确认采购', post: '确认出库', send: '发出调拨', receive: '调拨收货', cancel: '取消未执行部分' }
const rows = ref([]), total = ref(0), page = ref(1), status = ref(''), warehouse = ref(''), loading = ref(false), loadError = ref('')
const detailError = ref(''), detailId = ref(null)
const creating = ref(false), selected = ref(null), receiving = ref(false), busy = ref(false), pending = ref(null)
const receiptLines = ref([]), receiptDate = ref(''), receiptNote = ref(''), actionDate = ref('')
const form = reactive({})
const pricing = ref([]), pricingError = ref(''), priceLoading = ref(false), operationError = ref('')
const pricingPart = id => pricing.value.find(item => item.part_id === id)
const lineQuote = line => activeQuote(pricingPart(line.part_id), line.supplier_id)
const salePrice = line => form.issue_type === 'warranty' ? '0.00' : pricingPart(line.part_id)?.sale_price
const issueLabels = { paid_repair: '收费维修', warranty: '质保维修' }
let priceVersion = 0
async function loadPricing() {
  const current = ++priceVersion
  priceLoading.value = true
  try { const result = await api('/pricing/parts'); if (current === priceVersion) { pricing.value = result.items; pricingError.value = '' } }
  catch (error) { if (current === priceVersion) { pricing.value = []; pricingError.value = error.message } }
  finally { if (current === priceVersion) priceLoading.value = false }
}
const canWrite = computed(() => ['admin', 'storekeeper'].includes(session.user.role))
const availableStates = computed(() => props.kind === 'purchase' ? ['draft', 'confirmed', 'partial', 'completed', 'cancelled'] : props.kind === 'transfer' ? ['draft', 'sent', 'received', 'cancelled'] : props.kind === 'issue' ? ['draft', 'posted', 'cancelled'] : ['posted'])
const allowed = computed(() => {
  const row = selected.value
  if (!row || !canWrite.value) return []
  if (row.status === 'draft') return [...(row.kind === 'issue' || row.review?.status === 'approved' ? [{ verb: { purchase: 'confirm', issue: 'post', transfer: 'send' }[row.kind] }] : []), { verb: 'cancel' }]
  if (row.kind === 'purchase' && ['confirmed', 'partial'].includes(row.status)) return [{ verb: 'receipt' }, { verb: 'cancel' }]
  if (row.kind === 'transfer' && row.status === 'sent') return [{ verb: 'receive' }]
  return []
})
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10)
let version = 0, detailVersion = 0
async function load() {
  const current = ++version
  loading.value = true
  try {
    const params = new URLSearchParams({ kind: props.kind, page: page.value, page_size: 10 })
    if (status.value) params.set('status', status.value)
    if (warehouse.value) params.set('warehouse_id', warehouse.value)
    const result = await api(`/documents?${params}`)
    if (current === version) { rows.value = result.items; total.value = result.total; loadError.value = '' }
  } catch (error) {
    if (current === version) { rows.value = []; total.value = 0; loadError.value = error.message }
  }
  finally { if (current === version) loading.value = false }
}
function search() { page.value = 1; load() }
function openCreate() {
  Object.keys(form).forEach(key => delete form[key])
  Object.assign(form, { kind: props.kind, warehouse_id: null, target_warehouse_id: null, business_date: today(), note: '', lines: [newLine()],
    ...(props.kind === 'issue' ? { issue_type: null, warranty_reason: '', work_order: '' } : {}) })
  operationError.value = ''
  if (['purchase', 'issue'].includes(props.kind)) loadPricing()
  creating.value = true
}
const newLine = () => ({ part_id: null, supplier_id: null, quantity: 1, unit_price: null, quote_version: null })
function partsFor(line) {
  const supplier = props.suppliers.find(item => item.id === line.supplier_id && item.active)
  return props.parts.filter(part => part.active && (props.kind !== 'purchase' || (supplier?.supply_categories?.includes(part.category) && activeQuote(pricingPart(part.id), line.supplier_id))))
}
function changeSupplier(line) {
  line.part_id = null
  line.unit_price = null; line.quote_version = null
}
function changePart(line) {
  const quote = lineQuote(line)
  line.unit_price = quote?.supply_price ?? null
  line.quote_version = quote?.version ?? null
}
const lineAmount = line => line.part_id ? purchaseLineCents(line.unit_price, line.quantity) : null
const purchaseTotal = computed(() => {
  let total = 0n
  for (const line of form.lines || []) {
    const amount = lineAmount(line)
    if (amount === null) return null
    total += amount
  }
  return total
})
async function openDetail(id) {
  const current = ++detailVersion
  detailId.value = id
  try {
    const result = await api(`/documents/${id}`)
    if (current === detailVersion) { selected.value = result; actionDate.value = today(); detailError.value = '' }
  }
  catch (error) { if (current === detailVersion) { selected.value = null; detailError.value = error.message } }
}
async function execute(path, body, mode) {
  if (busy.value) return
  pending.value ||= { path, body: JSON.parse(JSON.stringify(body)), mode }
  const job = pending.value
  busy.value = true
  try {
    const result = await api(job.path, { method: 'POST', body: JSON.stringify(job.body) })
    pending.value = null
    operationError.value = ''
    ElMessage.success(`${labels[job.mode === 'receipt' ? 'receipt' : props.kind]} #${result.id} 操作成功${result.replayed ? '（重复请求已恢复）' : ''}`)
    creating.value = false; receiving.value = false
    await load()
    await openDetail(job.mode === 'receipt' && selected.value ? selected.value.id : result.id)
  } catch (error) {
    if (error.status >= 400 && error.status < 500) pending.value = null
    operationError.value = error.message
  } finally { busy.value = false }
}
async function saveDraft() {
  if (!form.warehouse_id || form.lines.some(line => !line.part_id || !Number.isInteger(line.quantity) || line.quantity < 1)) { ElMessage.warning('请选择仓库、备件并填写正整数数量'); return }
  if (props.kind === 'purchase' && form.lines.some(line => !line.supplier_id)) { ElMessage.warning('请为每条采购明细选择有供货资格的供应商'); return }
  if (props.kind === 'purchase' && (purchaseTotal.value === null || form.lines.some(line => !lineQuote(line) || line.quote_version !== lineQuote(line).version))) { operationError.value = '采购明细缺少有效供应商报价，请重新查询价格并选择备件'; return }
  if (props.kind === 'issue' && !['paid_repair', 'warranty'].includes(form.issue_type)) { operationError.value = '请选择收费维修或质保维修'; return }
  if (props.kind === 'issue' && form.issue_type === 'warranty' && !form.warranty_reason.trim()) { operationError.value = '质保维修必须填写说明'; return }
  if (props.kind === 'issue' && form.issue_type === 'paid_repair' && form.lines.some(line => !purchaseLineCents(salePrice(line), 1))) { operationError.value = '所选备件尚无有效门店定价，请先由管理员或经理维护'; return }
  const lines = documentLines(props.kind, form.lines)
  await execute('/documents', { ...form, lines, request_id: crypto.randomUUID() }, 'create')
}
async function act(verb) {
  if (verb === 'receipt') {
    receiptDate.value = today(); receiptNote.value = ''
    receiptLines.value = selected.value.lines.filter(line => line.quantity > line.received_quantity).map(line => ({ ...line, receiving: 0 }))
    receiving.value = true; return
  }
  if (!actionDate.value) { ElMessage.warning('请选择业务操作日期'); return }
  try { await ElMessageBox.confirm(`${verbs[verb]}：${labels[selected.value.kind]} #${selected.value.id}。请核对明细与操作日期，生效的库存流水无法直接撤销。`, '核对业务操作', { confirmButtonText: '确认执行', cancelButtonText: '返回检查', type: 'warning' }) }
  catch { return }
  await execute(`/documents/${selected.value.id}/${verb}`, { request_id: crypto.randomUUID(), business_date: actionDate.value }, 'action')
}
async function saveReceipt() {
  const lines = receiptLines.value.filter(line => line.receiving > 0).map(line => ({ purchase_line_id: line.id, quantity: line.receiving }))
  if (!lines.length || !receiptDate.value) { ElMessage.warning('请填写日期，并至少登记一项正数量收货'); return }
  await execute(`/documents/${selected.value.id}/receipts`, { request_id: crypto.randomUUID(), business_date: receiptDate.value, note: receiptNote.value, lines }, 'receipt')
}
watch(() => props.kind, () => { detailVersion++; priceVersion++; page.value = 1; status.value = ''; selected.value = null; detailError.value = ''; operationError.value = ''; pricingError.value = ''; priceLoading.value = false; creating.value = false; receiving.value = false; load() })
watch(() => props.focusDocument, id => { if (id) openDetail(id) })
onMounted(() => { load(); if (props.focusDocument) openDetail(props.focusDocument) })
</script>

<template>
  <div v-if="pending" class="error-banner" role="alert">上次操作结果尚未确认。请重试原请求，系统会核对是否已经生效。<el-button :loading="busy" @click="execute()">重试原请求</el-button></div>
  <div v-if="operationError && !creating" class="error-banner" role="alert">业务操作失败：{{ operationError }}</div>
  <section class="panel">
    <div v-if="loadError" class="error-banner" role="alert">单据查询失败：{{ loadError }}。旧结果已清除。<el-button link :loading="loading" @click="load">重试查询</el-button></div>
    <div v-if="detailError" class="error-banner" role="alert">单据 #{{ detailId }} 详情加载失败：{{ detailError }}。<el-button link @click="openDetail(detailId)">重试详情</el-button></div>
    <div class="toolbar"><form class="filters" @submit.prevent="search"><el-select :value-on-clear="''" clearable v-model="status" placeholder="全部状态" aria-label="单据状态"><el-option label="全部状态" value="" /><el-option v-for="state in availableStates" :key="state" :value="state" :label="states[state]" /></el-select><el-select :value-on-clear="''" clearable v-model="warehouse" placeholder="全部仓库" aria-label="单据仓库"><el-option label="全部仓库" value="" /><el-option v-for="item in warehouses" :key="item.id" :value="item.id" :label="item.name" /></el-select><el-button native-type="submit" :loading="loading">查询</el-button></form><el-button v-if="canWrite && kind !== 'receipt'" type="primary" :disabled="!!pending" @click="openCreate">新建{{ labels[kind] }}</el-button></div>
    <el-table :data="rows" v-loading="loading" row-key="id" :empty-text="loadError ? '单据加载失败，请重试' : '暂无匹配单据'">
      <el-table-column label="单据" width="105"><template #default="{ row }"><el-button link type="primary" @click="openDetail(row.id)">#{{ row.id }} 查看</el-button></template></el-table-column>
      <el-table-column label="仓库 / 业务关联" min-width="200"><template #default="{ row }"><strong>{{ row.warehouse }}</strong><div class="muted">{{ kind === 'transfer' ? `→ ${row.target_warehouse}` : kind === 'purchase' ? (row.supplier || '多供应商 · 查看明细') : kind === 'receipt' ? `来源采购 #${row.purchase_id}` : '门店维修领用' }}</div></template></el-table-column>
      <el-table-column label="状态" min-width="150"><template #default="{ row }"><el-tag :type="['completed','posted','received'].includes(row.status) ? 'success' : row.status === 'cancelled' ? 'info' : 'warning'">{{ states[row.status] }}</el-tag></template></el-table-column>
      <el-table-column v-if="['purchase','transfer'].includes(kind)" label="审核" min-width="125"><template #default="{ row }">{{ reviewLabels[row.review_status] }}</template></el-table-column>
      <el-table-column prop="business_date" label="业务日期" width="120" /><el-table-column prop="operator" label="创建人" min-width="120" /><el-table-column prop="note" label="备注" min-width="200" show-overflow-tooltip />
    </el-table>
    <div class="table-footer"><span>共 {{ total }} 张{{ labels[kind] }}</span><el-pagination v-model:current-page="page" :page-size="10" :total="total" layout="prev, pager, next" @current-change="load" /></div>
    <p class="table-note">{{ kind === 'purchase' ? '确认采购不增加库存；仅实际收货入库。允许部分收货，取消只终止尚未收货的余量。' : kind === 'transfer' ? '发出扣减来源仓，计入目标方向在途；收货后转为目标仓现有库存。首版整单收发，不支持发出后撤回。' : kind === 'receipt' ? '入库单从采购单实际收货产生。每张单据保留来源采购及对应库存流水。' : '草稿不占用库存。确认出库时重新检查实际可用量，成功后生成不可直接修改的库存流水。' }}</p>
  </section>

  <el-dialog v-model="creating" :title="`新建${labels[kind]}`" :width="kind === 'purchase' ? '1000px' : '790px'" :close-on-click-modal="false">
    <el-form label-position="top" :disabled="busy || !!pending" @submit.prevent="saveDraft">
      <div v-if="pricingError" class="error-banner" role="alert">价格查询失败：{{ pricingError }}。旧价格已清除。<el-button link @click="loadPricing">重新查询</el-button></div>
      <p v-if="priceLoading" class="muted">正在查询当前价格…</p><p v-if="operationError" class="error-text" role="alert">{{ operationError }}</p>
      <div class="form-grid"><el-form-item :label="kind === 'transfer' ? '来源仓库' : '业务仓库'"><el-select :value-on-clear="null" clearable v-model="form.warehouse_id" placeholder="选择仓库"><el-option v-for="item in warehouses.filter(item => item.active)" :key="item.id" :value="item.id" :label="item.name" /></el-select></el-form-item><el-form-item label="业务日期"><input v-model="form.business_date" class="date-input" type="date" required /></el-form-item></div>
      <el-form-item v-if="kind === 'transfer'" label="目标仓库"><el-select :value-on-clear="null" clearable v-model="form.target_warehouse_id" placeholder="选择目标仓库"><el-option v-for="item in warehouses.filter(item => item.active && item.id !== form.warehouse_id)" :key="item.id" :value="item.id" :label="item.name" /></el-select></el-form-item>
      <template v-if="kind === 'issue'"><div class="form-grid"><el-form-item label="维修用途（必选）"><el-select v-model="form.issue_type" clearable :value-on-clear="null" placeholder="请选择维修用途" @change="form.warranty_reason = ''"><el-option label="收费维修" value="paid_repair" /><el-option label="质保维修" value="warranty" /></el-select></el-form-item><el-form-item label="关联工单（选填）"><el-input v-model="form.work_order" maxlength="80" /></el-form-item></div><el-form-item v-if="form.issue_type === 'warranty'" label="质保说明（必填）"><el-input v-model="form.warranty_reason" maxlength="200" required /></el-form-item><p class="muted">收费维修按创建时门店定价保存结算快照；质保结算为0，仍计入实际消耗成本。实际成本在过账时计算。</p></template>
      <div v-for="(line,index) in form.lines" :key="index" class="document-line" :class="{ purchase: kind === 'purchase', 'issue-pricing': kind === 'issue' }">
        <el-form-item v-if="kind === 'purchase'" :label="`供应商 ${index + 1}`"><el-select clearable :value-on-clear="null" v-model="line.supplier_id" filterable placeholder="先选择供应商" @change="changeSupplier(line)"><el-option v-for="item in suppliers.filter(item => item.active)" :key="item.id" :value="item.id" :label="item.name" :disabled="!item.supply_categories?.length" /></el-select></el-form-item>
        <el-form-item :label="`备件 ${index + 1}`"><el-select :value-on-clear="null" clearable v-model="line.part_id" filterable :disabled="kind === 'purchase' && !line.supplier_id" :placeholder="kind === 'purchase' ? '选择该供应商的备件' : '搜索备件'" no-data-text="供货范围内暂无可选备件" @change="changePart(line)"><el-option v-for="part in partsFor(line)" :key="part.id" :value="part.id" :label="`${part.sku} · ${part.name} / ${part.unit}`" /></el-select></el-form-item>
        <el-form-item label="数量"><el-input-number v-model="line.quantity" :min="1" :max="1000000000" :precision="0" /></el-form-item>
        <el-form-item v-if="kind === 'purchase'" label="供货价 / 元"><output>{{ moneyText(line.unit_price) }}</output><small class="quote-source">{{ lineQuote(line)?.source || '选择有效报价后显示来源' }}</small></el-form-item>
        <el-form-item v-if="kind === 'issue'" label="结算价 / 元每件"><output>{{ form.issue_type ? moneyText(salePrice(line)) : '先选择用途' }}</output><small v-if="line.part_id && form.issue_type === 'paid_repair' && salePrice(line) == null" class="error-text">门店定价未维护</small></el-form-item>
        <el-form-item v-if="kind === 'issue'" label="结算金额 / 元"><output>{{ form.issue_type && line.part_id ? formatPurchaseAmount(purchaseLineCents(salePrice(line), line.quantity)) : '—' }}</output></el-form-item>
        <el-form-item v-if="kind === 'purchase'" label="明细金额 / 元"><output class="purchase-line-amount" :aria-label="`备件${index + 1}明细金额`">{{ formatPurchaseAmount(lineAmount(line)) }}</output></el-form-item>
        <el-button link type="danger" :disabled="form.lines.length === 1" @click="form.lines.splice(index,1)">移除</el-button>
      </div>
      <el-button :disabled="form.lines.length >= 100" @click="form.lines.push(newLine())">添加明细</el-button>
      <template v-if="kind === 'purchase'"><p class="document-total" aria-live="polite">采购合计：¥ {{ formatPurchaseAmount(purchaseTotal) }}</p><p class="muted">仅可选择该供应商有有效报价的备件，供货价只读；确需调整时由管理员在备件资料维护报价。保存时复核报价版本，后续按单据价格快照办理；需采用新报价时取消重建。缺少报价时不以0元代替。</p></template>
      <el-form-item label="备注" style="margin-top:18px"><el-input v-model="form.note" maxlength="200" /></el-form-item>
      <p class="muted">{{ ['purchase','transfer'].includes(kind) ? '保存后前往审核中心提交，由其他管理员或经理审核通过后再办理。' : '保存为草稿后，可在单据详情核对并确认。' }}草稿不改变库存；填写有误可取消后重新创建。</p>
      <div class="dialog-actions"><el-button @click="creating = false">关闭</el-button><el-button type="primary" native-type="submit" :disabled="priceLoading || (!!pricingError && (kind === 'purchase' || form.issue_type === 'paid_repair'))">保存草稿</el-button></div>
    </el-form>
  </el-dialog>

  <el-dialog :model-value="!!selected" :title="selected ? `${labels[selected.kind]} #${selected.id}` : ''" width="860px" :close-on-click-modal="false" @update:model-value="selected = null">
    <template v-if="selected">
      <p v-if="['purchase','transfer'].includes(selected.kind)">审核：{{ reviewLabels[selected.review?.status] }}<span v-if="selected.review?.note"> · 意见：{{ selected.review.note }}</span> <el-button link type="primary" @click="selected = null; emit('open-reviews')">前往审核中心</el-button></p>
      <div class="detail-facts"><span>当前状态<strong>{{ states[selected.status] }}</strong></span><span>业务仓库<strong>{{ warehouses.find(item => item.id === selected.warehouse_id)?.name }}</strong></span><span v-if="selected.target_warehouse_id">目标仓库<strong>{{ warehouses.find(item => item.id === selected.target_warehouse_id)?.name }}</strong></span><span v-if="selected.supplier_id">供应商<strong>{{ suppliers.find(item => item.id === selected.supplier_id)?.name }}</strong></span><span>业务日期<strong>{{ selected.business_date }}</strong></span><span>创建时间<strong>{{ localTime(selected.created_at) }}</strong></span></div>
      <div v-if="selected.kind === 'issue'" class="detail-facts"><span>维修用途<strong>{{ issueLabels[selected.issue_type] || '用途缺失' }}</strong></span><span>质保说明<strong>{{ selected.warranty_reason || '无' }}</strong></span><span>关联工单<strong>{{ selected.work_order || '未填写' }}</strong></span></div>
      <el-table :data="selected.lines" row-key="id"><el-table-column label="备件" min-width="220"><template #default="{ row }"><div class="part-cell"><img :src="imageFor(row)" alt="备件分类演示图" /><div>{{ row.name }}<small>{{ row.sku }} · {{ row.unit }}</small></div></div></template></el-table-column><el-table-column v-if="['purchase','receipt'].includes(selected.kind)" prop="supplier" label="供应商" min-width="170" /><el-table-column prop="quantity" label="数量" width="85" /><el-table-column v-if="selected.kind === 'purchase'" prop="received_quantity" label="已收货" width="85" /><el-table-column v-if="['purchase','receipt'].includes(selected.kind)" prop="unit_price" label="单价 / 元" width="110" /><el-table-column v-if="['purchase','receipt'].includes(selected.kind)" prop="subtotal" label="金额 / 元" width="120" /><el-table-column v-if="selected.kind === 'issue'" label="结算价 / 元" width="120"><template #default="{ row }">{{ moneyText(row.sale_unit_price) }}</template></el-table-column><el-table-column v-if="selected.kind === 'issue'" label="结算金额 / 元" width="140"><template #default="{ row }">{{ formatPurchaseAmount(purchaseLineCents(row.sale_unit_price, row.quantity)) }}</template></el-table-column><el-table-column v-if="['issue','transfer'].includes(selected.kind)" label="实际成本 / 元" width="145"><template #default="{ row }">{{ row.cost_amount == null ? (selected.status === 'draft' ? '生效时计算' : '成本缺失') : moneyText(row.cost_amount) }}</template></el-table-column><el-table-column label="价格依据" min-width="180"><template #default="{ row }">{{ row.pricing_source || '未记录' }}<small v-if="row.quote_version != null"> · 报价 v{{ row.quote_version }}</small><small v-if="row.sale_price_version != null"> · 售价 v{{ row.sale_price_version }}</small></template></el-table-column></el-table>
      <p v-if="['purchase','receipt'].includes(selected.kind)" class="document-total">单据金额 ¥ {{ selected.total }}</p><p class="muted">{{ selected.note || '无备注' }}</p>
      <div v-if="selected.receipts?.length" class="related-documents">实际入库：<el-button v-for="item in selected.receipts" :key="item.id" link type="primary" @click="openDetail(item.id)">入库单 #{{ item.id }} / {{ item.business_date }}</el-button></div>
      <p v-if="selected.purchase_id"><el-button link type="primary" @click="openDetail(selected.purchase_id)">查看来源采购 #{{ selected.purchase_id }}</el-button></p>
      <div v-if="allowed.length" class="document-actions"><label>操作业务日期<input v-model="actionDate" class="date-input" type="date" :disabled="busy || !!pending" /></label><el-button v-for="item in allowed" :key="item.verb" :type="item.verb === 'cancel' ? 'default' : 'primary'" :disabled="busy || !!pending" @click="act(item.verb)">{{ item.verb === 'receipt' ? '登记实际收货' : verbs[item.verb] }}</el-button></div>
    </template>
  </el-dialog>

  <el-dialog v-model="receiving" title="登记采购实际收货" width="700px" :close-on-click-modal="false" append-to-body>
    <el-form label-position="top" :disabled="busy || !!pending" @submit.prevent="saveReceipt"><el-form-item label="实际收货日期"><input v-model="receiptDate" class="date-input" type="date" required /></el-form-item><el-table :data="receiptLines" row-key="id"><el-table-column prop="name" label="备件" min-width="180" /><el-table-column prop="supplier" label="供应商" min-width="170" /><el-table-column label="剩余待收" width="100"><template #default="{ row }">{{ row.quantity - row.received_quantity }} {{ row.unit }}</template></el-table-column><el-table-column label="本次收货" width="180"><template #default="{ row }"><el-input-number v-model="row.receiving" :min="0" :max="row.quantity-row.received_quantity" :precision="0" /></template></el-table-column></el-table><el-form-item label="收货备注" style="margin-top:18px"><el-input v-model="receiptNote" maxlength="200" /></el-form-item><div class="dialog-actions"><el-button @click="receiving = false">关闭</el-button><el-button native-type="submit" type="primary">确认入库</el-button></div></el-form>
  </el-dialog>
</template>
