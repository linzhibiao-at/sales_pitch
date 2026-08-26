/* global window, document, fetch */

const mainEl = document.getElementById('main')
const statusEl = document.getElementById('status')
const titleEl = document.getElementById('page-title')
const linkJsonApi = document.getElementById('link-json-api')
const linkJsonFile = document.getElementById('link-json-file')

const esc = (s) => {
  const d = document.createElement('div')
  d.textContent = s == null ? '' : String(s)
  return d.innerHTML
}

const pickUrl = (o) => {
  if (!o) {
    return ''
  }
  if (typeof o === 'string') {
    return o.trim()
  }
  if (typeof o === 'object') {
    if (typeof o.path === 'string' && o.path.trim()) {
      return o.path.trim()
    }
    if (typeof o.url === 'string' && o.url.trim()) {
      return o.url.trim()
    }
  }
  return ''
}

const uniqImageRows = (rows) => {
  const seen = new Set()
  const out = []
  for (const row of rows) {
    const u = row.url
    if (!u || seen.has(u)) {
      continue
    }
    seen.add(u)
    out.push(row)
  }
  return out
}

const normalizeFromProcessed = (raw) => {
  const rows = []
  const push = (label, u) => {
    const url = typeof u === 'string' ? u.trim() : ''
    if (url) {
      rows.push({ label, url })
    }
  }
  push('display_image', raw.display_image)
  push('index_image', raw.index_image)
  push('tryon_image', raw.tryon_image)
  const all = raw.all_images
  if (Array.isArray(all)) {
    all.forEach((item, i) => {
      const u = typeof item === 'string' ? item : pickUrl(item)
      push(`all_images[${i}]`, u)
    })
  }
  const season = raw.season
  const seasonStr = Array.isArray(season) ? season.join('、') : ''
  const iq = raw.image_quality || {}
  return {
    title: raw.title || raw.sku_id || '商品详情',
    metaBasic: [
      ['货号 (sku_id)', raw.sku_id],
      ['款号 (spu_id)', raw.spu_id],
      ['商品 ID (goods_id)', raw.goods_id],
      ['id_pa', raw.id_pa],
      ['价格', raw.price != null ? `¥${raw.price}` : null],
      ['性别', raw.gender],
      ['颜色', raw.color_name || raw.color_family],
      ['品类 role', raw.role],
      ['系列', raw.series],
      ['子系列', raw.sub_series],
      ['季节', seasonStr || null],
    ],
    metaExtra: [
      ['大类', raw.category_l1],
      ['中类', raw.category_l2],
      ['小类', raw.category_l3],
      ['场合标签', Array.isArray(raw.occasion_tags) ? raw.occasion_tags.join('、') : ''],
      ['风格标签', Array.isArray(raw.style_tags) ? raw.style_tags.join('、') : ''],
      ['试衣就绪', iq.is_tryon_ready != null ? String(iq.is_tryon_ready) : ''],
    ],
    imageRows: uniqImageRows(rows),
    source: 'processed',
    raw,
  }
}

const normalizeFromDebugJson = (raw) => {
  const sel = raw.selected || {}
  const rows = []
  const push = (label, obj) => {
    const url = pickUrl(obj)
    if (url) {
      rows.push({ label, url })
    }
  }
  push('display_image', sel.display_image)
  push('index_image', sel.index_image)
  push('tryon_image', sel.tryon_image)
  const flags = raw.flags || {}
  const flagParts = Object.keys(flags).filter((k) => flags[k]).map((k) => k)
  return {
    title: raw.title || raw.sku_id || '商品详情',
    metaBasic: [
      ['货号 (sku_id)', raw.sku_id],
      ['款号 (spu_id)', raw.spu_id],
      ['颜色', raw.color_name],
      ['品类 role', raw.role],
      ['图片 Debug flags', flagParts.length ? flagParts.join('、') : null],
    ],
    metaExtra: [],
    imageRows: uniqImageRows(rows),
    source: 'debug_json',
    raw,
  }
}

