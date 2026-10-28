<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, session } from './api'

const emit = defineEmits(['indexed'])
const props = defineProps({ reviewMode: Boolean })
const rows = ref([]), title = ref(''), domain = ref('inventory_policies'), file = ref(null), fileInput = ref(null)
const loading = ref(false), uploading = ref(false), reviewing = ref(''), error = ref(''), notice = ref(''), preview = ref(null), note = ref('')
const admin = computed(() => props.reviewMode && session.user?.role === 'admin')
const labels = { pending: '待审核', approved: '审核通过 · 待建索引', indexed: '已入库', rejected: '审核未通过', index_failed: '审核通过 · 索引失败' }
const domains = { inventory_policies: '库存与仓储资料', general_diagnostics: '通用故障排查资料' }
function selectFile(event) {
  file.value = event.target.files?.[0] || null
  if (file.value && !title.value.trim()) title.value = file.value.name.replace(/\.[^.]+$/, '').slice(0, 100)
}
async function load() {
  loading.value = true; error.value = ''
  try {
    const items = (await api('/knowledge/submissions')).items
    rows.value = props.reviewMode ? items : items.filter(row => row.user_id === session.user.id)
  }
  catch (e) { error.value = e.message }
  finally { loading.value = false }
}
async function upload() {
  if (!file.value || !title.value.trim() || !domain.value || uploading.value) return
  const format = file.value.name.split('.').pop().toLowerCase()
  if (!['txt','md','pdf','docx'].includes(format) || file.value.size > 10*1024*1024) { error.value = '请选择10MB以内的TXT、Markdown、PDF或DOCX文档'; return }
  uploading.value = true; error.value = ''; notice.value = ''
  try {
    const params = new URLSearchParams({ title: title.value.trim(), knowledge_base: domain.value, format })
    await api(`/knowledge/submissions?${params}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file.value, timeout: 60000 })
    title.value = ''; file.value = null; fileInput.value.value = ''
    await load(); notice.value = '已提交审核。管理员审核通过且索引完成后，助手才能检索这份资料。'
  } catch (e) { error.value = e.message }
  finally { uploading.value = false }
}
function inspect(row) { preview.value = row; note.value = row.review_note || '' }
async function review(approved) {
  if (reviewing.value) return
  reviewing.value = preview.value.id; error.value = ''; notice.value = ''
  try {
    const result = await api(`/knowledge/submissions/${preview.value.id}/review`, { method: 'POST', timeout: 300000, body: JSON.stringify({ approved, note: note.value }) })
    preview.value = result; await load()
    notice.value = approved ? '资料已审核入库，可供助手检索引用。' : '资料未通过审核，不会用于助手回答。'
    if (approved) emit('indexed')
  } catch (e) {
    const message = e.name === 'TimeoutError' ? '索引等待超时，后台可能仍在处理。请刷新状态核对，不要重复提交。' : e.message
    await load(); preview.value = rows.value.find(row => row.id === reviewing.value) || preview.value; error.value = message
  } finally { reviewing.value = '' }
}
onMounted(load)
</script>

<template>
  <section class="submission-panel">
    <p class="muted">上传资料须在审核中心经管理员审核并完成索引后才能用于回答。审核前仅用于预览，不进入知识检索。{{ props.reviewMode && !admin ? '当前角色没有资料审核权限，仅可查看本人上传记录。' : '' }}</p>
    <form v-if="!props.reviewMode" class="submission-form" @submit.prevent="upload">
      <label>资料标题<el-input v-model="title" maxlength="100" clearable :disabled="uploading" placeholder="填写资料标题" /></label>
      <label>资料分类<el-select v-model="domain" clearable :disabled="uploading" placeholder="选择资料分类"><el-option v-for="(label, value) in domains" :key="value" :value="value" :label="label" /></el-select></label>
      <label class="file-row">选择文档<input ref="fileInput" type="file" accept=".txt,.md,.pdf,.docx" :disabled="uploading" @change="selectFile" /></label>
      <p class="muted">支持UTF-8文本、Markdown、文字型PDF和DOCX，每份不超过10MB。扫描件暂不支持。</p>
      <el-button native-type="submit" type="primary" :loading="uploading" :disabled="!file || !title.trim() || !domain">提交审核</el-button>
    </form>
    <div v-if="error" class="error-banner" role="alert">{{ error }}</div><p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <div class="submission-heading"><h3>{{ admin ? '资料审核记录' : '我的上传记录' }}</h3><el-button link :loading="loading" @click="load">刷新状态</el-button></div>
    <p v-if="reviewing" role="status">正在审核并更新索引，请稍候。库存业务仍可使用。</p>
    <el-table :data="rows" v-loading="loading" empty-text="暂无上传记录" max-height="360">
      <el-table-column prop="title" label="资料" min-width="180" /><el-table-column label="分类" min-width="150"><template #default="{ row }">{{ domains[row.kb] }}</template></el-table-column>
      <el-table-column label="状态" min-width="165"><template #default="{ row }"><el-tag :type="row.status === 'indexed' ? 'success' : row.status === 'rejected' ? 'danger' : 'warning'">{{ labels[row.status] || row.status }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" :disabled="!!reviewing" @click="inspect(row)">{{ admin && ['pending','approved','index_failed'].includes(row.status) ? '预览与审核' : '查看' }}</el-button></template></el-table-column>
    </el-table>
    <el-dialog :model-value="!!preview" title="资料预览与审核" width="min(780px, 92vw)" append-to-body :close-on-click-modal="false" :before-close="done => { if (!reviewing) { preview = null; done() } }" @update:model-value="value => { if (!value && !reviewing) preview = null }">
      <template v-if="preview"><h3>{{ preview.title }}</h3><p class="muted">{{ preview.format.toUpperCase() }} · {{ labels[preview.status] }} · 提交时间 {{ new Date(preview.created_at).toLocaleString('zh-CN', { hour12: false }) }}</p>
        <p class="muted">{{ preview.source_notice }}</p>
        <div class="submission-preview"><div v-for="(chunk, index) in preview.preview" :key="index"><small>{{ chunk.location.page_start ? `第${chunk.location.page_start}页` : chunk.location.block_start ? `正文块${chunk.location.block_start}` : `第${chunk.location.line_start}行` }}</small><pre>{{ chunk.text }}</pre></div></div>
        <el-input v-if="admin && ['pending','approved','index_failed'].includes(preview.status)" v-model="note" type="textarea" maxlength="500" placeholder="填写审核意见（选填）" :disabled="!!reviewing" />
        <p v-else-if="preview.review_note">审核意见：{{ preview.review_note }}</p>
        <div v-if="error" class="error-banner" role="alert">{{ error }}</div><p v-if="reviewing" role="status">正在更新索引，完成前请勿重复操作…</p>
      </template>
      <template #footer><el-button :disabled="!!reviewing" @click="preview = null">关闭</el-button><template v-if="admin && preview && ['pending','approved','index_failed'].includes(preview.status)"><el-button :disabled="!!reviewing" type="danger" plain @click="review(false)">不通过</el-button><el-button type="primary" :loading="!!reviewing" @click="review(true)">{{ preview.status === 'pending' ? '审核通过并入库' : '重试建立索引' }}</el-button></template></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.submission-form { border: 1px solid #e6e9ef; border-radius: 12px; padding: 18px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 20px 0; }
.submission-form label { display: grid; gap: 8px; color: #515863; }.file-row,.submission-form>p { grid-column: 1 / -1; }.submission-form>p { margin: 0; }.submission-form>.el-button { justify-self: start; }
.submission-heading { display: flex; align-items: center; justify-content: space-between; }.notice { color: #22754b; }.submission-preview { max-height: 40vh; overflow: auto; border: 1px solid #e6e9ef; border-radius: 10px; padding: 16px; margin: 16px 0; }.submission-preview pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.8; }.submission-preview small { color: #9096a1; }
@media(max-width:600px){.submission-form { grid-template-columns: 1fr; }}
</style>
