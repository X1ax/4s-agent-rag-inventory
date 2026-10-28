<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElCascader, ElCheckbox } from 'element-plus'
import { api } from './api'
import { imageFor } from './media'
import TrendChart from './TrendChart.vue'
import { dateRange } from './trendChart.js'
import { filterAttention } from './stockAttention.js'

const props = defineProps({ warehouses: Array, parts: Array })
const data = ref(null), loading = ref(false), loadError = ref('')
const dateText = value => new Date(value - new Date(value).getTimezoneOffset() * 60000).toISOString().slice(0, 10)
const form = reactive({ part_id: '', warehouse_id: '', date_from: dateText(Date.now() - 29 * 86400000), date_to: dateText(Date.now()) })
const partOptions = computed(() => {
  const groups = new Map()
  for (const part of props.parts) {
    const category = part.category?.trim() || '未分类'
    if (!groups.has(category)) groups.set(category, { value: category, label: category, children: [] })
    groups.get(category).children.push({ value: part.id, label: part.name })
  }
  return [...groups.values()]
})
let version = 0
async function load() {
  const current = ++version
  loading.value = true
  try {
    const params = new URLSearchParams({ date_from: form.date_from, date_to: form.date_to })
    if (form.part_id) params.set('part_id', form.part_id)
    if (form.warehouse_id) params.set('warehouse_id', form.warehouse_id)
    const result = await api(`/statistics?${params}`)
    if (current === version) { data.value = result; loadError.value = '' }
  } catch (error) {
    if (current === version) { data.value = null; loadError.value = error.message }
  }
  finally { if (current === version) loading.value = false }
}
const chart = computed(() => {
  const dates = dateRange(data.value?.date_from, data.value?.date_to)
  const values = new Map((data.value?.trend || []).map(row => [`${row.business_date}-${row.event}`, Math.abs(row.quantity)]))
  return { dates, series: [
    { key: 'receipt', label: '采购入库', color: '#1677ff' },
    { key: 'issue', label: '领用出库', color: '#ff8b37' },
  ].map(series => ({ ...series, values: dates.map(day => values.get(`${day}-${series.key}`) || 0) })) }
})
const onlyUncovered = ref(false)
const attention = computed(() => filterAttention((data.value?.groups || []).flatMap(group => group.low_stock), onlyUncovered.value))
const attentionPage = ref(1)
const attentionRows = computed(() => attention.value.slice((attentionPage.value - 1) * 12, attentionPage.value * 12))
watch([data, onlyUncovered], () => { attentionPage.value = 1 })
const rankPage = ref(1)
const rankSort = ref({ prop: 'issue_count', order: 'descending' })
const ranking = computed(() => (data.value?.consumption_ranking || []).toSorted((a, b) =>
  (rankSort.value.order === 'ascending' ? 1 : -1) * (a[rankSort.value.prop] - b[rankSort.value.prop])))
function sortRanking({ prop, order }) {
  rankSort.value = { prop: prop || 'issue_count', order: order || 'descending' }
  rankPage.value = 1
}
const rankingRows = computed(() => ranking.value.slice((rankPage.value - 1) * 7, rankPage.value * 7))
watch(data, () => { rankPage.value = 1 })
const metrics = [
  { key: 'on_hand', label: '当前现有库存', note: '可用量相同' },
  { key: 'purchase_pending', label: '采购待收货', note: '不含草稿及取消余量' },
  { key: 'in_transit', label: '调拨在途', note: '尚未计入现有量' },
]
watch(() => props.parts, parts => {
  if (form.part_id && !parts.some(part => part.id === form.part_id)) form.part_id = ''
  load()
}, { immediate: true })
</script>

