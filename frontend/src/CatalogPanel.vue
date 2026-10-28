<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, session } from './api'
import { imageFor, imageOptions } from './media'
import { activeQuote, catalogPayload, moneyText } from './pricing'

const props = defineProps({ kind: String, suppliers: Array, parts: Array })
const supplyCategories = computed(() => [...new Set([...(props.parts || []).map(part => part.category),
  ...(props.suppliers || []).flatMap(supplier => supplier.supply_categories || [])])].sort())
const eligibleSuppliers = computed(() => props.suppliers.filter(item => item.supply_categories?.includes(form.category || '')))
const suppliedParts = supplier => (props.parts || []).filter(part => supplier.supply_categories?.includes(part.category))
const emit = defineEmits(['changed'])
const names = { parts: '备件', suppliers: '供应商', warehouses: '仓库' }
const label = computed(() => names[props.kind])
const rows = ref([]), total = ref(0), page = ref(1), q = ref(''), active = ref(''), loading = ref(false), loadError = ref('')
const visible = ref(false), saving = ref(false), editing = ref(null)
const form = reactive({})
const view = ref('cards'), preview = ref(null)
const uploading = ref(false), uploadError = ref('')
const imageInput = ref(null)
const pricing = ref([]), pricingError = ref(''), priceVisible = ref(false), priceSaving = ref(false), priceError = ref('')
const priceForm = reactive({ part_id: null, supplier_id: null, kind: 'sale', price: '', source: '', active: true, expected_version: 0 })
const pricePart = id => pricing.value.find(item => item.part_id === id)
const defaultQuote = row => activeQuote(pricePart(row.id), row.default_supplier_id)
const otherQuotes = row => (pricePart(row.id)?.suppliers || []).filter(item => item.id !== row.default_supplier_id)
const canPrice = computed(() => ['admin', 'manager'].includes(session.user.role))
let pricingVersion = 0
async function loadPricing() {
  const current = ++pricingVersion
  if (props.kind !== 'parts') { pricing.value = []; pricingError.value = ''; return }
  try { const result = await api('/pricing/parts'); if (current === pricingVersion) { pricing.value = result.items; pricingError.value = '' } }
  catch (error) { if (current === pricingVersion) { pricing.value = []; pricingError.value = error.message } }
}
function openPrice(row, kind = 'sale', supplier = null) {
  Object.assign(priceForm, { part_id: row.id, supplier_id: supplier?.id || null, kind,
    price: kind === 'sale' ? pricePart(row.id)?.sale_price ?? '' : supplier?.supply_price ?? '', source: '', active: supplier?.active ?? true,
    expected_version: kind === 'sale' ? pricePart(row.id)?.sale_price_version || 0 : supplier?.version || 0 })
  priceError.value = ''; priceVisible.value = true
}
async function savePrice() {
  if (!priceForm.source.trim()) { priceError.value = '请填写价格来源或调价依据'; return }
  priceSaving.value = true
  try {
    await api(priceForm.kind === 'sale' ? `/pricing/parts/${priceForm.part_id}/sale-price` : `/pricing/suppliers/${priceForm.supplier_id}/parts/${priceForm.part_id}`, {
      method: priceForm.kind === 'sale' ? 'PATCH' : 'PUT', body: JSON.stringify(priceForm.kind === 'sale'
        ? { sale_price: priceForm.price, source: priceForm.source, expected_version: priceForm.expected_version }
        : { supply_price: priceForm.price, source: priceForm.source, active: priceForm.active, expected_version: priceForm.expected_version }) })
    priceVisible.value = false; await loadPricing(); emit('changed'); ElMessage.success('价格已保存')
  } catch (error) { priceError.value = error.message }
  finally { priceSaving.value = false }
}
async function uploadImage(event) {
  const file = event.target.files?.[0]
  if (!file) return
  uploadError.value = ''
  if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size > 5*1024*1024) { uploadError.value = '请选择5MB以内的PNG、JPEG或WebP图片'; event.target.value = ''; return }
  uploading.value = true
  try { const result = await api('/media/images', { method: 'POST', headers: { 'Content-Type': file.type }, body: file }); form.image_key = result.image_key }
  catch (e) { uploadError.value = e.message }
  finally { uploading.value = false; event.target.value = '' }
}
const detailFields = [['aliases', '别名', 200]]
const packageText = part => {
  const packaging = /整[瓶桶套]/.exec(part.name)?.[0]
  return packaging ? `${packaging}包装，按1件计数` : '通用备件，按件计数'
}
let requestVersion = 0

