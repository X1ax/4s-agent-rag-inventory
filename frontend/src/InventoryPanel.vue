<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, localTime, session } from './api'
import { imageFor } from './media'

const props = defineProps({ kind: String, warehouses: Array, parts: Array })
const rows = ref([]), total = ref(0), page = ref(1), q = ref(''), warehouse = ref(''), loading = ref(false), loadError = ref('')
const visible = ref(false), saving = ref(false), pending = ref(null)
const newLine = () => ({ part_id: null, quantity: 1, unit_cost: '', cost_source: '' })
const form = reactive({ warehouse_id: null, business_date: new Date().toLocaleDateString('en-CA'), note: '', lines: [newLine()] })
const canWrite = computed(() => ['admin', 'storekeeper'].includes(session.user.role))
let version = 0

async function load() {
  const current = ++version
  loading.value = true
  try {
    const params = new URLSearchParams({ page: page.value, page_size: 10 })
    if (props.kind === 'balances') params.set('q', q.value)
    if (warehouse.value) params.set('warehouse_id', warehouse.value)
    const result = await api(`/inventory/${props.kind}?${params}`)
    if (current === version) { rows.value = result.items; total.value = result.total; loadError.value = '' }
  } catch (error) {
    if (current === version) { rows.value = []; total.value = 0; loadError.value = error.message }
  }
  finally { if (current === version) loading.value = false }
}

function search() { page.value = 1; load() }
function open() {
  pending.value = null
  Object.assign(form, { warehouse_id: null, business_date: new Date().toLocaleDateString('en-CA'), note: '', lines: [newLine()] })
  visible.value = true
}

async function save() {
  if (saving.value) return
  if (!form.warehouse_id || form.lines.some(line => !line.part_id || !Number.isInteger(line.quantity) || line.quantity < 1)) {
    ElMessage.warning('请选择仓库、备件，并填写正整数数量'); return
  }
  if (form.lines.some(line => !/^\d{1,10}(?:\.\d{1,2})?$/.test(line.unit_cost.trim()) || !line.cost_source.trim())) {
    ElMessage.warning('请填写期初单位成本及成本依据；0元成本也需说明来源'); return
  }
  saving.value = true
  // Keep exactly the same request and parameters after a timeout, until the result is resolved.
  pending.value ||= { ...JSON.parse(JSON.stringify(form)), request_id: crypto.randomUUID() }
  try {
    const result = await api('/inventory/openings', { method: 'POST', body: JSON.stringify(pending.value) })
    ElMessage.success(`期初单 #${result.id} 已入账${result.replayed ? '（重复请求已安全恢复）' : ''}`)
    visible.value = false; pending.value = null; await load()
  } catch (error) { ElMessage.error(error.message) }
  finally { saving.value = false }
}

