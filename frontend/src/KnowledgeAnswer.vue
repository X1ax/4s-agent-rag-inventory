<script setup>
import { computed, ref } from 'vue'
import { ElCollapse, ElCollapseItem, ElDescriptions, ElDescriptionsItem } from 'element-plus'
import { api } from './api'
import { images } from './media'
const props = defineProps({ answer: { type: Object, required: true } })
const emit = defineEmits(['choose-action'])
const answer = computed(() => props.answer)
const result = computed(() => answer.value?.calculation)
const referenceError = ref(''), reference = ref(null), referenceOpen = ref(false), referenceLoading = ref(false)
const partStatuses = { undetermined: '证据不足，无法判断', not_indicated: '当前不支持更换', conditional: '检查确认后再判断更换', recommended: '按所提供检查记录支持更换' }
function citationNumber(id) { return (answer.value?.citations.findIndex(c => c.citation_id === id) ?? -1) + 1 }
async function openReference(citation) {
  referenceOpen.value = true; reference.value = null; referenceError.value = ''; referenceLoading.value = true
  try {
    if (['web_search', 'tool_result'].includes(citation.source_type)) { reference.value = { fragment: citation }; return }
    const query = new URLSearchParams({ generation_id: answer.value.generation_id, knowledge_base: answer.value.knowledge_base, knowledge_scope: answer.value.knowledge_scope })
    reference.value = await api(`/knowledge/chunks/${encodeURIComponent(citation.chunk_id)}?${query}`)
  } catch (e) { referenceError.value = e.message }
  finally { referenceLoading.value = false }
}
function findCitation(id) { return answer.value.citations.find(c => c.citation_id === id) }
function location(c) {
  const section = c.section_path?.join(' / ') || ''
  if (c.page_start) return `${section} · 第${c.page_start}—${c.page_end}页`
  if (c.block_start) return `${section} · 正文块${c.block_start}—${c.block_end}`
  return c.line_start ? `${section} · 第${c.line_start}—${c.line_end}行` : section
}
</script>
<template>
    <div class="assistant-answer">
      <p v-if="answer.message" class="chat-text">{{ answer.message }}</p>
      <section v-for="(issue, index) in answer.business_issues || []" :key="`issue-${index}`" class="business-issue"><strong>{{ issue.summary }}</strong><p>{{ issue.notice }}</p><el-button v-for="option in issue.options" :key="option.label" @click="emit('choose-action', option.prompt)">{{ option.label }}</el-button></section>
      <p v-if="!answer.message && !answer.claims.length && answer.limitations.length" class="chat-text">{{ answer.limitations[0] }}</p>
      <el-tag v-if="answer.knowledge_scope === 'draft_preview'" type="info" effect="plain">本地参考资料</el-tag>
      <div v-for="(claim, index) in answer.claims" :key="`claim-${index}`" class="knowledge-claim"><p>{{ claim.text }}</p><el-button v-for="id in claim.citation_ids" :key="id" link type="primary" @click="openReference(findCitation(id))">依据 [{{ citationNumber(id) }}]</el-button></div>
      <div v-if="result" class="knowledge-calculation">
        <h3>假设计算 · 服务端复算</h3><p>{{ result.notice }}</p>
        <p v-for="step in result.steps" :key="step">{{ step }}</p>
        <el-descriptions v-if="result.kind === 'replenishment'" :column="2" border><el-descriptions-item label="目标库存">{{ result.result.target }} 件</el-descriptions-item><el-descriptions-item label="库存位置">{{ result.result.position }} 件</el-descriptions-item><el-descriptions-item label="假设补货缺口">{{ result.result.gap }} 件</el-descriptions-item></el-descriptions>
        <el-descriptions v-else-if="result.kind === 'transfer_conservation'" :column="1" border><el-descriptions-item label="发出后">来源仓 {{ result.result.after_ship.source }} 件，目标仓 {{ result.result.after_ship.target }} 件，在途 {{ result.result.after_ship.in_transit }} 件</el-descriptions-item><el-descriptions-item label="收货后">来源仓 {{ result.result.after_receive.source }} 件，目标仓 {{ result.result.after_receive.target }} 件，在途 0 件</el-descriptions-item></el-descriptions>
        <template v-else-if="result.kind === 'allocation'"><p v-for="(row, index) in result.result.allocations" :key="index">{{ row.source }} → {{ row.target }}：{{ row.quantity }} 件</p><p v-for="(gap, code) in result.result.purchase_gaps" :key="code">{{ code }} 剩余采购缺口：{{ gap }} 件</p></template>
        <el-collapse><el-collapse-item title="核对计算输入与本轮原文" name="inputs"><pre>{{ JSON.stringify(result.input_evidence, null, 2) }}</pre></el-collapse-item></el-collapse>
      </div>
      <template v-for="(rows, title) in { '可能故障类型': answer.fault_types, '检查方向': answer.checks }" :key="title">
        <div v-if="rows.length"><h3>{{ title }}</h3><div v-for="(row, index) in rows" :key="index" class="knowledge-claim"><p>{{ row.text }}</p><el-button v-for="id in row.citation_ids" :key="id" link type="primary" @click="openReference(findCitation(id))">依据 [{{ citationNumber(id) }}]</el-button></div></div>
      </template>
      <div v-if="answer.part_decisions.length"><h3>部件类型与换件判断</h3><div class="knowledge-parts"><article v-for="(part, index) in answer.part_decisions" :key="index"><img v-if="images[part.part_type_key]" :src="images[part.part_type_key].src" :alt="part.catalog.catalog_name" /><div><strong>{{ part.catalog?.catalog_name || '尚未收录的部件类型' }}</strong><p><el-tag :type="part.status === 'recommended' ? 'warning' : 'info'">{{ partStatuses[part.status] }}</el-tag></p><p>{{ part.text }}</p><p>成立条件：{{ part.condition }}</p><p v-if="part.check_record" class="muted">你提供的记录：{{ part.check_record.quote }}</p><el-button v-for="id in part.citation_ids" :key="id" link type="primary" @click="openReference(findCitation(id))">依据 [{{ citationNumber(id) }}]</el-button><small class="muted">类型配图，不代表商品适配、需求数量或现有库存。</small></div></article></div></div>
      <div v-if="answer.follow_up_questions.length" class="knowledge-followup"><h3>需要你补充</h3><p v-for="question in answer.follow_up_questions" :key="question">{{ question }}</p><p class="muted">补充后，请连同原症状或条件重新提交完整问题。</p></div>
      <details v-if="answer.citations.length" class="knowledge-citations"><summary>参考来源（{{ answer.citations.length }}）</summary><button v-for="(citation, index) in answer.citations" :key="citation.citation_id" @click="openReference(citation)"><strong>[{{ index + 1 }}] {{ citation.title }}</strong><span>{{ citation.source_type === 'web_search' ? '联网网页 · ' + citation.document_version : citation.document_id + ' · ' + citation.document_version + ' · ' + citation.native_format.toUpperCase() }}</span><small>{{ location(citation) }}</small></button></details>
      <details v-if="answer.search_sources?.length && !answer.citations.length" class="knowledge-citations"><summary>本次检索到的网页（证据尚不足）</summary><button v-for="source in answer.search_sources" :key="source.citation_id" @click="openReference(source)"><strong>{{ source.title }}</strong><small>{{ source.url }}</small></button></details>
      <details v-if="answer.limitations.length" class="knowledge-limits"><summary>说明与边界</summary><p v-for="item in [...new Set(answer.limitations)]" :key="item">{{ item }}</p></details>
    </div>
    <el-dialog v-model="referenceOpen" title="核对引用原文" width="min(850px, 94vw)" destroy-on-close><p v-if="referenceLoading">正在核对来源资格并读取原文…</p><div v-if="referenceError" class="error-banner" role="alert">{{ referenceError }}</div><template v-if="reference"><h3>{{ reference.fragment.title }}</h3><p class="muted">{{ reference.fragment.document_id }} · {{ reference.fragment.document_version }} · {{ location(reference.fragment) }}</p><p v-if="reference.fragment.accessed_at" class="muted">获取时间：{{ new Date(reference.fragment.accessed_at).toLocaleString('zh-CN', { hour12: false }) }}</p><a v-if="reference.fragment.url" :href="reference.fragment.url" target="_blank" rel="noopener noreferrer">打开网页来源</a><el-tag v-if="reference.fragment.source_state === 'draft'" type="warning">项目编制资料</el-tag><el-tag v-if="reference.fragment.source_state === 'reference'" type="success">管理员审核参考资料</el-tag><pre class="knowledge-excerpt">{{ reference.fragment.excerpt }}</pre><el-collapse v-if="reference.context && reference.context.chunk_id !== reference.fragment.chunk_id"><el-collapse-item title="展开完整章节与适用条件" name="context"><pre class="knowledge-excerpt">{{ reference.context.excerpt }}</pre></el-collapse-item></el-collapse></template></el-dialog>
