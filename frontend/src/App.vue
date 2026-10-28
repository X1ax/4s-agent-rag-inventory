<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { House, ShoppingCart, Download, Upload, Switch, Box, TrendCharts, Tools, Van, OfficeBuilding, List, ChatDotRound, CircleCheck, User, DocumentChecked } from '@element-plus/icons-vue'
import { allCatalog, api, roleLabels, session } from './api'
import CatalogPanel from './CatalogPanel.vue'
import InventoryPanel from './InventoryPanel.vue'
import AccountsPanel from './AccountsPanel.vue'
import AuditPanel from './AuditPanel.vue'
import DocumentsPanel from './DocumentsPanel.vue'
import StatisticsPanel from './StatisticsPanel.vue'
import ProfitPanel from './ProfitPanel.vue'
import KnowledgePanel from './KnowledgePanel.vue'
import ReviewCenter from './ReviewCenter.vue'
const focusDocument = ref(null)
function openAgentDocument(target) { focusDocument.value = target; nav.value = target.kind }
import { imageFor } from './media'

const nav = ref('home'), loggingIn = ref(false), loginError = ref(''), ready = ref('checking'), catalogError = ref('')
const loginForm = reactive({ username: 'demo_admin', password: '' })
const catalogs = reactive({ parts: [], suppliers: [], warehouses: [] })
const menuIcons = { home: House, purchase: ShoppingCart, receipt: Download, issue: Upload, transfer: Switch, balances: Box, profits: TrendCharts, parts: Tools, suppliers: Van, warehouses: OfficeBuilding, ledger: List, knowledge: ChatDotRound, reviews: CircleCheck, users: User, audit: DocumentChecked }
const menus = computed(() => [
  { id: 'home', label: '业务概览', note: '从库存快照到备件流转，查看已记录的门店业务' },
  { id: 'purchase', label: '采购管理', note: '创建采购草稿、确认采购与管理未收余量' },
  { id: 'receipt', label: '采购入库', note: '按采购单登记实际收货，追溯每笔入库' },
  { id: 'issue', label: '领用出库', note: '登记维修领用，过账时核对并扣减可用库存' },
  { id: 'transfer', label: '跨仓调拨', note: '整单发出与收货，在途库存单独追踪' },
  { id: 'balances', label: '库存总览', note: '查看各仓备件余额与库存状态' },
  { id: 'profits', label: '毛利分析', note: '按已过账出库结算与实际库存成本，分析收费毛利与质保消耗' },
  { id: 'parts', label: '备件资料', note: '维护备件编码、单价、安全库存与供应商' },
  { id: 'suppliers', label: '供应商管理', note: '维护供应商联系方式与采购交期' },
  { id: 'warehouses', label: '仓库管理', note: '维护本店仓库及启停状态' },
  { id: 'ledger', label: '库存流水', note: '追溯期初、采购入库、领用和调拨收发的库存变化' },
  { id: 'knowledge', label: '智能助手', note: '结合业务资料与公开信息，协助查询、分析和日常工作' },
  { id: 'reviews', label: '审核中心', note: '统一核对采购、调拨和知识资料，查看审核结论与角色权限' },
  ...(session.user?.role === 'admin' ? [
    { id: 'users', label: '账号权限', note: '维护账号和必要业务角色' },
    { id: 'audit', label: '操作审计', note: '查看成功提交的业务操作记录' },
  ] : []),
])
const current = computed(() => menus.value.find(item => item.id === nav.value) || menus.value[0])

async function check() {
  ready.value = 'checking'
  try { await Promise.all([api('/health'), api('/ready')]); ready.value = 'ok' }
  catch { ready.value = 'error' }
}
async function loadCatalogs() {
  try {
    const [parts, suppliers, warehouses] = await Promise.all([allCatalog('/parts'), allCatalog('/suppliers'), allCatalog('/warehouses')])
    Object.assign(catalogs, { parts, suppliers, warehouses }); catalogError.value = ''
  } catch (error) { Object.assign(catalogs, { parts: [], suppliers: [], warehouses: [] }); catalogError.value = error.message }
}
async function login() {
  loggingIn.value = true; loginError.value = ''
  try {
    const result = await api('/auth/login', { method: 'POST', body: JSON.stringify(loginForm) })
    session.token = result.access_token; session.user = result.user
    loginForm.password = ''; nav.value = 'home'; window.scrollTo(0, 0); await loadCatalogs()
  } catch (error) { loginError.value = error.message }
  finally { loggingIn.value = false }
}
function logout() {
  session.token = ''; session.user = null; loginForm.password = ''; loginError.value = ''
  Object.assign(catalogs, { parts: [], suppliers: [], warehouses: [] })
}
watch(nav, () => window.scrollTo(0, 0))
onMounted(() => { check(); if (session.user) loadCatalogs() })
</script>

