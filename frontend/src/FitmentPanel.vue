<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { allCatalog, api, session } from './api'

defineProps({ parts: Array })
const mode = ref('vehicle-models'), rows = ref([]), vehicles = ref([]), total = ref(0), page = ref(1)
const q = ref(''), vehicleFilter = ref(null), partFilter = ref(null), reviewed = ref('')
const loading = ref(false), loadError = ref(''), visible = ref(false), saving = ref(false), saveError = ref(''), reviewing = ref(false)
const editing = ref(null), preview = ref(null), form = reactive({})
const reviewError = ref('')
const label = computed(() => mode.value === 'vehicle-models' ? '车型条件' : '适配关系')
const vehicleFields = [['code', '车型标识', 40], ['brand', '品牌', 80], ['model_name', '车型名称', 100],
  ['powertrain_type', '动力类型', 60], ['engine_code', '发动机或动力标识', 100], ['transmission', '变速器', 100],
  ['market', '市场版本', 80], ['configuration', '相关配置', 300]]
const fitmentFields = [['conditions', '车辆适用条件', 500], ['restrictions', '适配限制', 500]]
const sourceFields = [['source_reference', '依据来源及章节 / 页码', 300], ['source_version', '依据版本', 60]]
const fields = computed(() => [...(mode.value === 'vehicle-models' ? vehicleFields : fitmentFields), ...sourceFields])
let requestVersion = 0

function vehicleLabel(row) { return `${row.code} · ${row.model_name} · ${row.model_year || '年款待补充'} · ${row.engine_code || row.powertrain_type || '动力待补充'}` }
function reviewTime(value) { return value ? new Date(value + 'Z').toLocaleString('zh-CN') : '尚未核准' }

async function load() {
  const version = ++requestVersion, kind = mode.value
  loading.value = true
  const params = new URLSearchParams({ page: page.value, page_size: 12 })
  if (kind === 'vehicle-models') params.set('q', q.value)
  else {
    if (vehicleFilter.value) params.set('vehicle_id', vehicleFilter.value)
    if (partFilter.value) params.set('part_id', partFilter.value)
    if (reviewed.value !== '') params.set('reviewed', reviewed.value)
  }
  try {
    const [result, options] = await Promise.all([api(`/${kind}?${params}`), allCatalog('/vehicle-models')])
    if (version === requestVersion) { rows.value = result.items; total.value = result.total; vehicles.value = options; loadError.value = '' }
  } catch (error) {
    if (version === requestVersion) { rows.value = []; total.value = 0; vehicles.value = []; loadError.value = error.message }
  } finally { if (version === requestVersion) loading.value = false }
}

function open(row) {
  editing.value = row?.id || null; saveError.value = ''
  Object.keys(form).forEach(key => delete form[key])
  fields.value.forEach(([key]) => { form[key] = row?.[key] || '' })
  if (mode.value === 'vehicle-models') form.model_year = row?.model_year ?? null
  else Object.assign(form, { vehicle_id: row?.vehicle_id ?? null, part_id: row?.part_id ?? null, required_quantity: row?.required_quantity ?? null })
  form.active = row?.active ?? true
  visible.value = true
}

async function save() {
  saving.value = true; saveError.value = ''
  try {
    await api(`/${mode.value}${editing.value ? `/${editing.value}` : ''}`, { method: editing.value ? 'PUT' : 'POST', body: JSON.stringify(form) })
    ElMessage.success('资料已保存，变更内容须重新核准'); visible.value = false; await load()
  } catch (error) { saveError.value = error.message }
  finally { saving.value = false }
}

async function review(row) {
  reviewing.value = true; reviewError.value = ''
  try {
    await api(`/catalog/${mode.value}/${row.id}/review`, { method: 'POST' })
    ElMessage.success('已记录人工核准'); await load()
    preview.value = rows.value.find(item => item.id === row.id) || null
  } catch (error) { reviewError.value = error.message }
  finally { reviewing.value = false }
}

