/* global window, document, fetch */

const mainEl = document.getElementById('main')
const statusEl = document.getElementById('status')
const titleEl = document.getElementById('page-title')
const linkJsonApi = document.getElementById('link-json-api')

const esc = (s) => {
  const d = document.createElement('div')
  d.textContent = s == null ? '' : String(s)
  return d.innerHTML
}

const thumbOf = (row) => {
  if (!row || typeof row !== 'object') {
    return ''
  }
  const u = row.display_image || row.index_image || row.tryon_image
  return typeof u === 'string' ? u : ''
}

const render = (spuId, rows) => {
  titleEl.textContent = `SPU ${spuId} · ${rows.length} 个 SKU`
  statusEl.remove()
  const parts = []
  parts.push(
    '<div class="detail-panel"><h2>颜色款列表</h2><p class="gallery-caption">点击卡片查看与 outfits-viewer 同风格的商品详情页。</p>' +
      '<div class="spu-sku-grid">',
  )
  for (const row of rows) {
    const sid = String(row.sku_id || '')
    if (!sid) {
      continue
    }
    const href = `sku-detail.html?sku_id=${encodeURIComponent(sid)}`
    const img = thumbOf(row)
    const t = esc(row.title || sid)
    const id = esc(sid)
    parts.push(`<a class="spu-sku-card" href="${href}">`)
    if (img) {
      const u = esc(img)
      parts.push(`<img src="${u}" alt="" loading="lazy" decoding="async" />`)
    } else {
      parts.push('<div style="height:180px;background:#1a1e26"></div>')
    }
    parts.push('<div class="spu-sku-card-body">')
    parts.push(`<p class="spu-sku-card-title">${t}</p>`)
    parts.push(`<div class="spu-sku-card-id">${id}</div>`)
    parts.push('</div></a>')
  }
  parts.push('</div></div>')
  mainEl.innerHTML = parts.join('')
}

;(async () => {
  const params = new URLSearchParams(window.location.search)
  const spuId = (params.get('spu_id') || params.get('spu') || '').trim()
  if (!spuId) {
    statusEl.textContent = '缺少参数：请使用 ?spu_id=款号'
    statusEl.className = 'error-msg'
    titleEl.textContent = '参数错误'
    return
  }

  const apiUrl = `/spus/${encodeURIComponent(spuId)}/skus`
  linkJsonApi.href = apiUrl
  linkJsonApi.hidden = false

  try {
    const r = await fetch(apiUrl, { cache: 'no-store' })
    if (!r.ok) {
      throw new Error(`API ${r.status}`)
    }
    const data = await r.json()
    const rows = Array.isArray(data.skus) ? data.skus : []
    if (!rows.length) {
      statusEl.textContent = '该 SPU 下没有 SKU（或数据未加载）'
      statusEl.className = 'error-msg'
      titleEl.textContent = spuId
      return
    }
    render(spuId, rows)
  } catch (e) {
    statusEl.textContent =
      `${String(e.message || e)}。请使用 FastAPI 启动服务（同域 /spus/...）。`
    statusEl.className = 'error-msg'
    titleEl.textContent = '加载失败'
  }
})()