const normalizeRecord = (raw) => {
  if (!raw || typeof raw !== 'object') {
    return null
  }
  if (raw.selected && typeof raw.selected === 'object') {
    return normalizeFromDebugJson(raw)
  }
  if (raw.sku_id && (raw.display_image || raw.index_image || raw.search_text)) {
    return normalizeFromProcessed(raw)
  }
  if (raw.sku_id) {
    return normalizeFromProcessed(raw)
  }
  return null
}

const renderMetaSection = (title, pairs) => {
  const filtered = pairs.filter(([, v]) => v != null && String(v).trim() !== '')
  if (!filtered.length) {
    return ''
  }
  const parts = [`<div class="detail-panel"><h2>${esc(title)}</h2><dl class="meta-grid">`]
  for (const [k, v] of filtered) {
    parts.push(`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`)
  }
  parts.push('</dl></div>')
  return parts.join('')
}

const render = (norm, opts) => {
  const fallbackNote = opts && opts.fallbackNote ? String(opts.fallbackNote) : ''
  titleEl.textContent = norm.title
  statusEl.remove()

  const parts = []
  if (fallbackNote) {
    parts.push(
      '<div class="detail-panel" style="border-color:#3d4a5c">' +
        `<p class="gallery-caption">${esc(fallbackNote)}</p></div>`,
    )
  }
  parts.push(renderMetaSection('基本信息', norm.metaBasic))
  parts.push(renderMetaSection('属性 / 标签', norm.metaExtra))

  if (norm.imageRows.length) {
    parts.push(
      '<div class="detail-panel"><h2>商品图</h2><div class="catalog-by-type">',
    )
    parts.push('<div class="catalog-type-row">')
    parts.push('<h3 class="catalog-type-label">主图与索引图</h3>')
    parts.push('<div class="gallery">')
    for (const row of norm.imageRows) {
      const path = esc(row.url)
      const lab = esc(row.label)
      parts.push('<figure class="gallery-figure">')
      parts.push(
        `<a class="gallery-link" href="${path}" target="_blank" rel="noopener noreferrer">` +
          `<img src="${path}" alt="" loading="lazy" decoding="async" /></a>`,
      )
      parts.push('<figcaption class="gallery-caption">')
      parts.push(`<span class="gallery-caption-line">${lab}</span>`)
      parts.push('</figcaption></figure>')
    }
    parts.push('</div></div></div></div>')
  } else {
    parts.push(
      '<div class="detail-panel"><h2>商品图</h2><p class="gallery-caption">暂无图片 URL</p></div>',
    )
  }

  const rawStr = JSON.stringify(norm.raw, null, 2)
  parts.push(
    '<details class="json-toggle"><summary>查看原始 JSON</summary>' +
      `<pre>${esc(rawStr)}</pre></details>`,
  )

  mainEl.innerHTML = parts.join('')
}

const loadSku = async (skuId) => {
  const enc = encodeURIComponent(skuId)
  const apiUrl = `/skus/${enc}`

  linkJsonApi.href = apiUrl
  linkJsonApi.hidden = false
  linkJsonFile.hidden = true

  let raw = null
  let errMsg = ''

  try {
    const r = await fetch(apiUrl, { cache: 'no-store' })
    if (r.ok) {
      raw = await r.json()
    } else {
      errMsg = `API ${r.status}`
    }
  } catch {
    errMsg = '无法请求 API，请用 uvicorn 启动服务'
  }

  if (!raw) {
    statusEl.textContent = errMsg || '未找到商品数据'
    statusEl.className = 'error-msg'
    titleEl.textContent = '未找到'
    return
  }

  const norm = normalizeRecord(raw)
  if (!norm) {
    statusEl.textContent = '无法解析数据结构'
    statusEl.className = 'error-msg'
    titleEl.textContent = '解析失败'
    return
  }

  render(norm, { fallbackNote: errMsg })
}

;(async () => {
  const params = new URLSearchParams(window.location.search)
  const sku = (params.get('sku_id') || params.get('sku') || '').trim()
  if (!sku) {
    statusEl.textContent = '缺少参数：请使用 ?sku_id=货号'
    statusEl.className = 'error-msg'
    titleEl.textContent = '参数错误'
    linkJsonApi.hidden = true
    linkJsonFile.hidden = true
    return
  }
  await loadSku(sku)
})()
