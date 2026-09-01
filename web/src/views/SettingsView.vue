<template>
  <div class="settings-page">
    <div class="page-header">
      <h1 class="page-title">设置</h1>
    </div>

    <div class="card settings-card">
      <h2 class="section-title">接口配置</h2>
      <p class="section-desc">配置项保存在浏览器 localStorage，不会上传至服务器。</p>

      <div class="form-col">
        <div class="form-group">
          <label>后端地址（Base URL）</label>
          <input
            v-model="localForm.baseUrl"
            class="form-input"
            placeholder="留空则使用 Vite 代理（开发环境），生产填写如 https://api.example.com"
          />
          <span class="field-hint">开发时留空即可（Vite 已代理 /v1 → localhost:8000）</span>
        </div>

        <div class="form-group">
          <label>API Key（X-API-Key）</label>
          <div class="input-row">
            <input
              v-model="localForm.apiKey"
              :type="showKey ? 'text' : 'password'"
              class="form-input"
              placeholder="后端鉴权 Key，auth.enabled=false 时可留空"
              autocomplete="off"
            />
            <button class="btn btn-secondary btn-sm" @click="showKey = !showKey">
              {{ showKey ? '隐藏' : '显示' }}
            </button>
          </div>
        </div>

        <div class="form-group">
          <label>App ID</label>
          <input
            v-model="localForm.appId"
            class="form-input"
            placeholder="如：micro_guide"
          />
          <span class="field-hint">须在后端 allowed_app_ids 白名单内</span>
        </div>

        <div class="action-row">
          <button class="btn btn-primary" @click="saveSettings">保存</button>
          <span v-if="saved" class="saved-tip">已保存 ✓</span>
        </div>
      </div>
    </div>

    <!-- 当前配置预览 -->
    <div class="card preview-card">
      <h2 class="section-title">当前生效配置</h2>
      <div class="preview-list">
        <div class="preview-row">
          <span class="preview-key">Base URL</span>
          <span class="preview-val">{{ current.baseUrl || '（空，使用 Vite 代理）' }}</span>
        </div>
        <div class="preview-row">
          <span class="preview-key">API Key</span>
          <span class="preview-val">{{ current.apiKey ? '●●●●●●' + current.apiKey.slice(-4) : '（未设置）' }}</span>
        </div>
        <div class="preview-row">
          <span class="preview-key">App ID</span>
          <span class="preview-val">{{ current.appId || '（未设置）' }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'

const showKey = ref(false)
const saved = ref(false)

const localForm = reactive({
  baseUrl: localStorage.getItem('sp_base_url') || '',
  apiKey: localStorage.getItem('sp_api_key') || '',
  appId: localStorage.getItem('sp_app_id') || 'micro_guide',
})

const current = reactive({ ...localForm })

function saveSettings() {
  localStorage.setItem('sp_base_url', localForm.baseUrl.trim())
  localStorage.setItem('sp_api_key', localForm.apiKey.trim())
  localStorage.setItem('sp_app_id', localForm.appId.trim())
  Object.assign(current, localForm)
  saved.value = true
  setTimeout(() => { saved.value = false }, 2000)
}
</script>

<style scoped>
.settings-page { display: flex; flex-direction: column; gap: 20px; }
.page-title { font-size: 22px; font-weight: 700; }

.settings-card, .preview-card {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-width: 580px;
}
.section-title { font-size: 15px; font-weight: 600; }
.section-desc { font-size: 13px; color: #888; margin-top: -8px; }

.form-col { display: flex; flex-direction: column; gap: 16px; }
.field-hint { font-size: 12px; color: #aaa; margin-top: 2px; }

.input-row { display: flex; gap: 8px; align-items: center; }
.input-row .form-input { flex: 1; }

.action-row { display: flex; align-items: center; gap: 14px; }
.saved-tip { font-size: 13px; color: #2e7d32; font-weight: 600; }

.preview-list { display: flex; flex-direction: column; gap: 10px; }
.preview-row {
  display: flex;
  gap: 16px;
  font-size: 13px;
  align-items: baseline;
}
.preview-key { min-width: 80px; color: #888; font-weight: 600; }
.preview-val { color: #333; word-break: break-all; font-family: monospace; }
</style>
