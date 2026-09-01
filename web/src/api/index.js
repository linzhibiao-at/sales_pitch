/**
 * API 客户端：封装后端接口调用，从 localStorage 读取 API Key 配置
 */

function getConfig() {
  return {
    apiKey: localStorage.getItem('sp_api_key') || '',
    appId: localStorage.getItem('sp_app_id') || 'micro_guide',
    baseUrl: localStorage.getItem('sp_base_url') || '',
  }
}

function buildHeaders() {
  const { apiKey } = getConfig()
  const headers = { 'Content-Type': 'application/json' }
  if (apiKey) headers['X-API-Key'] = apiKey
  return headers
}

/**
 * 生成营销话术
 * @param {object} payload - SalesPitchRequest 对象
 * @returns {Promise<{session_id, pitch, pitch_style, model, trace_id}>}
 */
export async function generatePitch(payload) {
  const { baseUrl } = getConfig()
  const url = `${baseUrl}/v1/sales-pitch/generate`
  const res = await fetch(url, {
    method: 'POST',
    headers: buildHeaders(),
    body: JSON.stringify(payload),
  })
  const data = await res.json()
  if (!res.ok) {
    const msg = data.detail || data.message || `HTTP ${res.status}`
    throw new Error(msg)
  }
  return data
}

/**
 * 查询审计记录列表
 * @param {object} params - { app_id, session_id, trace_id, ts_from, ts_to, page, size }
 * @returns {Promise<{total, items}>}
 */
export async function queryAudit(params = {}) {
  const { baseUrl } = getConfig()
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) qs.set(k, v)
  })
  const url = `${baseUrl}/v1/audit/requests?${qs}`
  const res = await fetch(url, { headers: buildHeaders() })
  const data = await res.json()
  if (!res.ok) {
    const msg = data.detail || data.message || `HTTP ${res.status}`
    throw new Error(msg)
  }
  return data
}

/**
 * 查询单条审计详情
 * @param {string} traceId
 */
export async function getAuditDetail(traceId) {
  const { baseUrl } = getConfig()
  const url = `${baseUrl}/v1/audit/requests/${traceId}`
  const res = await fetch(url, { headers: buildHeaders() })
  const data = await res.json()
  if (!res.ok) {
    const msg = data.detail || data.message || `HTTP ${res.status}`
    throw new Error(msg)
  }
  return data
}
