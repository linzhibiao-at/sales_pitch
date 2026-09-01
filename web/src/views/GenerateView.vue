<template>
  <div class="generate-page">
    <div class="page-header">
      <h1 class="page-title">话术生成</h1>
      <div class="session-bar">
        <span class="session-label">会话 ID：</span>
        <span class="session-id">{{ currentSessionId || '新会话' }}</span>
        <button class="btn btn-secondary btn-sm" @click="newSession">新建会话</button>
      </div>
    </div>

    <div class="page-body">
      <!-- 左列：表单 -->
      <div class="form-col">
        <!-- 顾客信息 -->
        <div class="card section-card">
          <div class="section-header">
            <span class="section-icon">👤</span>
            <h2>顾客信息</h2>
            <span class="hint">（选填）</span>
          </div>
          <div class="form-grid-2">
            <div class="form-group">
              <label>称呼</label>
              <input v-model="form.customer.nickname" class="form-input" placeholder="如：王女士" />
            </div>
            <div class="form-group">
              <label>性别</label>
              <select v-model="form.customer.gender" class="form-select">
                <option value="">不限</option>
                <option value="女">女</option>
                <option value="男">男</option>
              </select>
            </div>
            <div class="form-group">
              <label>年龄 / 年龄段</label>
              <input v-model="form.customer.age" class="form-input" placeholder="如：35 / 大学生" />
            </div>
            <div class="form-group">
              <label>尺码 / 身材</label>
              <input v-model="form.customer.size_info" class="form-input" placeholder="如：M码 / 173cm 60kg" />
            </div>
            <div class="form-group">
              <label>风格偏好</label>
              <input v-model="form.customer.style_preference" class="form-input" placeholder="如：简约通勤、复古运动" />
            </div>
            <div class="form-group">
              <label>使用场景</label>
              <input v-model="form.customer.scene" class="form-input" placeholder="如：秋季通勤、周末出游" />
            </div>
            <div class="form-group">
              <label>预算范围</label>
              <input v-model="form.customer.budget" class="form-input" placeholder="如：500-800元" />
            </div>
            <div class="form-group">
              <label>导购备注</label>
              <input v-model="form.customer.notes" class="form-input" placeholder="关注点、历史消费备注" />
            </div>
          </div>
        </div>

        <!-- 商品信息 -->
        <div class="card section-card">
          <div class="section-header">
            <span class="section-icon">👗</span>
            <h2>商品信息</h2>
            <button class="btn btn-secondary btn-sm" @click="addProduct" :disabled="form.products.length >= 10">
              + 添加商品
            </button>
          </div>
          <div
            v-for="(p, idx) in form.products"
            :key="idx"
            class="product-block"
          >
            <div class="product-block-header">
              <span class="product-index">商品 {{ idx + 1 }}</span>
              <button
                v-if="form.products.length > 1"
                class="btn btn-danger btn-sm"
                @click="removeProduct(idx)"
              >移除</button>
            </div>
            <div class="form-grid-2">
              <div class="form-group">
                <label>商品名称 <span class="required">*</span></label>
                <input v-model="p.title" class="form-input" placeholder="如：FILA 经典卫衣" />
              </div>
              <div class="form-group">
                <label>SKU ID</label>
                <input v-model="p.sku_id" class="form-input" placeholder="如：U2D240211" />
              </div>
              <div class="form-group">
                <label>价格（元）</label>
                <input v-model.number="p.price" type="number" class="form-input" placeholder="如：399" />
              </div>
              <div class="form-group">
                <label>类目</label>
                <input v-model="p.category" class="form-input" placeholder="如：卫衣、运动鞋" />
              </div>
              <div class="form-group">
                <label>颜色</label>
                <input v-model="p.color" class="form-input" placeholder="如：黑色、米白" />
              </div>
              <div class="form-group">
                <label>材质</label>
                <input v-model="p.material" class="form-input" placeholder="如：纯棉、聚酯纤维" />
              </div>
            </div>
            <div class="form-group" style="margin-top:10px">
              <label>卖点描述</label>
              <textarea v-model="p.selling_points" class="form-textarea" placeholder="面料、工艺、功能等，用分号分隔" rows="2" />
            </div>
          </div>
        </div>

        <!-- 话术要求 -->
        <div class="card section-card">
          <div class="section-header">
            <span class="section-icon">🎯</span>
            <h2>话术要求</h2>
          </div>
          <div class="form-grid-3">
            <div class="form-group">
              <label>话术风格</label>
              <select v-model="form.pitch_style" class="form-select">
                <option value="">不限</option>
                <option value="warm">热情亲切（warm）</option>
                <option value="professional">专业顾问（professional）</option>
                <option value="concise">简短干练（concise）</option>
              </select>
            </div>
            <div class="form-group">
              <label>触达渠道</label>
              <select v-model="form.channel" class="form-select">
                <option value="">不限</option>
                <option value="wechat">微信</option>
                <option value="offline">线下</option>
                <option value="phone">电话</option>
              </select>
            </div>
            <div class="form-group">
              <label>字数上限</label>
              <input v-model.number="form.max_length" type="number" class="form-input" placeholder="0 = 不限" min="0" />
            </div>
          </div>
        </div>

        <!-- 操作按钮 -->
        <div class="action-bar">
          <button class="btn btn-primary" :disabled="loading || !canSubmit" @click="doGenerate">
            <span v-if="loading" class="spinner" />
            <span>{{ loading ? '生成中…' : '生成话术' }}</span>
          </button>
          <span v-if="!canSubmit && !loading" class="hint-tip">请至少填写一个商品名称</span>
        </div>
      </div>

      <!-- 右列：对话历史 -->
      <div class="result-col">
        <div class="card result-card">
          <div class="result-header">
            <h2>对话记录</h2>
            <button v-if="history.length" class="btn btn-secondary btn-sm" @click="clearHistory">清除</button>
          </div>

          <div v-if="!history.length" class="empty-state">
            填写左侧表单后点击「生成话术」
          </div>

          <div v-else class="history-list" ref="historyListRef">
            <div v-for="(item, idx) in history" :key="idx" class="history-item">
              <!-- 请求摘要 -->
              <div class="msg-user">
                <span class="msg-label">请求</span>
                <div class="msg-body user-body">
                  <div class="msg-meta">
                    <span v-if="item.req.customer?.nickname" class="badge badge-blue">{{ item.req.customer.nickname }}</span>
                    <span v-for="p in item.req.products" :key="p.sku_id || p.title" class="badge badge-orange">{{ p.title }}</span>
                    <span v-if="item.req.pitch_style" class="badge badge-green">{{ item.req.pitch_style }}</span>
                  </div>
                </div>
              </div>
              <!-- 响应 -->
              <div class="msg-ai">
                <span class="msg-label ai-label">AI</span>
                <div class="msg-body ai-body">
                  <div v-if="item.loading" class="loading-dots">
                    <span /><span /><span />
                  </div>
                  <div v-else-if="item.error" class="error-tip">{{ item.error }}</div>
                  <div v-else class="pitch-text">{{ item.pitch }}</div>
                  <div v-if="item.pitch && !item.loading" class="msg-footer">
                    <span class="trace-id">trace: {{ item.traceId }}</span>
                    <button class="btn btn-secondary btn-sm" @click="copyPitch(item.pitch)">
                      {{ copied === item.traceId ? '已复制 ✓' : '复制' }}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import { generatePitch } from '@/api/index.js'