watch(() => props.kind, () => { page.value = 1; q.value = ''; load() })
onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="toolbar">
      <form class="filters" @submit.prevent="search">
        <el-input v-if="kind === 'balances'" v-model="q" placeholder="搜索备件编码或名称" aria-label="搜索备件编码或名称" clearable />
        <el-select :value-on-clear="''" clearable v-model="warehouse" placeholder="选择仓库" aria-label="仓库筛选"><el-option label="全部仓库" value="" /><el-option v-for="item in warehouses" :key="item.id" :label="item.name" :value="item.id" /></el-select>
        <el-button native-type="submit" :loading="loading">查询</el-button>
      </form>
      <el-button v-if="kind === 'balances' && canWrite" type="primary" @click="open">登记期初库存</el-button>
    </div>
    <div v-if="loadError" class="error-banner" role="alert">{{ kind === 'balances' ? '库存' : '流水' }}查询失败：{{ loadError }}。旧结果已清除。<el-button link :loading="loading" @click="load">重试查询</el-button></div>
    <el-table :data="rows" v-loading="loading" :empty-text="loadError ? '数据加载失败，请重试' : '暂无匹配记录'" :row-key="kind === 'balances' ? row => `${row.warehouse_id}-${row.part_id}` : 'id'">
      <el-table-column v-if="kind === 'ledger'" label="来源单据" width="130"><template #default="{ row }">{{ { opening: '期初', receipt: '采购入库', issue: '领用出库', transfer: row.event === 'transfer_send' ? '调拨发出' : '调拨收货' }[row.document_kind] }} #{{ row.document_id }}</template></el-table-column>
      <el-table-column prop="warehouse" label="仓库" min-width="120" />
      <el-table-column label="备件" min-width="230"><template #default="{ row }"><div class="part-cell"><img :src="imageFor(row)" alt="备件分类演示图" loading="lazy" /><div>{{ row.name }}<small>{{ row.sku }}</small></div></div></template></el-table-column>
      <el-table-column prop="unit" label="单位" width="65" />
      <template v-if="kind === 'balances'">
        <el-table-column prop="quantity" label="现有 / 可用" width="125" align="right" />
        <el-table-column prop="purchase_pending" label="采购待收" width="100" align="right" />
        <el-table-column prop="in_transit" label="调拨在途" width="100" align="right" />
        <el-table-column prop="safety_stock" label="安全库存" width="100" align="right" />
        <el-table-column label="库存成本 / 元" min-width="135" align="right"><template #default="{ row }">{{ row.stock_value ?? (row.quantity === 0 ? '0.00' : '待补齐') }}</template></el-table-column>
        <el-table-column label="库存状态" min-width="130"><template #default="{ row }"><el-tag :type="row.quantity === 0 ? 'danger' : row.quantity < row.safety_stock ? 'warning' : 'success'">{{ row.quantity === 0 ? '零库存' : row.quantity < row.safety_stock ? '低于安全库存' : '正常' }}</el-tag></template></el-table-column>
        <el-table-column label="库存记录" width="110"><template #default="{ row }">{{ row.initialized ? '已建账' : '未建账' }}</template></el-table-column>
        <el-table-column label="资料状态" width="115"><template #default="{ row }">{{ row.part_active && row.warehouse_active ? '启用' : '含停用资料' }}</template></el-table-column>
      </template>
      <template v-else>
        <el-table-column label="数量变化" width="100" align="right"><template #default="{ row }"><span :class="row.quantity_delta > 0 ? 'positive' : 'negative'">{{ row.quantity_delta > 0 ? '+' : '' }}{{ row.quantity_delta }}</span></template></el-table-column>
        <el-table-column prop="business_date" label="业务日期" min-width="115" />
        <el-table-column prop="operator" label="操作人" min-width="120" />
        <el-table-column label="入账时间" min-width="180"><template #default="{ row }">{{ localTime(row.created_at) }}</template></el-table-column>
        <el-table-column prop="note" label="备注" min-width="180" />
      </template>
    </el-table>
    <div class="table-footer"><span>共 {{ total }} 条{{ kind === 'balances' ? '仓库备件组合' : '流水' }}</span><el-pagination v-model:current-page="page" :page-size="10" :total="total" layout="prev, pager, next" @current-change="load" /></div>
    <p class="table-note">{{ kind === 'balances' ? '尚无预留流程，可用量等于现有量。采购待收与调拨在途单列，不计入现有库存；安全库存按每仓判断。' : '流水来自期初、采购实际入库、领用及调拨收发单据，正数为增加，负数为减少，不提供直接编辑或删除。' }}</p>
  </section>
  <el-dialog v-model="visible" title="登记期初库存" width="820px" :close-on-click-modal="false" :before-close="done => { if (!saving) done() }">
    <p class="muted">仅用于首次建账。已有库存记录的仓库备件组合不能再次初始化，提交后立即生效。</p>
    <div>
      <el-form label-position="top" :disabled="saving || !!pending" @submit.prevent="save">
        <div class="form-grid"><el-form-item label="入账仓库"><el-select :value-on-clear="null" clearable v-model="form.warehouse_id" placeholder="选择仓库"><el-option v-for="item in warehouses.filter(item => item.active)" :key="item.id" :label="item.name" :value="item.id" /></el-select></el-form-item><el-form-item label="业务日期"><input v-model="form.business_date" class="date-input" type="date" required :disabled="saving || !!pending" /></el-form-item></div>
        <div v-for="(line, index) in form.lines" :key="index" class="opening-line">
          <el-form-item :label="`备件 ${index + 1}`"><el-select :value-on-clear="null" clearable v-model="line.part_id" filterable placeholder="搜索并选择备件"><el-option v-for="item in parts.filter(item => item.active)" :key="item.id" :value="item.id" :label="`${item.sku} · ${item.name} / ${item.unit}`" /></el-select></el-form-item>
          <el-form-item label="数量"><el-input-number v-model="line.quantity" :min="1" :max="1000000000" :precision="0" /></el-form-item>
          <el-form-item label="期初单位成本 / 元"><el-input v-model="line.unit_cost" inputmode="decimal" required /></el-form-item>
          <el-form-item label="成本依据"><el-input v-model="line.cost_source" maxlength="300" placeholder="填写期初成本来源" required /></el-form-item>
          <el-button :disabled="form.lines.length === 1" link type="danger" @click="form.lines.splice(index, 1)">移除</el-button>
        </div>
        <el-button :disabled="form.lines.length >= 100" @click="form.lines.push(newLine())">添加明细</el-button>
        <el-form-item label="备注" style="margin-top: 18px"><el-input v-model="form.note" maxlength="200" /></el-form-item>
      <p v-if="pending" class="muted">本次参数已固定。可重试确认结果；若需修改，请关闭后重新登记，并先查询库存与流水确认上次结果。</p>
      </el-form>
      <div class="dialog-actions"><el-button :disabled="saving" @click="visible = false">关闭</el-button><el-button type="primary" :loading="saving" @click="save">{{ pending ? '重试原请求' : '确认入账' }}</el-button></div>
    </div>
  </el-dialog>
</template>

<style scoped>
.opening-line { grid-template-columns: minmax(180px, 1.5fr) 120px 130px minmax(150px, 1fr) 40px; }
@media (max-width: 850px) { .opening-line { grid-template-columns: 1fr 1fr; } }
</style>