function search() { page.value = 1; load() }
watch(mode, () => { page.value = 1; preview.value = null; visible.value = false; load() })
watch(() => preview.value?.id, () => { reviewError.value = '' })
onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="toolbar">
      <div class="view-controls" role="group" aria-label="资料类型"><el-button :type="mode === 'vehicle-models' ? 'primary' : 'default'" :aria-pressed="mode === 'vehicle-models'" @click="mode = 'vehicle-models'">车型条件</el-button><el-button :type="mode === 'fitments' ? 'primary' : 'default'" :aria-pressed="mode === 'fitments'" @click="mode = 'fitments'">备件适配</el-button></div>
      <el-button v-if="session.user.role === 'admin'" type="primary" @click="open()">新增{{ label }}</el-button>
    </div>
    <p class="muted">车型按年款、动力、变速器、市场及配置分别登记。适配须有具体备件身份、条件、数量和版本依据；名称或图片相似不能证明适配。</p>
    <form class="filters fitment-filters" @submit.prevent="search">
      <el-input v-if="mode === 'vehicle-models'" v-model="q" placeholder="搜索车型标识、名称或动力信息" aria-label="搜索车型" clearable />
      <template v-else>
        <el-select :value-on-clear="''" v-model="vehicleFilter" filterable clearable placeholder="全部车型条件" aria-label="筛选车型"><el-option v-for="item in vehicles" :key="item.id" :value="item.id" :label="vehicleLabel(item)" /></el-select>
        <el-select :value-on-clear="''" v-model="partFilter" filterable clearable placeholder="全部备件" aria-label="筛选备件"><el-option v-for="item in parts" :key="item.id" :value="item.id" :label="`${item.sku} · ${item.name}`" /></el-select>
        <el-select :value-on-clear="''" clearable v-model="reviewed" aria-label="核准筛选" placeholder="全部核准状态"><el-option label="全部核准状态" value="" /><el-option label="已核准且启用" value="true" /><el-option label="待核准或不可用" value="false" /></el-select>
      </template>
      <el-button native-type="submit" :loading="loading">查询</el-button>
    </form>
    <div v-if="loadError" class="error-banner" role="alert">资料查询失败：{{ loadError }}<el-button link :loading="loading" @click="load">重试查询</el-button></div>
    <el-table :data="rows" v-loading="loading" row-key="id" :empty-text="loadError ? '加载失败，请重试' : '尚无资料。请依据可使用的车型文件建档，不自动填造适配关系。'">
      <template v-if="mode === 'vehicle-models'">
        <el-table-column prop="code" label="车型标识" min-width="150" />
        <el-table-column prop="model_name" label="车型" min-width="145" />
        <el-table-column prop="model_year" label="年款" width="85" />
        <el-table-column prop="powertrain_type" label="动力类型" min-width="120" />
        <el-table-column prop="engine_code" label="发动机 / 动力标识" min-width="150" />
        <el-table-column prop="transmission" label="变速器" min-width="120" />
        <el-table-column prop="market" label="市场版本" min-width="120" />
      </template>
      <template v-else>
        <el-table-column prop="vehicle_code" label="车型标识" min-width="150" />
        <el-table-column label="备件" min-width="180"><template #default="{ row }">{{ row.part_name }}<small class="fitment-sku">{{ row.sku }}</small></template></el-table-column>
        <el-table-column prop="conditions" label="适用条件" min-width="200" show-overflow-tooltip />
        <el-table-column prop="restrictions" label="适配限制" min-width="180" show-overflow-tooltip />
        <el-table-column label="单次需求" width="110"><template #default="{ row }">{{ row.required_quantity == null ? '待核准' : `${row.required_quantity} ${row.unit}` }}</template></el-table-column>
      </template>
      <el-table-column prop="source_version" label="依据版本" min-width="110" />
      <el-table-column label="核准状态" width="145"><template #default="{ row }"><el-tag :type="(mode === 'fitments' ? row.eligible : row.reviewed_at && row.active) ? 'success' : 'info'">{{ (mode === 'fitments' ? row.eligible : row.reviewed_at && row.active) ? '已核准且启用' : '待核准或停用' }}</el-tag></template></el-table-column>
      <el-table-column label="操作" :width="session.user.role === 'admin' ? 140 : 80" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="preview = row">查看</el-button><el-button v-if="session.user.role === 'admin'" link @click="open(row)">编辑</el-button></template></el-table-column>
    </el-table>
    <div class="table-footer"><span>共 {{ total }} 条{{ label }}</span><el-pagination v-model:current-page="page" :page-size="12" :total="total" layout="prev, pager, next" @current-change="load" /></div>
  </section>

  <el-dialog v-model="visible" :title="`${editing ? '编辑' : '新增'}${label}`" width="680px" :close-on-click-modal="false">
    <el-form class="fitment-form" label-position="top" @submit.prevent="save">
      <div v-if="mode === 'fitments'" class="form-grid">
        <el-form-item label="车型条件"><el-select :value-on-clear="null" clearable v-model="form.vehicle_id" filterable aria-label="适配车型" placeholder="选择已登记的车型条件"><el-option v-for="item in vehicles" :key="item.id" :value="item.id" :label="vehicleLabel(item)" :disabled="!item.active" /></el-select></el-form-item>
        <el-form-item label="库存备件 SKU"><el-select :value-on-clear="null" clearable v-model="form.part_id" filterable aria-label="适配备件" placeholder="选择库存备件"><el-option v-for="item in parts" :key="item.id" :value="item.id" :label="`${item.sku} · ${item.name}`" :disabled="!item.active" /></el-select></el-form-item>
      </div>
      <div class="form-grid"><el-form-item v-for="[key, text, length] in fields" :key="key" :label="text"><el-input v-model="form[key]" :maxlength="length" :type="length >= 300 ? 'textarea' : 'text'" :rows="2" :required="['code', 'model_name'].includes(key)" /></el-form-item>
        <el-form-item v-if="mode === 'vehicle-models'" label="年款"><el-input-number v-model="form.model_year" :min="1900" :max="2100" :precision="0" :controls="false" /></el-form-item>
        <el-form-item v-else label="单次需求数量（按备件库存单位）"><el-input-number v-model="form.required_quantity" :min="1" :max="1000000000" :precision="0" :controls="false" /><small class="muted">{{ parts.find(item => item.id === form.part_id)?.unit || '先选择备件' }}；未知时留空，不默认填1</small></el-form-item>
      </div>
      <el-form-item label="资料状态"><el-switch v-model="form.active" active-text="启用" inactive-text="停用" /></el-form-item>
      <p class="muted">仅登记可追溯来源。没有厂家资料时保留待核准；“不适用”也需有依据。资料或条件变化后，相关核准失效。</p>
      <p v-if="saveError" class="error-text" role="alert">{{ saveError }}</p>
      <div class="dialog-actions"><el-button :disabled="saving" @click="visible = false">取消</el-button><el-button native-type="submit" type="primary" :loading="saving">保存资料</el-button></div>
    </el-form>
  </el-dialog>

  <el-dialog :model-value="!!preview" :title="`${label}详情`" width="720px" @update:model-value="preview = null">
    <template v-if="preview">
      <div class="detail-facts"><span v-for="[key, text] in fields" :key="key">{{ text }}<strong>{{ preview[key] || '待补充' }}</strong></span><span v-if="mode === 'vehicle-models'">年款<strong>{{ preview.model_year || '待补充' }}</strong></span><template v-else><span>车型<strong>{{ preview.vehicle_code }} · {{ preview.model_name }}</strong></span><span>备件<strong>{{ preview.sku }} · {{ preview.part_name }}</strong></span><span>单次需求<strong>{{ preview.required_quantity == null ? '待核准' : `${preview.required_quantity} ${preview.unit}` }}</strong></span></template><span>人工核准时间<strong>{{ reviewTime(preview.reviewed_at) }}</strong></span><span>状态<strong>{{ preview.active ? '启用' : '停用' }}</strong></span></div>
      <p class="muted">核准表示管理员已对照来源人工核验；系统不会自动证明文件真实性或厂家适配。适配核准要求关联的车型和具体备件身份先通过核准。</p>
      <p v-if="reviewError" class="error-text" role="alert">{{ reviewError }}</p>
      <el-button v-if="session.user.role === 'admin' && !preview.reviewed_at" type="primary" :loading="reviewing" @click="review(preview)">登记人工核准</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.fitment-filters { margin: 22px 0; flex-wrap: wrap; }
.fitment-filters > .el-select { width: 220px; }
.fitment-form { max-height: 72vh; overflow-y: auto; padding-right: 8px; }
.fitment-sku { display: block; color: #96969d; margin-top: 5px; }
.detail-facts strong { overflow-wrap: anywhere; line-height: 1.7; white-space: pre-wrap; }
</style>