// ── 表单状态 ──────────────────────────────────────────────
const defaultProduct = () => ({
  title: '', sku_id: '', price: null, category: '',
  color: '', material: '', selling_points: '',
})

const form = ref({
  customer: {
    nickname: '', gender: '', age: '', size_info: '',
    style_preference: '', scene: '', budget: '', notes: '',
  },
  products: [defaultProduct()],
  pitch_style: 'warm',
  channel: 'wechat',
  max_length: 120,
})

const canSubmit = computed(() =>
  form.value.products.some(p => p.title.trim())
)

function addProduct() {
  if (form.value.products.length < 10) form.value.products.push(defaultProduct())
}
function removeProduct(idx) {
  form.value.products.splice(idx, 1)
}

// ── 会话管理 ──────────────────────────────────────────────
const currentSessionId = ref(localStorage.getItem('sp_session_id') || '')

function newSession() {
  currentSessionId.value = ''
  localStorage.removeItem('sp_session_id')
  history.value = []
}

// ── 对话历史 ──────────────────────────────────────────────
const history = ref([])
const historyListRef = ref(null)
const loading = ref(false)
const copied = ref('')

async function doGenerate() {
  if (!canSubmit.value || loading.value) return

  // 清理空字段，构造请求体
  const appId = localStorage.getItem('sp_app_id') || 'micro_guide'
  const customer = Object.fromEntries(
    Object.entries(form.value.customer).filter(([, v]) => v !== '' && v !== null)
  )
  const products = form.value.products
    .filter(p => p.title.trim())
    .map(p => {
      const obj = { title: p.title.trim() }
      if (p.sku_id) obj.sku_id = p.sku_id
      if (p.price !== null && p.price !== '') obj.price = p.price
      if (p.category) obj.category = p.category
      if (p.color) obj.color = p.color
      if (p.material) obj.material = p.material
      if (p.selling_points) obj.selling_points = p.selling_points
      return obj
    })

  const payload = {
    app_id: appId,
    products,
    ...(Object.keys(customer).length > 0 && { customer }),
    ...(form.value.pitch_style && { pitch_style: form.value.pitch_style }),
    ...(form.value.channel && { channel: form.value.channel }),
    ...(form.value.max_length > 0 && { max_length: form.value.max_length }),
    ...(currentSessionId.value && { session_id: currentSessionId.value }),
  }

  // 添加 loading 占位条目
  const entry = { req: { ...form.value, products }, loading: true, pitch: '', error: '', traceId: '' }
  history.value.push(entry)
  loading.value = true

  await nextTick()
  if (historyListRef.value) {
    historyListRef.value.scrollTop = historyListRef.value.scrollHeight
  }

  try {
    const res = await generatePitch(payload)
    entry.loading = false
    entry.pitch = res.pitch
    entry.traceId = res.trace_id
    // 保存 session_id 供多轮复用
    if (res.session_id) {
      currentSessionId.value = res.session_id
      localStorage.setItem('sp_session_id', res.session_id)
    }
  } catch (e) {
    entry.loading = false
    entry.error = e.message || '请求失败'
  } finally {
    loading.value = false
    await nextTick()
    if (historyListRef.value) {
      historyListRef.value.scrollTop = historyListRef.value.scrollHeight
    }
  }
}