<template>
  <div v-if="!session.user" class="login-layout">
    <section class="login-intro">
      <div class="identity"><span class="mark">4S</span><span>备件智能库存管理系统</span></div>
      <div class="login-copy"><p class="eyebrow">基于 Agent 与 RAG</p><h1>库存有据可查，<br />业务从此有序。</h1><p>连接备件资料、库存流转与日常办理，<br />让每一步业务都有据可循。</p><div class="login-capabilities"><article><strong>库存与流转</strong><span>采购、入库、领用与跨仓调拨，统一记录。</span></article><article><strong>知识与检索</strong><span>资料审核入库，回答可查看原文依据。</span></article><article><strong>协作与办理</strong><span>助手协助查询与整理提案，业务变更经人工确认。</span></article></div></div>
    </section>
    <section class="login-side">
      <el-form class="login-card" label-position="top" @submit.prevent="login">
        <el-tag effect="plain">管理工作台</el-tag><h2>登录系统</h2><p class="muted">使用已开通的账号访问备件管理工作台。</p>
          <el-form-item label="用户名"><el-input v-model="loginForm.username" autocomplete="username" required maxlength="40" size="large" /></el-form-item>
          <el-form-item label="密码"><el-input v-model="loginForm.password" type="password" autocomplete="current-password" show-password required maxlength="128" size="large" /></el-form-item>
        <p v-if="loginError" class="error-text" role="alert">{{ loginError }}</p>
        <el-button type="primary" native-type="submit" size="large" :loading="loggingIn" class="login-submit">进入工作台</el-button>
        <div class="demo-accounts"><span>本机演示账号</span><div><el-button v-for="(label, role) in roleLabels" :key="role" :class="[`account-${role}`, { selected: loginForm.username === `demo_${role}` }]" :aria-pressed="loginForm.username === `demo_${role}`" size="small" @click="loginForm.username = `demo_${role}`">{{ label }}</el-button></div></div>
        <div class="connection"><span :class="['dot', ready]" />{{ ready === 'ok' ? '应用与数据库连接正常' : ready === 'checking' ? '正在检测服务' : '服务连接异常' }}<el-button link @click="check">重新检测</el-button></div>
      </el-form>
    </section>
  </div>
  <div v-else class="workspace">
    <aside class="sidebar">
      <div class="identity"><span class="mark">4S</span><div>备件库存管理<small>Agent & RAG</small></div></div>
      <p class="nav-caption">业务工作台</p>
      <nav aria-label="主导航"><button v-for="item in menus" :key="item.id" :class="{ selected: nav === item.id }" :aria-current="nav === item.id ? 'page' : undefined" @click="nav = item.id"><component :is="menuIcons[item.id]" class="nav-icon" aria-hidden="true" /><span>{{ item.label }}</span></button></nav>
      <div class="sidebar-bottom"><el-tag :type="{ admin: 'primary', storekeeper: 'success', manager: 'warning' }[session.user.role]" effect="plain" class="sidebar-role">{{ roleLabels[session.user.role] }}</el-tag><el-button link class="sidebar-logout" @click="logout">退出登录</el-button></div>
    </aside>
    <div class="main-shell">
      <header class="topbar"><span>基于Agent与RAG的4S店备件智能库存管理系统</span></header>
      <main :class="{ 'assistant-page': nav === 'knowledge' }">
        <div class="page-heading"><div><p class="eyebrow">门店备件 / {{ current.label }}</p><h1>{{ current.label }}</h1><p class="muted">{{ current.note }}</p></div></div>
        <div v-if="nav === 'balances'" class="stats">
          <article><span>备件资料</span><strong>{{ catalogs.parts.length }}</strong><small>种 · 含停用资料</small></article><article><span>仓库资料</span><strong>{{ catalogs.warehouses.length }}</strong><small>个 · 含停用资料</small></article><article><span>供应商资料</span><strong>{{ catalogs.suppliers.length }}</strong><small>家 · 含停用资料</small></article>
        </div>
        <div v-if="catalogError" class="error-banner" role="alert">资料加载失败：{{ catalogError }}<el-button link @click="loadCatalogs">重新加载</el-button></div>
        <div v-if="nav === 'home'" class="dashboard"><section class="dashboard-hero"><div><p class="eyebrow">每一件备件，每一次流转</p><h2>清晰掌握库存，<br />有序连接业务。</h2><p>采购、领用与调拨，始终有据可查。</p><div class="hero-actions"><el-button type="primary" @click="nav = 'balances'">查看库存</el-button><el-button v-if="['admin','storekeeper'].includes(session.user.role)" @click="nav = 'purchase'">办理采购</el-button><el-button v-if="['admin','storekeeper'].includes(session.user.role)" @click="nav = 'transfer'">办理调拨</el-button><el-button @click="nav = 'knowledge'">智能助手</el-button></div></div><img src="/images/dealership-hero.png" alt="简约现代汽车门店外观" /></section><div class="warehouse-strip"><button v-for="item in catalogs.warehouses" :key="item.id" @click="nav = 'warehouses'"><img :src="imageFor(item, 'warehouses')" :alt="`${item.name}演示配图`" /><div><strong>{{ item.name }}</strong><small>{{ item.location }} · {{ item.active ? '启用' : '停用' }}</small></div><span>查看 →</span></button></div><StatisticsPanel :warehouses="catalogs.warehouses" :parts="catalogs.parts" /></div>
        <DocumentsPanel v-else-if="['purchase','receipt','issue','transfer'].includes(nav)" :kind="nav" :parts="catalogs.parts" :suppliers="catalogs.suppliers" :warehouses="catalogs.warehouses" :focus-document="focusDocument?.kind === nav ? focusDocument.id : null" @open-reviews="nav = 'reviews'" />
        <CatalogPanel v-else-if="['parts', 'suppliers', 'warehouses'].includes(nav)" :kind="nav" :suppliers="catalogs.suppliers" :parts="catalogs.parts" @changed="loadCatalogs" />
        <InventoryPanel v-else-if="['balances', 'ledger'].includes(nav)" :kind="nav" :warehouses="catalogs.warehouses" :parts="catalogs.parts" />
        <ProfitPanel v-else-if="nav === 'profits'" :warehouses="catalogs.warehouses" :parts="catalogs.parts" @open-document="openAgentDocument" />
        <AccountsPanel v-else-if="nav === 'users'" />
        <AuditPanel v-else-if="nav === 'audit'" />
        <KnowledgePanel v-else-if="nav === 'knowledge'" @open-document="openAgentDocument" />
        <ReviewCenter v-else-if="nav === 'reviews'" />
      </main>
    </div>
  </div>
</template>