</template>
<style scoped>
.business-issue { margin: 18px 0; padding: 18px; border: 1px solid #f0d6a4; border-radius: 12px; background: #fffcf5; }
.business-issue p { font-size: 13px; color: #767982; }
.business-issue .el-button { margin: 5px 6px 5px 0; }
.knowledge-claim { margin: 20px 0; line-height: 1.85; }
.knowledge-claim p { white-space: pre-wrap; margin-bottom: 5px; }
.knowledge-parts { display: grid; gap: 16px; }
.knowledge-parts article { display: flex; align-items: flex-start; padding: 20px; border: 1px solid #e5e7eb; border-radius: 14px; gap: 22px; }
.knowledge-parts img { width: 80px; height: 80px; object-fit: contain; }
.knowledge-parts small { display: block; margin-top: 14px; }
.knowledge-citations { margin-top: 26px; }
.knowledge-citations button { display: grid; gap: 6px; width: 100%; background: #fafafa; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px; margin: 10px 0; text-align: left; cursor: pointer; color: inherit; }
.knowledge-citations span, .knowledge-citations small { color: #767982; }
.knowledge-limits { margin-top: 24px; padding-top: 14px; border-top: 1px solid #e5e7eb; color: #767982; font-size: 13px; line-height: 1.8; }
.knowledge-calculation, .knowledge-followup { background: #f5f8fc; padding: 22px; border-radius: 12px; margin: 22px 0; }
.assistant-answer pre, .knowledge-excerpt { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.85; font: inherit; }
@media (max-width: 760px) { .knowledge-parts article { flex-direction: column; } }
</style>
