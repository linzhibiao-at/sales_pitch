<template>
  <div class="audit-page">
    <div class="page-header">
      <h1 class="page-title">审计记录</h1>
    </div>

    <!-- 筛选栏 -->
    <div class="card filter-card">
      <div class="form-grid-3">
        <div class="form-group">
          <label>App ID</label>
          <input v-model="filter.app_id" class="form-input" placeholder="如：micro_guide" />
        </div>
        <div class="form-group">
          <label>Session ID</label>
          <input v-model="filter.session_id" class="form-input" placeholder="会话 ID" />
        </div>
        <div class="form-group">
          <label>Trace ID</label>
          <input v-model="filter.trace_id" class="form-input" placeholder="精确匹配" />
        </div>
        <div class="form-group">
          <label>起始时间</label>
          <input v-model="filter.ts_from" type="datetime-local" class="form-input" />
        </div>
        <div class="form-group">
          <label>结束时间</label>
          <input v-model="filter.ts_to" type="datetime-local" class="form-input" />
        </div>
        <div class="form-group" style="justify-content:flex-end;flex-direction:row;align-items:flex-end;gap:8px">
          <button class="btn btn-secondary" @click="resetFilter">重置</button>
          <button class="btn btn-primary" :disabled="auditLoading" @click="doQuery">
            <span v-if="auditLoading" class="spinner" />
            <span>查询</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 错误提示 -->
    <div v-if="auditError" class="error-tip">{{ auditError }}</div>
    <div v-if="detailError" class="error-tip">{{ detailError }}</div>

    <!-- 列表 -->
    <div class="card table-card">
      <div class="table-header">
        <span class="total-tip">共 {{ total }} 条</span>
      </div>

      <div v-if="!items.length && !auditLoading" class="empty-state">
        暂无记录，点击「查询」加载
      </div>

      <table v-else class="audit-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>App ID</th>
            <th>话术风格</th>
            <th>话术（截取）</th>
            <th>Trace ID</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.trace_id" @click="openDetail(item)" class="table-row">
            <td class="td-time">{{ formatTime(item.created_at) }}</td>
            <td><span class="badge badge-blue">{{ item.app_id }}</span></td>
            <td>
              <span v-if="item.pitch_style" class="badge badge-green">{{ item.pitch_style }}</span>
              <span v-else class="text-muted">—</span>
            </td>
            <td class="td-pitch">{{ truncate(item.pitch, 60) }}</td>
            <td class="td-trace">{{ item.trace_id }}</td>
            <td><button class="btn btn-secondary btn-sm" @click.stop="openDetail(item)">详情</button></td>
          </tr>
        </tbody>
      </table>

      <!-- 分页 -->
      <div v-if="total > pageSize" class="pagination">
        <button class="btn btn-secondary btn-sm" :disabled="page <= 1" @click="changePage(page - 1)">上一页</button>
        <span class="page-info">{{ page }} / {{ totalPages }}</span>
        <button class="btn btn-secondary btn-sm" :disabled="page >= totalPages" @click="changePage(page + 1)">下一页</button>
      </div>
    </div>

    <!-- 详情弹窗 -->
    <div v-if="detailItem || detailLoading" class="modal-overlay" @click.self="detailItem = null; detailLoading = false">
      <div class="modal-box">
        <div class="modal-header">
          <h3>审计详情</h3>
          <button class="btn btn-secondary btn-sm" @click="detailItem = null; detailLoading = false">✕ 关闭</button>
        </div>
        <div class="modal-body">
          <div v-if="detailLoading" class="empty-state">加载中…</div>
          <template v-else-if="detailItem">
          <div class="detail-row">
            <span class="detail-label">Trace ID</span>
            <span class="detail-val mono">{{ detailItem.trace_id }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Session ID</span>
            <span class="detail-val mono">{{ detailItem.session_id || '—' }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">时间</span>
            <span class="detail-val">{{ formatTime(detailItem.created_at) }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">App ID</span>
            <span class="detail-val">{{ detailItem.app_id }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">耗时</span>
            <span class="detail-val">{{ detailItem.elapsed_ms }} ms</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">状态</span>
            <span class="detail-val">{{ detailItem.status }}{{ detailItem.error ? '：' + detailItem.error : '' }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">话术风格</span>
            <span class="detail-val">{{ detailItem.input?.pitch_style || '—' }}</span>
          </div>
          <div class="detail-section">
            <span class="detail-label">生成话术</span>
            <pre class="detail-pre">{{ detailItem.result?.pitch || '—' }}</pre>
          </div>
          <div class="detail-section">
            <span class="detail-label">顾客信息</span>
            <pre class="detail-pre">{{ JSON.stringify(detailItem.input?.customer ?? null, null, 2) }}</pre>
          </div>
          <div class="detail-section">
            <span class="detail-label">商品信息</span>
            <pre class="detail-pre">{{ JSON.stringify(detailItem.input?.products ?? null, null, 2) }}</pre>
          </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { queryAudit, getAuditDetail } from '@/api/index.js'

const filter = ref({ app_id: '', session_id: '', trace_id: '', ts_from: '', ts_to: '' })
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const auditLoading = ref(false)
const auditError = ref('')
const detailItem = ref(null)
const detailLoading = ref(false)
const detailError = ref('')

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function resetFilter() {
  filter.value = { app_id: '', session_id: '', trace_id: '', ts_from: '', ts_to: '' }
}

async function doQuery(resetPage = true) {
  if (resetPage) page.value = 1
  auditLoading.value = true
  auditError.value = ''
  try {
    // datetime-local 值（本地时间，如 2026-09-01T14:00）直接传，
    // 与库里本地时区 iso 字符串字典序可比；勿转 toISOString()（UTC 时区错位）
    const params = { ...filter.value, page: page.value, size: pageSize }
    const res = await queryAudit(params)
    items.value = res.items || []
    total.value = res.total || 0
  } catch (e) {
    auditError.value = e.message || '查询失败'
  } finally {
    auditLoading.value = false
  }
}

function changePage(p) {
  page.value = p
  doQuery(false)
}

async function openDetail(item) {
  // 列表是精简行（无完整话术/顾客/商品），调详情 API 取完整文档
  detailLoading.value = true
  detailError.value = ''
  detailItem.value = null
  try {
    detailItem.value = await getAuditDetail(item.trace_id)
  } catch (e) {
    detailError.value = e.message || '加载详情失败'
  } finally {
    detailLoading.value = false
  }
}

function formatTime(ts) {
  if (!ts) return '—'
  return new Date(ts).toLocaleString('zh-CN', { hour12: false })
}

function truncate(str, len) {
  if (!str) return '—'
  return str.length > len ? str.slice(0, len) + '…' : str
}
</script>

<style scoped>
.audit-page { display: flex; flex-direction: column; gap: 20px; }
.page-header { display: flex; align-items: center; }
.page-title { font-size: 22px; font-weight: 700; }

.filter-card { display: flex; flex-direction: column; gap: 14px; }

.table-card { display: flex; flex-direction: column; gap: 14px; overflow-x: auto; }
.table-header { display: flex; justify-content: flex-end; }
.total-tip { font-size: 13px; color: #888; }

.audit-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.audit-table th {
  text-align: left;
  padding: 10px 12px;
  border-bottom: 2px solid #eee;
  color: #888;
  font-weight: 600;
  white-space: nowrap;
}
.audit-table td { padding: 10px 12px; border-bottom: 1px solid #f0f0f5; vertical-align: middle; }
.table-row { cursor: pointer; transition: background 0.15s; }
.table-row:hover { background: #f7f8fc; }

.td-time { white-space: nowrap; color: #666; }
.td-pitch { max-width: 280px; color: #444; }
.td-trace { font-family: monospace; font-size: 11px; color: #aaa; max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.text-muted { color: #ccc; }

.pagination { display: flex; align-items: center; justify-content: center; gap: 14px; padding-top: 8px; }
.page-info { font-size: 13px; color: #666; }

/* 详情弹窗 */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}
.modal-box {
  background: #fff;
  border-radius: 14px;
  width: min(640px, 92vw);
  max-height: 85vh;
  display: flex; flex-direction: column;
  box-shadow: 0 8px 40px rgba(0,0,0,.18);
}
.modal-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 22px;
  border-bottom: 1px solid #f0f0f5;
}
.modal-header h3 { font-size: 16px; font-weight: 700; }
.modal-body { padding: 18px 22px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }

.detail-row { display: flex; gap: 12px; align-items: baseline; }
.detail-label { font-size: 12px; font-weight: 600; color: #888; min-width: 80px; }
.detail-val { font-size: 13px; color: #333; }
.mono { font-family: monospace; font-size: 12px; word-break: break-all; }
.detail-section { display: flex; flex-direction: column; gap: 6px; }
.detail-pre {
  background: #f5f6fa;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 12px;
  font-family: monospace;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}
</style>
