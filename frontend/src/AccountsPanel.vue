<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, roleLabels, session } from './api'

const rows = ref([]), loading = ref(false), visible = ref(false), saving = ref(false), editing = ref(null), loadError = ref('')
const form = reactive({})
let version = 0
async function load() {
  const current = ++version
  loading.value = true
  try {
    const result = await api('/users')
    if (current === version) { rows.value = result.items; loadError.value = '' }
  }
  catch (error) { if (current === version) { rows.value = []; loadError.value = error.message } }
  finally { if (current === version) loading.value = false }
}
function open(row) {
  editing.value = row?.id || null
  Object.keys(form).forEach(key => delete form[key])
  Object.assign(form, row ? { role: row.role, active: row.active } : { username: '', display_name: '', password: '', role: 'storekeeper' })
  visible.value = true
}
async function save() {
  saving.value = true
  try {
    await api(`/users${editing.value ? `/${editing.value}` : ''}`, { method: editing.value ? 'PATCH' : 'POST', body: JSON.stringify(form) })
    visible.value = false; form.password = ''; ElMessage.success('账号已保存'); await load()
  } catch (error) { ElMessage.error(error.message) }
  finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="toolbar"><p class="muted">角色和状态由服务端检查，变更对后续请求生效。</p><el-button type="primary" @click="open()">新增账号</el-button></div>
    <div v-if="loadError" class="error-banner" role="alert">账号查询失败：{{ loadError }}。旧结果已清除。<el-button link :loading="loading" @click="load">重试查询</el-button></div>
    <el-table :data="rows" v-loading="loading" row-key="id" :empty-text="loadError ? '账号加载失败，请重试' : '暂无账号'">
      <el-table-column prop="username" label="用户名" min-width="170" />
      <el-table-column prop="display_name" label="显示名称" min-width="160" />
      <el-table-column label="角色" min-width="110"><template #default="{ row }">{{ roleLabels[row.role] }}</template></el-table-column>
      <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.active ? 'success' : 'info'">{{ row.active ? '启用' : '停用' }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" :disabled="row.id === session.user.id" @click="open(row)">编辑权限</el-button></template></el-table-column>
    </el-table>
    <p class="table-note">管理员维护资料和账号；库管可查询资料并登记期初库存；经理只读查询。不能停用或降级当前管理员账号。</p>
  </section>
  <el-dialog v-model="visible" :title="editing ? '编辑账号权限' : '新增账号'" width="450px" :close-on-click-modal="false" @closed="form.password = ''">
    <el-form label-position="top" @submit.prevent="save">
      <template v-if="!editing">
        <el-form-item label="用户名（3–40位小写字母、数字或下划线）"><el-input v-model="form.username" required maxlength="40" autocomplete="off" /></el-form-item>
        <el-form-item label="显示名称"><el-input v-model="form.display_name" required maxlength="80" /></el-form-item>
        <el-form-item label="密码（10–128字符）"><el-input v-model="form.password" type="password" show-password required minlength="10" maxlength="128" autocomplete="new-password" /></el-form-item>
      </template>
      <el-form-item label="角色"><el-select :value-on-clear="''" clearable v-model="form.role"><el-option v-for="(label, role) in roleLabels" :key="role" :label="label" :value="role" /></el-select></el-form-item>
      <el-form-item v-if="editing" label="状态"><el-switch v-model="form.active" active-text="启用" inactive-text="停用" /></el-form-item>
    <div class="dialog-actions"><el-button :disabled="saving" @click="visible = false">取消</el-button><el-button type="primary" native-type="submit" :loading="saving">保存账号</el-button></div></el-form>
  </el-dialog>
</template>