<template>
  <section class="panel statistics-filters"><form class="filters" @submit.prevent="load"><el-select :value-on-clear="null" clearable v-model="form.warehouse_id" placeholder="全部仓库" aria-label="统计仓库" @clear="form.warehouse_id = ''; load()"><el-option label="全部仓库" value="" /><el-option v-for="item in props.warehouses" :key="item.id" :label="item.name" :value="item.id" /></el-select><el-cascader clearable v-model="form.part_id" :options="partOptions" :props="{ emitPath: false }" :show-all-levels="false" filterable placeholder="全部备件（可按分类筛选）" aria-label="统计备件名称" @clear="form.part_id = ''; load()" /><label class="date-filter">从<input v-model="form.date_from" type="date" class="date-input" required /></label><label class="date-filter">至<input v-model="form.date_to" type="date" class="date-input" required /></label><el-button native-type="submit" type="primary" :loading="loading">更新统计</el-button></form></section>
  <p v-if="!props.parts.length" class="empty-state">暂无可供统计的备件，请先维护备件资料。</p>
  <div v-if="loadError" class="error-banner" role="alert">统计查询失败：{{ loadError }}。旧统计结果已清除，请检查筛选条件后重试。<el-button link :loading="loading" @click="load">重试查询</el-button></div>
  <template v-if="data">
    <div class="stats business-stats"><article v-for="metric in metrics" :key="metric.key"><span>{{ metric.label }}</span><strong>{{ data.totals[metric.key] }}</strong><small>件 · {{ metric.note }}</small></article></div>
    <div class="analytics-grid">
      <section class="panel chart-card trend-panel"><div class="section-heading"><h2>入库与领用趋势</h2><span class="muted">{{ data.date_from }} — {{ data.date_to }}</span></div><TrendChart :dates="chart.dates" :series="chart.series" stretch unit="件" label="入库与领用趋势" />
        <p class="table-note">按业务日期汇总实际采购入库量和已过账领用量，统一按件展示；同一单据的各备件数量累加，期初与调拨不计入。无记录日期按零显示，不代表真实门店完整历史。</p>
      </section>
      <section class="panel chart-card consumption-card">
        <h2>备件领用排行</h2>
        <p class="muted">{{ data.part_name || '全部备件' }} · {{ data.date_from }} — {{ data.date_to }} · 共 {{ ranking.length }} 种备件</p>
        <el-table :data="rankingRows" class="consumption-table" aria-label="备件领用排行" :default-sort="{ prop: 'issue_count', order: 'descending' }" empty-text="该期间没有已过账领用记录" @sort-change="sortRanking">
          <el-table-column prop="name" label="备件名称" min-width="200" show-overflow-tooltip><template #default="{ row }"><div class="consumption-part"><img :src="imageFor(props.parts.find(part => part.sku === row.sku) || row)" :alt="row.name" loading="lazy" /><span :title="`${row.name} · ${row.sku}`">{{ row.name }}</span></div></template></el-table-column>
          <el-table-column prop="issue_count" label="领用次数" width="120" align="right" sortable="custom" :sort-orders="['descending', 'ascending']"><template #default="{ row }">{{ row.issue_count }} 次</template></el-table-column>
          <el-table-column prop="quantity" label="领用数量" width="120" align="right" sortable="custom" :sort-orders="['descending', 'ascending']"><template #default="{ row }">{{ row.quantity }} 件</template></el-table-column>
        </el-table>
        <div class="consumption-footer"><span>每页 7 条</span><el-pagination v-if="ranking.length > 7" v-model:current-page="rankPage" class="ranking-pagination" aria-label="备件领用排行分页" :page-size="7" :total="ranking.length" :pager-count="5" layout="prev, pager, next" /></div>
        <p class="table-note">领用次数按已过账出库单据统计，领用数量按件汇总。</p>
      </section>
    </div>
    <section class="panel"><div class="section-heading"><h2>库存关注</h2><el-checkbox v-model="onlyUncovered">仅看计入在途、待收后仍不足</el-checkbox><el-tag type="warning">低于安全库存 {{ attention.length }}</el-tag></div><div class="low-stock-grid"><article v-for="row in attentionRows" :key="`${row.warehouse_id}-${row.part_id}`"><img :src="imageFor(row)" :alt="row.name" loading="lazy" /><div><strong>{{ row.name }}</strong><p>{{ row.warehouse }} · {{ row.sku }}</p><span>现有 {{ row.quantity }} / 安全库存 {{ row.safety_stock }} {{ row.unit }}</span><p class="stock-pending">调拨在途 {{ row.in_transit }} 件 · 采购待收货 {{ row.purchase_pending }} 件</p><p v-if="!row.initialized" class="muted">尚未建账</p></div></article><p v-if="!attention.length" class="muted">{{ onlyUncovered ? '计入本仓在途和采购待收后，没有仍低于安全库存的备件。' : '当前筛选范围内没有低于安全库存的备件。' }}</p></div><el-pagination v-if="attention.length > 12" v-model:current-page="attentionPage" :page-size="12" :total="attention.length" layout="prev, pager, next" /><p class="table-note">仅显示现有量低于安全库存的备件，包含零库存。筛选勾选后，按“现有 + 本仓调拨在途 + 本仓采购待收货 &lt; 安全库存”判断。调拨只计目的地为当前仓库的已发出未收货数量；采购只计收货仓为当前仓库的已确认/部分收货未收数量，不含草稿、待审核及已取消数量。安全库存为0时不计入。尚未建账不代表已经盘点确认无实物。当前备件：{{ data.part_name || '全部备件' }}；库存统一按件计数，一件对应一个完整库存包装，特殊包装见备件规格。现有、采购待收及在途是当前快照（{{ data.snapshot_date }}）；日期范围仅约束趋势和领用量。</p></section>
  </template>
</template>
