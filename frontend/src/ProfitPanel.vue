<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from './api'
import { moneyText } from './pricing'
import { imageFor } from './media'
import TrendChart from './TrendChart.vue'
import { dateRange } from './trendChart.js'

const props = defineProps({ warehouses: Array, parts: Array })
const emit = defineEmits(['open-document'])
const dateText = value => new Date(value - new Date(value).getTimezoneOffset() * 60000).toISOString().slice(0, 10)
const form = reactive({ warehouse_id: null, category: null, part_id: null, date_from: dateText(Date.now() - 29 * 86400000), date_to: dateText(Date.now()), sort_by: 'margin' })
const data = ref(null), loading = ref(false), error = ref(''), page = ref(1), detailsPage = ref(1)
const categories = computed(() => [...new Set(props.parts.map(part => part.category))].sort())
const parts = computed(() => props.parts.filter(part => !form.category || part.category === form.category))
const metrics = [{ key: 'revenue', label: '收费结算金额' }, { key: 'paid_cost', label: '收费维修成本' }, { key: 'gross_profit', label: '收费维修毛利' }, { key: 'warranty_cost', label: '质保消耗成本' }]
const partImage = id => imageFor(props.parts.find(part => part.id === id) || {})
const warehouseImage = id => imageFor(props.warehouses.find(warehouse => warehouse.id === id) || {}, 'warehouses')
const issueLabels = { paid_repair: '收费维修', warranty: '质保维修' }
const percent = value => value == null ? '暂无' : `${(Number(value) * 100).toFixed(2)}%`
let version = 0
async function load() {
  const current = ++version
  loading.value = true
  try {
    const params = new URLSearchParams({ page: page.value, details_page: detailsPage.value, page_size: 7 })
    for (const [key, value] of Object.entries(form)) if (value !== null && value !== '') params.set(key, value)
    const result = await api(`/profits?${params}`)
    if (current === version) { data.value = result; error.value = '' }
  } catch (failure) { if (current === version) { data.value = null; error.value = failure.message } }
  finally { if (current === version) loading.value = false }
}
function search() { page.value = 1; detailsPage.value = 1; load() }
function clear() { Object.assign(form, { warehouse_id: null, category: null, part_id: null, date_from: dateText(Date.now() - 29 * 86400000), date_to: dateText(Date.now()), sort_by: 'margin' }); search() }
const useTotal = computed(() => (data.value?.totals.paid_quantity || 0) + (data.value?.totals.warranty_quantity || 0))
const chart = computed(() => {
  const dates = dateRange(data.value?.date_from, data.value?.date_to)
  const rows = new Map((data.value?.trend || []).map(row => [row.business_date, row]))
  return { dates, series: [
    { key: 'revenue', label: '收费结算金额', color: '#1677ff' },
    { key: 'gross_profit', label: '收费毛利', color: '#8b5cf6' },
    { key: 'warranty_cost', label: '质保成本', color: '#ff8b37' },
  ].map(series => ({ ...series, values: dates.map(day => rows.get(day)?.[series.key] ?? '0.00') })) }
})
const issueChart = computed(() => {
  const rows = new Map((data.value?.trend || []).map(row => [row.business_date, row]))
  return [
    { key: 'paid_quantity', label: '收费维修', color: '#1677ff' },
    { key: 'warranty_quantity', label: '质保维修', color: '#ff8b37' },
  ].map(series => ({ ...series, values: chart.value.dates.map(day => rows.get(day)?.[series.key] || 0) }))
})
const rankValue = row => form.sort_by === 'margin_rate' ? percent(row.gross_margin_rate) : form.sort_by === 'quantity' ? `${row.quantity} 件` : `¥ ${moneyText(row.gross_profit)}`
onMounted(load)
</script>

