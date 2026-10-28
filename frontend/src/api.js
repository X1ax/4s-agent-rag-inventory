import { reactive } from 'vue'

export const session = reactive({ token: '', user: null })

export async function api(path, options = {}) {
  const { timeout = 15000, ...requestOptions } = options
  const response = await fetch(`/api${path}`, {
    ...requestOptions,
    headers: { 'Content-Type': 'application/json', ...(session.token ? { Authorization: `Bearer ${session.token}` } : {}), ...options.headers },
    signal: AbortSignal.timeout(timeout),
  })
  let body
  try { body = await response.json() }
  catch {
    const error = new Error('服务未返回有效数据，请检查后端连接后重试')
    error.status = response.status
    throw error
  }
  if (!response.ok) {
    if (response.status === 401) { session.token = ''; session.user = null }
    const detail = Array.isArray(body.detail) ? body.detail.map(item => `${item.loc.slice(1).join('.')}：${item.msg}`).join('；') : body.detail
    const error = new Error(detail || '请求失败，请稍后重试')
    error.status = response.status
    error.businessIssue = body.business_issue
    throw error
  }
  return body
}

export async function allCatalog(path) {
  // ponytail: small local catalogs; switch selectors to remote search when data volume warrants it.
  const items = []
  for (let page = 1; ; page++) {
    const result = await api(`${path}?page=${page}&page_size=100`)
    items.push(...result.items)
    if (items.length >= result.total) return items
  }
}

export function localTime(value) {
  return new Date(`${value}Z`).toLocaleString('zh-CN', { hour12: false })
}

export const roleLabels = { admin: '管理员', storekeeper: '库管', manager: '经理' }