async function copyPitch(text) {
  try {
    await navigator.clipboard.writeText(text)
    // 用 traceId 作 key 避免同时复制多条时混乱
    const item = history.value.find(h => h.pitch === text)
    copied.value = item?.traceId || ''
    setTimeout(() => { copied.value = '' }, 2000)
  } catch { /* ignore */ }
}

function clearHistory() {
  history.value = []
}
</script>

<style scoped>
.generate-page { display: flex; flex-direction: column; gap: 20px; }

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.page-title { font-size: 22px; font-weight: 700; }
.session-bar { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.session-label { color: #888; }
.session-id {
  font-family: monospace;
  font-size: 12px;
  background: #f0f2f8;
  padding: 3px 8px;
  border-radius: 4px;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.page-body { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: start; }
@media (max-width: 1024px) { .page-body { grid-template-columns: 1fr; } }

.form-col { display: flex; flex-direction: column; gap: 16px; }

.section-card { display: flex; flex-direction: column; gap: 14px; }
.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-header h2 { font-size: 15px; font-weight: 600; flex: 1; }
.section-icon { font-size: 18px; }
.hint { font-size: 12px; color: #aaa; }
.required { color: #e53935; }

/* 商品块 */
.product-block {
  border: 1px dashed #dde1e9;
  border-radius: 10px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.product-block-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.product-index { font-size: 13px; font-weight: 600; color: #1a73e8; }

.action-bar { display: flex; align-items: center; gap: 14px; }
.hint-tip { font-size: 13px; color: #aaa; }

/* 结果列 */
.result-col { position: sticky; top: 20px; }
.result-card { display: flex; flex-direction: column; gap: 14px; min-height: 500px; max-height: 80vh; }
.result-header { display: flex; align-items: center; justify-content: space-between; }
.result-header h2 { font-size: 15px; font-weight: 600; }

.history-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding-right: 4px;
}

/* 消息气泡 */
.history-item { display: flex; flex-direction: column; gap: 10px; }
.msg-user, .msg-ai { display: flex; gap: 8px; align-items: flex-start; }
.msg-label {
  font-size: 11px;
  font-weight: 700;
  color: #888;
  min-width: 28px;
  padding-top: 6px;
}
.ai-label { color: #1a73e8; }
.msg-body { flex: 1; border-radius: 10px; padding: 10px 14px; }
.user-body { background: #f0f2f8; }
.ai-body { background: #e8f0fe; }
.msg-meta { display: flex; flex-wrap: wrap; gap: 6px; }

.pitch-text {
  font-size: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.msg-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}
.trace-id { font-size: 11px; color: #aaa; font-family: monospace; }

/* 加载动画点 */
.loading-dots { display: flex; gap: 5px; padding: 4px 0; }
.loading-dots span {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #1a73e8;
  animation: bounce 1s infinite ease-in-out;
}
.loading-dots span:nth-child(2) { animation-delay: 0.2s; }
.loading-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.5; }
  40% { transform: scale(1); opacity: 1; }
}
</style>