<template>
  <section class="panel profit-filters"><form class="filters" @submit.prevent="search">
    <el-select v-model="form.warehouse_id" clearable :value-on-clear="null" placeholder="全部仓库" aria-label="毛利仓库"><el-option v-for="item in warehouses" :key="item.id" :value="item.id" :label="item.name" /></el-select>
    <el-select v-model="form.category" clearable :value-on-clear="null" placeholder="全部分类" aria-label="毛利备件分类" @change="form.part_id = null"><el-option v-for="category in categories" :key="category" :value="category" :label="category || '未分类'" /></el-select>
    <el-select v-model="form.part_id" clearable filterable :value-on-clear="null" placeholder="全部备件" aria-label="毛利备件"><el-option v-for="part in parts" :key="part.id" :value="part.id" :label="`${part.sku} · ${part.name}`" /></el-select>
    <label class="date-filter">从<input v-model="form.date_from" type="date" class="date-input" required /></label><label class="date-filter">至<input v-model="form.date_to" type="date" class="date-input" required /></label>
    <el-button type="primary" native-type="submit" :loading="loading">查询毛利</el-button><el-button @click="clear">清空筛选</el-button>
  </form><p class="table-note">按已过账出库的业务日期统计，仓库留空表示全仓。仅含备件成交结算与实际库存成本，不计人工、房租等费用；扣除质保后的备件贡献不等于净利润。</p></section>
  <div v-if="error" class="error-banner" role="alert">毛利查询失败：{{ error }}。旧统计结果已清除。<el-button link :loading="loading" @click="load">重试查询</el-button></div>
  <div v-if="loading" class="muted profit-loading" role="status">正在查询毛利数据…</div>
  <div v-if="data" class="profit-content">
    <p v-if="data.notice" class="profit-notice">{{ data.notice }}</p>
    <p class="profit-notice">历史模拟补录价格、用途与成本仅用于项目演示；明细保留来源标记，不能视为真实门店经营结果。</p>
    <div v-if="data.totals.missing_lines" class="error-banner" role="alert">当前范围有 {{ data.totals.missing_lines }} 条明细、{{ data.totals.missing_quantity }} 件存在用途、结算或成本缺失。收费毛利仅覆盖结算与成本均完整的收费明细，不代表全量毛利。缺失行中已知结算 {{ moneyText(data.totals.uncovered_revenue) }} 元、已知成本 {{ moneyText(data.totals.uncovered_cost) }} 元，其他未覆盖金额无法确认。</div>
    <div class="stats profit-stats"><article v-for="metric in metrics" :key="metric.key"><span>{{ metric.label }}</span><strong :class="{ negative: Number(data.totals[metric.key]) < 0 }">{{ moneyText(data.totals[metric.key]) }}</strong><small>元</small></article></div>
    <p class="profit-summary">收费维修毛利率 <strong>{{ percent(data.totals.gross_margin_rate) }}</strong><span>扣除质保后的备件贡献 <strong :class="{ negative: Number(data.totals.contribution) < 0 }">¥ {{ moneyText(data.totals.contribution) }}</strong></span></p>
    <div class="analytics-grid">
      <section class="panel chart-card"><div class="section-heading"><h2>结算与毛利趋势</h2><span class="muted">{{ data.date_from }} — {{ data.date_to }}</span></div><TrendChart :dates="chart.dates" :series="chart.series" unit="元" label="结算与毛利趋势" />
        <p class="table-note">结算金额与质保成本保留已知金额，收费毛利仅含完整明细；无记录日期按零展示，负毛利正常保留。采购、入库与调拨不构成结算收入。</p>
      </section>
      <section class="panel chart-card"><div class="section-heading"><h2>收费 / 质保领用趋势</h2><span class="muted">{{ data.date_from }} — {{ data.date_to }}</span></div>
        <p class="muted">按业务日期统计已过账出库件数 · 合计 {{ useTotal }} 件</p>
        <TrendChart :dates="chart.dates" :series="issueChart" stacked unit="件" label="收费与质保领用每日堆叠柱状图" />
        <p class="table-note">每根柱子代表一天，蓝色为收费维修，橙色为质保维修；无记录日期按零展示，未登记用途的出库不纳入两类合计。质保结算金额为0，消耗实际成本。</p>
      </section>
    </div>
    <section class="panel"><div class="section-heading"><h2>备件毛利排行</h2><el-select v-model="form.sort_by" clearable :value-on-clear="'margin'" aria-label="毛利排序方式" @change="search"><el-option label="按毛利额" value="margin" /><el-option label="按毛利率" value="margin_rate" /><el-option label="按领用数量" value="quantity" /></el-select></div>
      <el-table :data="data.ranking" empty-text="当前范围暂无可统计备件"><el-table-column label="备件" min-width="250"><template #default="{ row }"><div class="profit-entity"><img :src="partImage(row.part_id)" :alt="row.name" loading="lazy" /><div>{{ row.name }}<small class="quote-source">{{ row.sku }}</small></div></div></template></el-table-column><el-table-column label="排序值" min-width="145"><template #default="{ row }">{{ rankValue(row) }}</template></el-table-column><el-table-column label="收费结算 / 元" min-width="135"><template #default="{ row }">{{ moneyText(row.revenue) }}</template></el-table-column><el-table-column label="收费成本 / 元" min-width="135"><template #default="{ row }">{{ moneyText(row.paid_cost) }}</template></el-table-column><el-table-column label="收费毛利 / 元" min-width="135"><template #default="{ row }"><span :class="{ negative: Number(row.gross_profit) < 0 }">{{ moneyText(row.gross_profit) }}</span></template></el-table-column><el-table-column label="毛利率" width="115"><template #default="{ row }">{{ percent(row.gross_margin_rate) }}</template></el-table-column><el-table-column label="领用数量" min-width="150"><template #default="{ row }">合计 {{ row.quantity }} {{ row.unit }}<small class="quote-source">收费 {{ row.paid_quantity }} / 质保 {{ row.warranty_quantity }}</small></template></el-table-column></el-table>
      <div class="table-footer"><span>共 {{ data.ranking_total }} 种备件，每页7条</span><el-pagination v-model:current-page="page" :page-size="7" :total="data.ranking_total" layout="prev, pager, next" @current-change="load" /></div>
    </section>
    <section class="panel"><h2>出库结算与成本追溯</h2><el-table :data="data.details" empty-text="当前页无出库明细"><el-table-column label="来源单据" width="135"><template #default="{ row }"><el-button link type="primary" @click="emit('open-document', { id: row.document_id, kind: 'issue' })">出库 #{{ row.document_id }}</el-button></template></el-table-column><el-table-column prop="business_date" label="业务日期" width="115" /><el-table-column label="仓库" min-width="200"><template #default="{ row }"><div class="profit-entity"><img class="profit-warehouse-image" :src="warehouseImage(row.warehouse_id)" :alt="row.warehouse" loading="lazy" /><span>{{ row.warehouse }}</span></div></template></el-table-column><el-table-column label="备件" min-width="250"><template #default="{ row }"><div class="profit-entity"><img :src="partImage(row.part_id)" :alt="row.name" loading="lazy" /><div>{{ row.name }}<small class="quote-source">{{ row.sku }}</small></div></div></template></el-table-column><el-table-column label="用途" width="110"><template #default="{ row }">{{ issueLabels[row.issue_type] || '用途缺失' }}<small v-if="row.complete === false" class="error-text">统计要素缺失</small></template></el-table-column><el-table-column prop="quantity" label="数量 / 件" width="100" /><el-table-column label="结算价 / 元" width="130"><template #default="{ row }">{{ moneyText(row.sale_unit_price) }}</template></el-table-column><el-table-column label="结算金额 / 元" width="140"><template #default="{ row }">{{ moneyText(row.revenue) }}</template></el-table-column><el-table-column label="实际成本 / 元" width="140"><template #default="{ row }">{{ row.cost_amount == null ? '成本缺失' : moneyText(row.cost_amount) }}</template></el-table-column><el-table-column label="收费毛利 / 元" width="140"><template #default="{ row }">{{ row.issue_type === 'warranty' ? '—（质保）' : moneyText(row.gross_profit) }}</template></el-table-column><el-table-column label="价格与成本来源" min-width="220"><template #default="{ row }">{{ row.pricing_source || '来源未记录' }}</template></el-table-column></el-table><div class="table-footer"><span>共 {{ data.details_total }} 条明细，每页7条</span><el-pagination v-model:current-page="detailsPage" :page-size="7" :total="data.details_total" layout="prev, pager, next" @current-change="load" /></div><p class="table-note">单据详情保留当时结算与实际成本快照；历史缺失不以当前供货价或当前售价替代。</p></section>
  </div>
</template>

<style scoped>
.profit-content { padding: 0 24px 24px; }
.profit-content > .panel { margin-bottom: 20px; }
.profit-stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.profit-stats strong { font-size: 25px; overflow-wrap: anywhere; }
.profit-notice { font-size: 12px; line-height: 1.7; color: #7b725e; }
.profit-summary { display: flex; gap: 30px; flex-wrap: wrap; font-size: 13px; color: #6e6e73; margin: 0 0 24px; }
.profit-summary strong { color: #333942; margin-left: 10px; }
.section-heading > .el-select { width: 150px; }
.profit-entity { display: flex; align-items: center; gap: 12px; }
.profit-entity img { width: 52px; height: 44px; flex: none; object-fit: contain; border-radius: 7px; background: #f5f5f7; }
.profit-entity .profit-warehouse-image { object-fit: cover; }
.profit-loading { padding: 12px 24px; }
@media (max-width: 1000px) { .profit-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 700px) { .profit-content { padding: 0 16px 16px; } .profit-stats strong { font-size: 20px; } }
</style>
