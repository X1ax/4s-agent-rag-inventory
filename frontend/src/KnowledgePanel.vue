<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessageBox, ElTabs, ElTabPane } from 'element-plus'
import { api } from './api'
import KnowledgeAnswer from './KnowledgeAnswer.vue'
import AgentProposal from './AgentProposal.vue'
import KnowledgeSubmissions from './KnowledgeSubmissions.vue'
const emit = defineEmits(['open-document'])
const proposals = ref([])

const threads = ref([]), currentId = ref(''), turns = ref([]), draft = ref(''), busy = ref(false), loading = ref(false)
const error = ref(''), historyError = ref(''), pending = ref(null), end = ref(null), showHistory = ref(true)
const sourcesOpen = ref(false), sources = ref([]), sourceError = ref(''), sourceLoading = ref(false), webConfigured = ref(false)
const sourceTab = ref('catalog')
const allowWeb = ref(true), scope = ref('draft_preview')
const threadMenu = ref(null), menuElement = ref(null)
const examples = ['采购入库前需要核对哪些信息？', '调拨发出后，两边仓库的库存如何变化？', '联网查询汽车备件仓储的公开规范']
let revision = 0
async function refreshThreads() {
  const data = await api('/assistant/threads')
  threads.value = data.items; webConfigured.value = data.web_configured; historyError.value = ''
}
async function scrollBottom() { await nextTick(); end.value?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }
async function selectThread(id) {
  if (busy.value) return
  const current = ++revision
  currentId.value = id; turns.value = []; error.value = ''; historyError.value = ''; pending.value = null; loading.value = true
  try { const data = await api(`/assistant/threads/${id}`); if (current === revision) { turns.value = data.turns; proposals.value = data.proposals || []; await scrollBottom() } }
  catch (e) { if (current === revision) historyError.value = e.message }
  finally { if (current === revision) loading.value = false }
}
function newConversation() {
  if (busy.value) return
  revision++; currentId.value = ''; turns.value = []; proposals.value = []; draft.value = ''; pending.value = null; error.value = ''; historyError.value = ''; loading.value = false
}
async function deleteConversation(thread) {
  try {
    await ElMessageBox.confirm(`删除“${thread.title}”及其中的聊天记录？`, '删除会话', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
  } catch { return }
  try {
    await api(`/assistant/threads/${thread.id}`, { method: 'DELETE' })
    if (currentId.value === thread.id) newConversation()
    await refreshThreads()
  } catch (e) { historyError.value = e.message }
}
async function openThreadMenu(event, thread) {
  if (busy.value) return
  const rect = event.currentTarget.getBoundingClientRect()
  threadMenu.value = { thread, x: Math.min(event.clientX || rect.left + 20, window.innerWidth - 170), y: Math.min(event.clientY || rect.bottom, window.innerHeight - 110) }
  await nextTick(); menuElement.value?.querySelector('button')?.focus()
}
function closeThreadMenu() { threadMenu.value = null }
function removeFromMenu() { const thread = threadMenu.value.thread; closeThreadMenu(); deleteConversation(thread) }
async function renameFromMenu() {
  const thread = threadMenu.value.thread; closeThreadMenu()
  let title
  try {
    const result = await ElMessageBox.prompt('请输入会话名称', '重命名会话', { inputValue: thread.title, confirmButtonText: '保存', cancelButtonText: '取消',
      inputValidator: value => Boolean(value?.trim()) && value.length <= 80, inputErrorMessage: '请输入1至80个字符的会话名称' })
    title = result.value.trim()
  } catch { return }
  try { await api(`/assistant/threads/${thread.id}`, { method: 'PATCH', body: JSON.stringify({ title }) }); await refreshThreads() }
  catch (e) { historyError.value = e.message }
}
async function send(retry = null) {
  if (busy.value || loading.value || (!retry && !draft.value.trim()) || !scope.value) return
  busy.value = true; error.value = ''
  const message = retry?.question || draft.value.trim()
  const requestId = retry?.client_request_id || crypto.randomUUID().replaceAll('-', '')
  const messageScope = retry?.scope || scope.value
  try {
    if (!currentId.value) { const thread = await api('/assistant/threads', { method: 'POST' }); currentId.value = thread.id; threads.value.unshift(thread) }
    if (!retry) { pending.value = { question: message }; draft.value = '' }
    else {
      const row = turns.value.find(t => t.id === retry.id)
      if (row) row.status = 'pending'
      if (draft.value.trim() === message) draft.value = ''
    }
    await scrollBottom()
    await api(`/assistant/threads/${currentId.value}/messages`, { method: 'POST', timeout: 125000, body: JSON.stringify({ message, client_request_id: requestId, knowledge_scope: messageScope, allow_web: allowWeb.value }) })
  } catch (e) {
    error.value = e.name === 'TimeoutError' ? '本轮等待超时，结果可能已保存。请刷新会话确认后再重试。' : e.message
    if (!retry && pending.value) draft.value = message
  } finally {
    try { if (currentId.value) { const data = await api(`/assistant/threads/${currentId.value}`); turns.value = data.turns; proposals.value = data.proposals || [] } await refreshThreads() }
    catch (e) { historyError.value = e.message }
    pending.value = null; busy.value = false; await scrollBottom()
  }
}
function onEnter(event) { if (!event.shiftKey && !event.isComposing) { event.preventDefault(); send() } }
async function loadSources() {
  if (!scope.value) { sources.value = []; return }
  sourceLoading.value = true; sourceError.value = ''
  try { const results = await Promise.all(['inventory_policies','general_diagnostics'].map(kb => api(`/knowledge/documents?knowledge_base=${kb}&knowledge_scope=${scope.value}`))); sources.value = results.flatMap(r => r.items) }
  catch (e) { sourceError.value = e.message; sources.value = [] }
  finally { sourceLoading.value = false }
}
async function inspectSources() { sourcesOpen.value = true; await loadSources() }
async function refreshProposals() {
  try { const data = await api(`/assistant/threads/${currentId.value}`); proposals.value = data.proposals || [] }
  catch (e) { error.value = e.message }
}
onMounted(async () => {
  document.addEventListener('pointerdown', closeThreadMenu)
  window.addEventListener('scroll', closeThreadMenu, true)
  try { await refreshThreads() } catch (e) { historyError.value = e.message }
})
onUnmounted(() => { document.removeEventListener('pointerdown', closeThreadMenu); window.removeEventListener('scroll', closeThreadMenu, true) })
</script>

<template>
  <section class="assistant-shell">
    <aside v-if="showHistory" class="chat-history">
      <div class="history-toolbar"><el-button class="new-chat" :disabled="busy" @click="newConversation">＋ 新建会话</el-button></div><p class="history-caption">最近会话</p>
      <div v-if="historyError" class="error-text" role="alert">{{ historyError }}<el-button link @click="refreshThreads">重试</el-button></div>
      <p v-if="!threads.length" class="muted">你的会话会保存在本机</p>
      <div v-for="thread in threads" :key="thread.id" class="history-row"><button :class="['history-item', { active: currentId === thread.id }]" :disabled="busy" :title="thread.title" @click="selectThread(thread.id)" @contextmenu.prevent="openThreadMenu($event, thread)" @keydown.shift.f10.prevent="openThreadMenu($event, thread)">{{ thread.title }}</button></div>
    </aside>
    <div class="chat-main">
      <header class="chat-header"><div><el-button link @click="showHistory = !showHistory">{{ showHistory ? '收起会话' : '历史会话' }}</el-button><el-button v-if="!showHistory" link :disabled="busy" @click="newConversation">新建会话</el-button></div></header>
      <div class="chat-scroll">
        <div v-if="!turns.length && !pending && !loading" class="chat-welcome"><span class="assistant-mark">4S</span><h2>今天需要处理什么？</h2><p>查询信息、梳理业务，让日常工作更高效。</p><div class="chat-examples"><button v-for="text in examples" :key="text" @click="draft = text">{{ text }}<span>↗</span></button></div></div>
        <p v-if="loading" class="muted">正在读取会话…</p>
        <template v-for="(turn, index) in turns" :key="turn.id">
          <div class="chat-user"><div>{{ turn.question }}</div></div>
          <article class="chat-assistant"><span class="assistant-avatar">4S</span><div class="chat-response"><KnowledgeAnswer v-if="turn.answer" :answer="turn.answer" @choose-action="draft = $event" /><div v-else-if="turn.status === 'failed'" class="chat-failure" role="alert"><p>{{ turn.error_detail || '本轮未取得完整回答' }}</p><el-button v-if="index === turns.length - 1" link type="primary" :disabled="busy" @click="send(turn)">重试这条消息</el-button></div><p v-else class="muted">{{ busy ? '正在处理，请稍候…' : '本轮尚在处理中，请刷新会话查看结果。' }}<el-button v-if="!busy" link @click="selectThread(currentId)">刷新会话</el-button></p></div></article>
          <AgentProposal v-for="proposal in proposals.filter(p => p.turn_id === turn.id)" :key="proposal.id" :proposal="proposal" :thread-id="currentId" :disabled="busy" @changed="refreshProposals" @choose-action="draft = $event" @open-document="emit('open-document', $event)" />
        </template>
        <template v-if="pending"><div class="chat-user"><div>{{ pending.question }}</div></div><article class="chat-assistant"><span class="assistant-avatar">4S</span><p class="chat-thinking">正在理解问题、查阅资料…</p></article></template>
        <div ref="end" />
      </div>
      <div class="chat-composer"><div v-if="error" class="error-banner" role="alert">{{ error }}</div><div class="composer-box"><el-input v-model="draft" type="textarea" :autosize="{ minRows: 2, maxRows: 7 }" :maxlength="2000" placeholder="向智能助手发送消息…" aria-label="发送给智能助手的消息" :disabled="busy" @keydown.enter="onEnter" /><div class="composer-toolbar"><div><el-switch v-model="allowWeb" :disabled="busy || !webConfigured" size="small" active-text="联网搜索" /><el-button link class="composer-source" @click="inspectSources">本地知识库</el-button></div><el-button type="primary" round :disabled="!draft.trim() || busy || loading || !scope" :loading="busy" @click="send()">发送</el-button></div></div><p class="composer-note">Enter 发送 · Shift + Enter 换行 · 业务变更须由人工审核确认</p></div>
    </div>
    <div v-if="threadMenu" ref="menuElement" class="thread-context-menu" role="menu" aria-label="会话操作" :style="{ left: `${threadMenu.x}px`, top: `${threadMenu.y}px` }" @pointerdown.stop @keydown.esc="closeThreadMenu"><button role="menuitem" @click="renameFromMenu">重命名会话</button><button role="menuitem" class="danger" @click="removeFromMenu">删除会话</button></div>
    <el-dialog v-model="sourcesOpen" title="本地知识库" width="min(960px, 94vw)"><el-tabs v-model="sourceTab"><el-tab-pane label="资料目录" name="catalog"><p>这里收录了库存制度、仓储规范和通用故障排查手册。助手回答相关问题时会检索这些文档；联网搜索则查找外部公开网页，两者分别标明来源。</p><el-select v-model="scope" clearable placeholder="选择资料范围" :disabled="busy" @change="loadSources"><el-option label="全部本地资料（默认）" value="draft_preview" /><el-option label="仅正式发布资料" value="released_only" /></el-select><p class="muted">{{ webConfigured ? 'Tavily 联网搜索已配置。只发送必要的公开搜索词。' : 'Tavily 尚未配置。' }}</p><p v-if="sourceLoading">正在核对资料…</p><p v-if="sourceError" class="error-text" role="alert">{{ sourceError }}</p><el-table :data="sources"><el-table-column prop="title" label="资料" min-width="220" /><el-table-column prop="document_version" label="版本" min-width="120" /><el-table-column prop="native_format" label="格式" width="80" /><el-table-column label="可用性" min-width="140"><template #default="{ row }">{{ row.eligible ? '可供参考' : '当前范围不可用' }}</template></el-table-column></el-table></el-tab-pane><el-tab-pane label="上传记录" name="submissions" lazy><KnowledgeSubmissions @indexed="loadSources" /></el-tab-pane></el-tabs></el-dialog>
  </section>
</template>

<style scoped>
.composer-toolbar > .el-button { min-width: 80px; justify-content: center; text-align: center; }
.composer-source.el-button.is-link:not(.is-disabled):hover, .composer-source.el-button.is-link:not(.is-disabled):focus-visible { color: #0071e3; }
.chat-examples button:hover, .chat-examples button:focus-visible { color: #0071e3; }
.thread-context-menu { position: fixed; z-index: 2200; min-width: 155px; background: white; border: 1px solid #e5e7eb; border-radius: 10px; padding: 6px; box-shadow: 0 8px 28px #13213b20; }
.thread-context-menu button { width: 100%; text-align: left; padding: 10px 14px; border: 0; border-radius: 6px; background: white; color: #424853; cursor: pointer; }
.thread-context-menu button:hover, .thread-context-menu button:focus-visible { background: #f0f5fc; color: #0071e3; }
.thread-context-menu button.danger { color: #d94b4b; }
.thread-context-menu button.danger:hover { background: #fff1ef; }

.assistant-shell{display:flex;min-height:650px;height:calc(100vh - 220px);background:white;border:1px solid #e5e7eb;border-radius:22px;overflow:hidden}.chat-history{width:205px;flex-shrink:0;background:#f9fafb;padding:0 12px 20px;border-right:1px solid #eceef2;overflow-y:auto}.history-toolbar{height:56px;display:flex;align-items:center;border-bottom:1px solid transparent}.new-chat{width:100%;height:32px;border-radius:10px}.history-row{display:flex;align-items:center;gap:3px}.history-row>.history-item{flex:1;min-width:0}.history-caption{font-size:12px;color:#9296a0;margin:28px 10px 12px}.history-item{font-size:13px;display:block;width:100%;border:0;text-align:left;padding:12px;border-radius:10px;color:#51555e;background:transparent;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:4px 0}.history-item:hover,.history-item.active{background:#eaf0f8;color:#1670dc}.chat-main{display:flex;flex-direction:column;min-width:0;flex:1}.chat-header{display:flex;align-items:center;justify-content:space-between;gap:12px;height:56px;flex-shrink:0;padding:0 24px;border-bottom:1px solid #f0f1f4}.chat-header>div{display:flex;align-items:center;gap:20px;min-width:0}.chat-scroll{overflow-y:auto;flex:1;padding:28px clamp(20px,4vw,60px);scroll-behavior:smooth}.chat-welcome{margin:60px auto 40px;text-align:center;max-width:580px}.assistant-mark{display:inline-flex;width:52px;height:52px;border-radius:15px;align-items:center;justify-content:center;color:white;background:#242629;font-weight:700}.chat-welcome h2{font-size:27px;margin:24px 0 14px}.chat-welcome>p{color:#9296a0}.chat-examples{display:grid;gap:12px;margin-top:34px}.chat-examples button{background:#fff;border:1px solid #e7e9ed;border-radius:12px;padding:18px;text-align:left;color:#606671;cursor:pointer;display:flex;justify-content:space-between;gap:20px}.chat-user{display:flex;justify-content:flex-end;margin:15px 0 28px 45px}.chat-user>div{background:#f0f2f6;border-radius:18px 18px 5px 18px;padding:14px 20px;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8;max-width:90%}.chat-assistant{display:flex;gap:15px;margin-bottom:35px;align-items:flex-start}.assistant-avatar{background:#25272a;color:white;border-radius:10px;width:32px;height:32px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}.chat-response{flex:1;min-width:0;line-height:1.85}.chat-response :deep(.chat-text){white-space:pre-wrap;margin-top:0}.chat-response :deep(.knowledge-claim:first-child){margin-top:0}.chat-thinking{margin:0;color:#89909b;line-height:32px}.chat-failure{background:#fff5f3;border-radius:12px;padding:15px 20px}.chat-failure p{margin:0}.chat-composer{padding:14px clamp(20px,4vw,60px) 16px;background:white}.composer-box{border:1px solid #dfe3e9;border-radius:20px;padding:12px 14px;box-shadow:0 4px 20px #16223406}.composer-box :deep(.el-textarea__inner){box-shadow:none;padding:4px 6px;resize:none;background:transparent;line-height:1.8}.composer-toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:8px}.composer-toolbar>div{display:flex;align-items:center;gap:16px}.composer-source{font-size:11px;color:#9399a3}.composer-note{text-align:center;color:#a1a5ad;font-size:11px;margin:10px 0 0}@media(max-width:1100px){.chat-history{width:160px}.chat-scroll,.chat-composer{padding-left:20px;padding-right:20px}.composer-source{font-size:11px}}@media(max-width:760px){.assistant-shell{height:75vh}.chat-history{width:130px;padding:0 6px 14px}.chat-header{padding:0 14px}.chat-welcome{margin-top:25px}.chat-user{margin-left:0}.chat-assistant{gap:9px}}
</style>