async function load() {
  const version = ++requestVersion
  loading.value = true
  try {
    const params = new URLSearchParams({ q: q.value, page: page.value, page_size: 12 })
    if (active.value !== '') params.set('active', active.value)
    const result = await api(`/${props.kind}?${params}`)
    if (version === requestVersion) { rows.value = result.items; total.value = result.total; loadError.value = '' }
  } catch (error) {
    if (version === requestVersion) { rows.value = []; total.value = 0; loadError.value = error.message }
  }
  finally { if (version === requestVersion) loading.value = false }
}

function open(row) {
  uploadError.value = ''
  editing.value = row?.id || null
  Object.keys(form).forEach(key => delete form[key])
  Object.assign(form, row || (props.kind === 'parts'
    ? { sku: '', name: '', category: '', unit: '件', safety_stock: 0, reference_price: '0.00', default_supplier_id: null, active: true }
    : props.kind === 'suppliers' ? { code: '', name: '', contact: '', phone: '', lead_time_days: 7, active: true }
      : { code: '', name: '', location: '', active: true }))
  delete form.id
  delete form.reviewed_by
  delete form.reviewed_at
  if (props.kind === 'parts') detailFields.forEach(([key]) => { form[key] ||= '' })
  if (props.kind === 'suppliers') form.supply_categories ||= []
  form.image_key ||= ''
  visible.value = true
}

async function save() {
  if (uploading.value) return
  saving.value = true
  try {
    await api(`/${props.kind}${editing.value ? `/${editing.value}` : ''}`, { method: editing.value ? 'PUT' : 'POST', body: JSON.stringify(catalogPayload(form)) })
    ElMessage.success('资料已保存'); visible.value = false
    await load(); emit('changed')
  } catch (error) { ElMessage.error(error.message) }
  finally { saving.value = false }
}

function search() { page.value = 1; load() }
watch(() => props.kind, () => { page.value = 1; q.value = ''; active.value = ''; visible.value = false; priceVisible.value = false; preview.value = null; load(); loadPricing() })
onMounted(() => { load(); loadPricing() })
</script>

<template>
  <section class="panel">
    <div class="toolbar">
      <form class="filters" @submit.prevent="search">
        <el-input v-model="q" :placeholder="kind === 'parts' ? '搜索编码、名称或别名' : `搜索${label}编码或名称`" aria-label="搜索编码或名称" clearable />
        <el-select :value-on-clear="''" clearable v-model="active" placeholder="状态" aria-label="状态筛选"><el-option label="全部状态" value="" /><el-option label="启用" value="true" /><el-option label="停用" value="false" /></el-select>
        <el-button native-type="submit" :loading="loading">查询</el-button>
      </form>
      <div class="view-controls"><el-button @click="view = view === 'cards' ? 'table' : 'cards'">{{ view === 'cards' ? '列表视图' : '卡片视图' }}</el-button><el-button v-if="session.user.role === 'admin'" type="primary" @click="open()">新增{{ label }}</el-button></div>
    </div>
    <div v-if="loadError" class="error-banner" role="alert">资料查询失败：{{ loadError }}。旧结果已清除。<el-button link :loading="loading" @click="load">重试查询</el-button></div>
    <div v-if="pricingError" class="error-banner" role="alert">价格查询失败：{{ pricingError }}。价格已清除。<el-button link @click="loadPricing">重试价格查询</el-button></div>
    <div v-if="view === 'cards'" class="catalog-grid" v-loading="loading">
      <article v-for="row in rows" :key="row.id" class="catalog-card">
        <button class="catalog-picture" :class="{ 'part-picture': kind === 'parts' }" :aria-label="`查看${row.name}`" @click="preview = row"><img :src="imageFor(row, kind)" :alt="`${row.name}的演示配图`" loading="lazy" /><span>{{ row.image_key?.startsWith('upload-') ? '本地上传' : '内置配图' }}</span></button>
        <div class="catalog-card-body"><div class="card-topline"><small>{{ row.sku || row.code }}</small><el-tag size="small" :type="row.active ? 'success' : 'info'">{{ row.active ? '启用' : '停用' }}</el-tag></div><h3>{{ row.name }}</h3>
          <template v-if="kind === 'parts'"><p>{{ row.category || '未分类' }} · 按{{ row.unit }}管理</p><div class="catalog-prices"><span>默认供应商供货价<strong>{{ defaultQuote(row) ? `¥ ${moneyText(defaultQuote(row).supply_price)}` : '暂无有效报价' }}</strong></span><span>4S门店定价<strong>{{ pricePart(row.id)?.sale_price != null ? `¥ ${moneyText(pricePart(row.id).sale_price)}` : '待维护' }}</strong></span></div><p>安全库存 {{ row.safety_stock }} {{ row.unit }} / 仓</p><details v-if="otherQuotes(row).length" class="quote-list"><summary>其他供应商报价（{{ otherQuotes(row).length }}）</summary><p v-for="quote in otherQuotes(row)" :key="quote.id">{{ quote.name }}：{{ quote.supply_price == null ? '未报价' : `¥ ${moneyText(quote.supply_price)}` }}{{ quote.active ? '' : '（未启用）' }}</p></details></template>
          <template v-else-if="kind === 'suppliers'"><p>{{ row.contact || '联系人待补充' }} · {{ row.phone || '电话待补充' }}</p><p>供货 {{ suppliedParts(row).length }} 种备件 · {{ (row.supply_categories || []).map(value => value || '未分类').join('、') || '范围未设置' }}</p><div class="card-value">{{ row.lead_time_days }}<small>天采购交期</small></div></template>
          <template v-else><p>{{ row.location || '位置待补充' }}</p><div class="card-value"><small>独立库存 · 单据追溯</small></div></template>
          <div class="card-actions"><el-button link type="primary" @click="preview = row">查看资料</el-button><el-button v-if="session.user.role === 'admin'" link @click="open(row)">编辑</el-button></div>
        </div>
      </article><p v-if="!rows.length && !loading && !loadError" class="muted">暂无匹配资料</p>
    </div>
    <el-table v-else :data="rows" v-loading="loading" :empty-text="loadError ? '资料加载失败，请重试' : '暂无匹配资料'" row-key="id">
      <el-table-column label="图片" width="85"><template #default="{ row }"><button class="thumbnail-button" :aria-label="`查看${row.name}`" @click="preview = row"><img class="thumbnail" :src="imageFor(row, kind)" :alt="`${row.name}的演示配图`" loading="lazy" /></button></template></el-table-column>
      <el-table-column :prop="kind === 'parts' ? 'sku' : 'code'" label="编码" min-width="145" />
      <el-table-column prop="name" label="名称" min-width="180" />
      <template v-if="kind === 'parts'">
        <el-table-column prop="category" label="分类" min-width="100" />
        <el-table-column prop="unit" label="单位" width="70" />
        
        <el-table-column prop="safety_stock" label="安全库存" width="100" align="right" />
        <el-table-column label="默认供货价 / 元" width="150" align="right"><template #default="{ row }">{{ defaultQuote(row) ? moneyText(defaultQuote(row).supply_price) : '暂无有效报价' }}</template></el-table-column>
        <el-table-column label="门店定价 / 元" width="140" align="right"><template #default="{ row }">{{ moneyText(pricePart(row.id)?.sale_price) }}</template></el-table-column>
        <el-table-column label="其他报价" min-width="170"><template #default="{ row }"><details v-if="otherQuotes(row).length"><summary>展开 {{ otherQuotes(row).length }} 家</summary><p v-for="quote in otherQuotes(row)" :key="quote.id">{{ quote.name }}：{{ moneyText(quote.supply_price) }}{{ quote.active ? '' : '（未启用）' }}</p></details><span v-else>无</span></template></el-table-column>
        <el-table-column label="默认供应商" min-width="180"><template #default="{ row }">{{ suppliers.find(item => item.id === row.default_supplier_id)?.name || '未指定' }}</template></el-table-column>
      </template>
      <template v-if="kind === 'suppliers'">
        <el-table-column prop="contact" label="联系人" min-width="120" />
        <el-table-column prop="phone" label="联系电话" min-width="130" />
        <el-table-column prop="lead_time_days" label="交期 / 天" width="110" />
      </template>
      <el-table-column v-if="kind === 'warehouses'" prop="location" label="位置" min-width="180" />
      <el-table-column label="状态" width="85"><template #default="{ row }"><el-tag :type="row.active ? 'success' : 'info'">{{ row.active ? '启用' : '停用' }}</el-tag></template></el-table-column>
      <el-table-column v-if="session.user.role === 'admin'" label="操作" width="85" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="open(row)">编辑</el-button></template></el-table-column>
    </el-table>
    <div class="table-footer"><span>共 {{ total }} 条资料 · 内置图为AI示意素材，上传图由资料维护人员提供</span><el-pagination v-model:current-page="page" :page-size="12" :total="total" layout="prev, pager, next" @current-change="load" /></div>
  </section>
  <el-dialog v-model="visible" :title="`${editing ? '编辑' : '新增'}${label}`" :width="kind === 'parts' ? '620px' : '520px'" :close-on-click-modal="false" :before-close="done => { if (!uploading && !saving) done() }">
      <el-form class="catalog-form" label-position="top" @submit.prevent="save">
        <el-form-item label="资料配图"><div><input ref="imageInput" type="file" hidden accept="image/png,image/jpeg,image/webp" :disabled="uploading || saving" @change="uploadImage" /><el-button :loading="uploading" :disabled="saving" @click="imageInput?.click()">{{ form.image_key ? '更换图片' : '上传图片' }}</el-button><p class="muted">PNG、JPEG或WebP，最大5MB。上传后保存资料生效；也可选择下方内置配图。</p><p v-if="uploadError" class="error-text" role="alert">{{ uploadError }}</p><div v-if="form.image_key" class="uploaded-preview"><img :src="imageFor(form, kind)" alt="当前配图预览" /><el-button link :disabled="uploading || saving" @click="form.image_key = ''">清除配图</el-button></div><div class="image-picker"><button v-for="[key, image] in imageOptions(kind)" :key="key" type="button" :class="{ chosen: form.image_key === key }" :aria-label="`选择${image.label}配图`" :aria-pressed="form.image_key === key" @click="form.image_key = key"><img :src="image.src" :alt="image.label" /><small>{{ image.label }}</small></button></div></div></el-form-item>
        <div class="form-grid">
          <el-form-item label="编码（大写字母、数字及 . _ -）"><el-input v-if="kind === 'parts'" v-model="form.sku" maxlength="40" required /><el-input v-else v-model="form.code" maxlength="40" required /></el-form-item>
          <el-form-item label="名称"><el-input v-model="form.name" maxlength="100" required /></el-form-item>
        </div>
        <template v-if="kind === 'parts'">
          <div class="form-grid"><el-form-item label="分类"><el-input v-model="form.category" maxlength="40" /></el-form-item><el-form-item label="库存单位"><span>件（一个完整库存包装）</span></el-form-item></div>
          <div class="form-grid"><el-form-item v-for="[key, text, length] in detailFields" :key="key" :label="text"><el-input v-model="form[key]" :maxlength="length" placeholder="选填" /></el-form-item></div>
          <el-form-item label="安全库存"><el-input-number v-model="form.safety_stock" :min="0" :max="1000000000" :precision="0" /></el-form-item>
          <el-form-item label="默认供应商"><el-select :value-on-clear="null" v-model="form.default_supplier_id" clearable filterable @clear="form.default_supplier_id = null"><el-option v-for="item in eligibleSuppliers" :key="item.id" :value="item.id" :label="item.name" :disabled="!item.active && item.id !== form.default_supplier_id" /></el-select></el-form-item>
          <p class="muted">库存按件计数，整瓶、整桶、整套等在备件名称中说明，不拆零换算。供应商供货价与门店定价在独立价格维护中管理。已有业务引用后编码不可修改。</p>
        </template>
        <template v-if="kind === 'suppliers'">
          <el-form-item label="供货类别"><el-select clearable v-model="form.supply_categories" multiple filterable :empty-values="[null, undefined]" placeholder="选择可供应的备件类别"><el-option v-for="category in supplyCategories" :key="category" :value="category" :label="category || '未分类'" /></el-select></el-form-item>
          <p class="muted">未设置供货类别时不能新增采购。缩小范围前需先调整范围外备件的默认供应商；已有采购与收货记录保留原供应商。</p>
          <div class="form-grid"><el-form-item label="联系人"><el-input v-model="form.contact" maxlength="80" /></el-form-item><el-form-item label="联系电话"><el-input v-model="form.phone" maxlength="40" /></el-form-item></div>
          <el-form-item label="采购交期 / 天"><el-input-number v-model="form.lead_time_days" :min="0" :max="365" :precision="0" /></el-form-item>
        </template>
        <el-form-item v-if="kind === 'warehouses'" label="位置"><el-input v-model="form.location" maxlength="200" /></el-form-item>
        <el-form-item label="状态"><el-switch v-model="form.active" active-text="启用" inactive-text="停用" /></el-form-item>
      <div class="dialog-actions"><el-button @click="visible = false" :disabled="saving || uploading">取消</el-button><el-button type="primary" native-type="submit" :disabled="uploading" :loading="saving">保存资料</el-button></div>
      </el-form>
  </el-dialog>
  <el-dialog :model-value="!!preview" :title="preview?.name" width="620px" @update:model-value="preview = null">
    <template v-if="preview && kind === 'suppliers'"><p>供货范围：{{ (preview.supply_categories || []).map(value => value || '未分类').join('、') || '未设置' }}</p><p>可供备件：{{ suppliedParts(preview).map(part => part.name).join('、') || '暂无匹配备件' }}</p></template>
    <template v-if="preview"><img class="catalog-preview" :class="{ 'part-preview': kind === 'parts' }" :src="imageFor(preview, kind)" :alt="`${preview.name}的演示配图`" /><p class="muted">{{ preview.image_key?.startsWith('upload-') ? '本地上传配图' : '内置示意配图' }}</p><div class="detail-facts"><span>编码<strong>{{ preview.sku || preview.code }}</strong></span><span>状态<strong>{{ preview.active ? '启用' : '停用' }}</strong></span><template v-if="kind === 'parts'"><span>默认供应商供货价<strong>{{ defaultQuote(preview) ? `¥ ${moneyText(defaultQuote(preview).supply_price)} / 件` : '暂无有效报价' }}</strong></span><span>4S门店定价<strong>{{ moneyText(pricePart(preview.id)?.sale_price) }} 元 / 件</strong></span><span>安全库存<strong>{{ preview.safety_stock }} 件 / 仓</strong></span><span>分类<strong>{{ preview.category || '未分类' }}</strong></span><span v-for="[key, text] in detailFields" :key="key">{{ text }}<strong>{{ preview[key] || '无' }}</strong></span><span>计数说明<strong>{{ packageText(preview) }}</strong></span><span>默认供应商<strong>{{ suppliers.find(item => item.id === preview.default_supplier_id)?.name || '未指定' }}</strong></span></template><span v-if="kind === 'warehouses'">位置<strong>{{ preview.location || '未填写' }}</strong></span><span v-if="kind === 'suppliers'">联系信息<strong>{{ preview.contact || '未填写' }} / {{ preview.phone || '未填写' }}</strong></span></div><el-button v-if="kind === 'parts' && session.user.role === 'admin'" type="primary" @click="open(preview); preview = null">编辑备件资料</el-button></template>
    <div v-if="preview && kind === 'parts'" class="pricing-maintenance"><el-button v-if="canPrice" @click="openPrice(preview)">维护门店定价</el-button><details class="quote-list"><summary>{{ session.user.role === 'admin' ? '查看与维护供应商报价' : '查看供应商报价' }}</summary><article v-for="quote in pricePart(preview.id)?.suppliers || []" :key="quote.id"><p><strong>{{ quote.name }}</strong> · {{ quote.supply_price == null ? '未报价' : `¥ ${moneyText(quote.supply_price)} / 件` }} · {{ quote.active ? '启用' : '未启用' }}</p><p class="muted">来源：{{ quote.source || '未登记' }} · 版本 {{ quote.version }}</p><el-button v-if="session.user.role === 'admin'" link type="primary" @click="openPrice(preview, 'supply', quote)">维护供货价</el-button></article><p v-if="!pricePart(preview.id)?.suppliers.length" class="muted">暂无具备供货资格的供应商，请先维护供货类别。</p></details></div>
  </el-dialog>
  <el-dialog v-model="priceVisible" :title="priceForm.kind === 'sale' ? '维护4S门店定价' : '维护供应商供货价'" width="500px" :close-on-click-modal="false" :before-close="done => { if (!priceSaving) done() }" append-to-body>
    <el-form label-position="top" :disabled="priceSaving" @submit.prevent="savePrice"><p class="muted">{{ priceForm.kind === 'sale' ? '用于新建收费维修出库的结算快照，必须大于0。' : '仅管理员维护；0元供货也需登记明确来源。' }}既有单据价格不随本次调整重写。</p><el-form-item label="价格 / 元每件"><el-input v-model="priceForm.price" inputmode="decimal" required /></el-form-item><el-form-item label="来源或调价依据"><el-input v-model="priceForm.source" maxlength="300" required /></el-form-item><el-form-item v-if="priceForm.kind === 'supply'" label="报价状态"><el-switch v-model="priceForm.active" active-text="启用" inactive-text="停用" /></el-form-item><p v-if="priceError" class="error-text" role="alert">{{ priceError }}</p><div class="dialog-actions"><el-button @click="priceVisible = false">取消</el-button><el-button type="primary" native-type="submit" :loading="priceSaving">保存价格</el-button></div></el-form>
  </el-dialog>
</template>

<style scoped>
.uploaded-preview { display: flex; align-items: center; gap: 15px; margin-bottom: 15px; }
.uploaded-preview img { width: 100px; height: 80px; object-fit: contain; border: 1px solid #e5e7eb; border-radius: 8px; }
.image-picker { max-height: 205px; overflow-y: auto; }
.catalog-form { max-height: 72vh; overflow-y: auto; padding-right: 8px; }
.part-picture img { object-fit: contain; background: #fff; }
.detail-facts strong { overflow-wrap: anywhere; }
.catalog-prices { display: grid; gap: 10px; margin: 18px 0; }
.catalog-prices span { display: flex; justify-content: space-between; gap: 12px; color: #697586; font-size: 12px; }
.catalog-prices strong { color: #1f2937; font-size: 15px; }
.quote-list { font-size: 12px; line-height: 1.6; margin: 12px 0; }
.quote-list summary { cursor: pointer; color: #0071e3; }
.quote-list article { border-bottom: 1px solid #e5e7eb; padding-bottom: 10px; }
.pricing-maintenance { margin-top: 20px; }
</style>
