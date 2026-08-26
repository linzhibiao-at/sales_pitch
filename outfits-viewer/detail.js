const mainEl = document.getElementById('main')
const statusEl = document.getElementById('status')

const setDocumentTitle = (text) => {
  document.title = text || 'FILA 商品详情'
}

const collectImageUrls = (item) => {
  const im = item.images || {}
  const urls = []
  const push = (u) => {
    if (u && typeof u === 'string' && !urls.includes(u)) {
      urls.push(u)
    }
  }
  push(im.cover)
  push(im.swatch)
  for (const u of im.outfitCd || []) {
    push(u)
  }
  for (const u of im.outfitCps || []) {
    push(u)
  }
  return urls
}

const TYPE_ROW_ORDER = ['big', 'master', 'bd']

const normImageTypeKey = (row) => String(row.imageType ?? '').trim().toLowerCase()

const cmpWithinImageType = (a, b) => {
  const oa = Number(a.orderId) || 0
  const ob = Number(b.orderId) || 0
  if (oa !== ob) {
    return oa - ob
  }
  const pa = Number(a.idPa) || 0
  const pb = Number(b.idPa) || 0
  if (pa !== pb) {
    return pa - pb
  }
  return String(a.path || '').localeCompare(String(b.path || ''))
}

const typeBlockSortIndex = (key) => {
  const i = TYPE_ROW_ORDER.indexOf(key)
  return i === -1 ? TYPE_ROW_ORDER.length : i
}

const catalogRowsByImageType = (item) => {
  const raw = item.allProductImages
  if (!Array.isArray(raw) || !raw.length) {
    return []
  }
  const buckets = new Map()
  const labelByKey = new Map()
  for (const row of raw) {
    const k = normImageTypeKey(row)
    const label = String(row.imageType ?? '').trim() || '—'
    if (!buckets.has(k)) {
      buckets.set(k, [])
      labelByKey.set(k, label)
    }
    buckets.get(k).push(row)
  }
  for (const rows of buckets.values()) {
    rows.sort(cmpWithinImageType)
  }
  const keys = [...buckets.keys()]
  keys.sort((a, b) => {
    const ia = typeBlockSortIndex(a)
    const ib = typeBlockSortIndex(b)
    if (ia !== ib) {
      return ia - ib
    }
    return a.localeCompare(b, 'en')
  })
  return keys.map((k) => ({
    displayLabel: labelByKey.get(k) || k || '—',
    rows: buckets.get(k),
  }))
}

const esc = (s) => {
  const d = document.createElement('div')
  d.textContent = s == null ? '' : String(s)
  return d.innerHTML
}

/** product_attr.attr_name，优先 color.attrName，兼容旧 colorName */
const resolveColorAttrName = (item) => {
  const c = item && item.color
  if (c) {
    const name = (c.attrName || c.colorName || '').trim()
    if (name) return name
  }
  return ((item && item.attrName) || '').trim()
}

const resolveItemAttr = (item) => item.attributes || item.meta || {}

const resolveSeries = (item) => {
  const top = (item && item.series) || ''
  if (String(top).trim()) {
    return String(top).trim()
  }
  const attr = resolveItemAttr(item)
  return attr.series ? String(attr.series).trim() : ''
}

const resolveCategoryL2 = (item) => {
  const top = (item && item.category_l2) || ''
  if (String(top).trim()) {
    return String(top).trim()
  }
  const attr = resolveItemAttr(item)
  return attr.middleClass || attr.catAlias || ''
}

const render = (item) => {
  setDocumentTitle(item.title || item.attrAlias || '商品详情')
  statusEl.remove()

  const parts = []
  parts.push('<div class="detail-panel">')
  parts.push('<h2>基本信息</h2>')
  parts.push('<dl class="meta-grid">')
  const title = (item.title || '').trim()
  parts.push(
    `<div class="meta-full"><dt>标题</dt><dd>${esc(title) || '—'}</dd></div>`,
  )
  const sku = (item.attrAlias || '').trim()
  const outfitSearchBtn = sku
    ? ` <a class="outfit-search-btn" href="by-sku.html?sku_id=${encodeURIComponent(sku)}" target="_blank" rel="noopener noreferrer">搭配检索</a>`
    : ''
  parts.push(`<div><dt>货号</dt><dd>${esc(item.attrAlias) || '—'}${outfitSearchBtn}</dd></div>`)
  parts.push(`<div><dt>款号</dt><dd>${esc(item.idAlias) || '—'}</dd></div>`)
  parts.push(
    `<div><dt>商品 ID</dt><dd>${item.idGoods != null ? esc(item.idGoods) : '—'}</dd></div>`,
  )
  const colorAttrName = resolveColorAttrName(item)
  if (colorAttrName || item.color) {
    parts.push(
      `<div><dt>颜色名称</dt><dd>${esc(colorAttrName) || '—'}</dd></div>`,
    )
    const idPaVal = item.color && item.color.idPa != null ? item.color.idPa : null
    parts.push(
      `<div><dt>idPa</dt><dd>${idPaVal != null ? esc(idPaVal) : '—'}</dd></div>`,
    )
  }
  if (item.price != null) {
    parts.push(
      `<div><dt>价格</dt><dd class="price">¥${esc(item.price)}</dd></div>`,
    )
  }
  if (item.up_time) {
    parts.push(
      `<div><dt>上架时间</dt><dd>${esc(item.up_time)}</dd></div>`,
    )
  }
  parts.push('</dl></div>')

  const attr = resolveItemAttr(item)
  parts.push(
    '<div class="detail-panel"><h2>属性</h2><dl class="meta-grid meta-grid-attrs">',
  )
  const attrFields = [
    ['性别', attr.sex],
    ['年龄', attr.age],
    ['上下装', attr.upDown],
    ['大类', attr.catType],
    ['中类', resolveCategoryL2(item)],
    ['小类', attr.categoryL3],
    ['品牌', attr.brand],
    ['集团品牌', attr.groupBrand],
    ['角色', attr.role],
    ['季节', attr.season],
    ['系列', resolveSeries(item)],
    ['子系列', attr.subSeries],
    ['色系', item.color_series],
    ['风格', item.style_tags],
    ['场景', item.occasion_tags],
    ['场景域', item.scene_domain],
    ['面料功能', item.fabric_function],
    ['材质', attr.material],
    ['层次', attr.layer],
    ['覆盖', attr.coverage],
    ['长度', attr.lengthClass],
    ['版型', attr.modeling],
    ['是否贴身', attr.isIntimate == null ? '—' : (attr.isIntimate ? '是' : '否')],
  ]
  for (const [k, v] of attrFields) {
    let dd
    if (Array.isArray(v)) {
      dd = v.length
        ? v.map((t) => `<span class="attr-tag">${esc(t)}</span>`).join(' ')
        : '—'
    } else {
      dd = esc(v) || '—'
    }
    parts.push(`<div><dt>${esc(k)}</dt><dd>${dd}</dd></div>`)
  }
  parts.push('</dl></div>')

  const kw = (item.search_keywords || '').trim()
  parts.push('<div class="detail-panel detail-panel-keywords">')
  parts.push('<h2>搜索关键词</h2>')
  parts.push(`<p class="keywords-body">${esc(kw) || '—'}</p>`)
  parts.push('</div>')

  const typeGroups = catalogRowsByImageType(item)
  const ai = item.aiSelect
  if (ai && typeof ai.path === 'string' && ai.path.trim()) {
    typeGroups.push({
      displayLabel: 'ai_select',
      rows: [
        {
          path: ai.path.trim(),
          idPa: ai.chosenIdPa != null && ai.chosenIdPa !== '' ? ai.chosenIdPa : '—',
          orderId: ai.chosenOrderId != null && ai.chosenOrderId !== '' ? ai.chosenOrderId : '—',
          imageType: ai.chosenImageType != null && ai.chosenImageType !== '' ? ai.chosenImageType : 'ai_select',
          note: typeof ai.note === 'string' ? ai.note : '',
          candidateCount:
            ai.candidateCount != null ? String(ai.candidateCount) : '',
        },
      ],
    })
  }

  if (Array.isArray(item.indexImages) && item.indexImages.length) {
    typeGroups.push({
      displayLabel: 'index_images',
      rows: item.indexImages.map((u, i) => ({
        path: u,
        idPa: '—',
        orderId: i + 1,
        imageType: 'index_images',
      })),
    })
  }

  if (typeGroups.length) {
    parts.push(
      '<div class="detail-panel"><h2>商品图（按 image_type）</h2>' +
        '<div class="catalog-by-type">',
    )
    for (const grp of typeGroups) {
      const isAi = grp.displayLabel === 'ai_select'
      parts.push(
        `<div class="catalog-type-row${isAi ? ' catalog-type-row--ai' : ''}">`,
      )
      parts.push(
        `<h3 class="catalog-type-label">image_type: ${esc(grp.displayLabel)}</h3>`,
      )
      parts.push('<div class="gallery">')
      for (const row of grp.rows) {
        const ipa = esc(String(row.idPa ?? '—'))
        const oid = esc(String(row.orderId ?? '—'))
        const ity = esc(String(row.imageType ?? '—'))
        const path = esc(String(row.path || ''))
        parts.push('<figure class="gallery-figure">')
        parts.push(
          `<a class="gallery-link" href="${path}" target="_blank" rel="noopener noreferrer">` +
            `<img src="${path}" alt="" loading="lazy" decoding="async" /></a>`,
        )
        parts.push('<figcaption class="gallery-caption">')
        parts.push(`<span class="gallery-caption-line">id_pa: ${ipa}</span>`)
        parts.push(`<span class="gallery-caption-line">order_id: ${oid}</span>`)
        parts.push(
          `<span class="gallery-caption-line">image_type: ${ity}</span>`,
        )
        if (isAi && row.note) {
          parts.push(
            `<span class="gallery-caption-line">note: ${esc(row.note)}</span>`,
          )
        }
        if (isAi && row.candidateCount) {
          parts.push(
            `<span class="gallery-caption-line">候选数: ${esc(row.candidateCount)}</span>`,
          )
        }
        parts.push('</figcaption></figure>')
      }
      parts.push('</div></div>')
    }
    parts.push('</div></div>')
  }

  const imgs = collectImageUrls(item)
  if (imgs.length) {
    parts.push('<div class="detail-panel"><h2>图片（cover / 搭配）</h2><div class="gallery">')
    for (const u of imgs) {
      parts.push(
        `<a class="gallery-link-plain" href="${esc(u)}" target="_blank" rel="noopener noreferrer">` +
          `<img src="${esc(u)}" alt="" loading="lazy" decoding="async" /></a>`,
      )
    }
    parts.push('</div></div>')
  }

  mainEl.innerHTML = parts.join('')
}

const normalizeAllImages = (raw) => {
  if (!Array.isArray(raw)) {
    return []
  }
  const out = []
  for (const item of raw) {
    if (typeof item === 'string' && item.trim()) {
      out.push({
        path: item.trim(),
        idPa: '—',
        orderId: '—',
        imageType: '—',
      })
      continue
    }
    if (!item || typeof item !== 'object') {
      continue
    }
    const path = String(
      item.path || item.url || item.image_url || '',
    ).trim()
    if (!path) {
      continue
    }
    out.push({
      path,
      idPa: item.idPa ?? item.id_pa ?? '—',
      orderId: item.orderId ?? item.order_id ?? '—',
      imageType: item.imageType ?? item.image_type ?? '—',
    })
  }
  return out
}

const normalizeAiSelect = (raw) => {
  if (!raw || typeof raw !== 'object') {
    return null
  }
  const path = String(raw.path || '').trim()
  if (!path) {
    return null
  }
  return {
    path,
    note: raw.note != null ? String(raw.note) : '',
    candidateCount: raw.candidateCount ?? raw.candidate_count ?? '',
    chosenIdPa: raw.chosenIdPa ?? raw.chosen_id_pa ?? '',
    chosenOrderId: raw.chosenOrderId ?? raw.chosen_order_id ?? '',
    chosenImageType: raw.chosenImageType ?? raw.chosen_image_type ?? 'ai_select',
  }
}

const normalizeIndexImages = (raw) => {
  if (Array.isArray(raw)) {
    return raw
      .map((u) => (typeof u === 'string' ? u.trim() : ''))
      .filter(Boolean)
  }
  if (typeof raw === 'string' && raw.trim()) {
    // 兼容 JSON 字符串形态
    try {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        return parsed
          .map((u) => (typeof u === 'string' ? u.trim() : ''))
          .filter(Boolean)
      }
    } catch (_) {}
    return [raw.trim()]
  }
  return []
}

/** 将可能是 JSON 字符串或数组的 tags 字段统一解析为字符串数组 */
function parseTags(val) {
  if (!val) return []
  let arr = val
  // 字符串 → 尝试 JSON.parse（可能需要多轮解转义）
  for (let i = 0; i < 3 && typeof arr === 'string'; i++) {
    try { arr = JSON.parse(arr) } catch (_) { break }
  }
  if (Array.isArray(arr)) {
    return arr.map((v) => {
      if (typeof v !== 'string') return String(v)
      // 处理双重转义的 unicode，如 \\u8170 → 腰
      if (v.includes('\\u')) {
        try { return JSON.parse('"' + v + '"') } catch (_) {}
      }
      return v
    })
  }
  return []
}

const normalizeSkuForDetail = (row) => ({
  attrAlias: row.sku_id || row.attrAlias || '',
  idAlias: row.spu_id || row.idAlias || '',
  idGoods: row.goods_id ?? row.idGoods ?? null,
  title: row.title || '',
  category_l2: row.category_l2 || '',
  series: row.series || '',
  price: row.price,
  up_time: row.up_time || '',
  search_keywords: String(
    row.search_keywords || row.search_text || '',
  ).trim(),
  color: {
    attrName: row.color_name || row.attr_name || '',
    colorName: row.color_name || row.attr_name || '',
    idPa: row.id_pa ?? row.idPa ?? null,
  },
  color_series: parseTags(row.color_series),
  style_tags: parseTags(row.style_tags),
  occasion_tags: parseTags(row.occasion_tags),
  scene_domain: row.scene_domain || '',
  fabric_function: parseTags(row.fabric_function),
  attributes: {
    sex: row.gender || '',
    age: row.age || '',
    upDown: row.up_down_raw || '',
    catType: row.category_l1 || '',
    season: Array.isArray(row.season) ? row.season.join(' / ') : row.season || '',
    series: row.series || '',
    brand: row.brand || '',
    groupBrand: row.group_brand || '',
    categoryL3: row.category_l3 || '',
    role: row.role || '',
    subSeries: row.sub_series || '',
    material: row.material || '',
    layer: row.layer || '',
    coverage: row.coverage || '',
    lengthClass: row.length_class || '',
    modeling: row.modeling || '',
    isIntimate: row.is_intimate,
  },
  images: {
    cover: row.display_image || row.index_image || row.tryon_image || '',
    swatch: '',
    outfitCd: row.index_image ? [row.index_image] : [],
    outfitCps: row.tryon_image ? [row.tryon_image] : [],
  },
  allProductImages: normalizeAllImages(row.all_images || row.allProductImages),
  aiSelect: normalizeAiSelect(row.ai_select || row.aiSelect),
  indexImages: normalizeIndexImages(row.index_images || row.indexImages),
  raw: row,
})

;(async () => {
  const params = new URLSearchParams(window.location.search)
  const sku = (params.get('sku') || params.get('attrAlias') || '').trim()

  if (!sku) {
    statusEl.textContent = '缺少参数：请使用 ?sku=货号'
    statusEl.className = 'error-msg'
    return
  }

  try {
    const res = await fetch(`/skus/${encodeURIComponent(sku)}`, { cache: 'no-store' })
    if (!res.ok) {
      throw new Error(`/skus/${sku} ${res.status}`)
    }
    const item = normalizeSkuForDetail(await res.json())
    if (!item) {
      statusEl.textContent = '未找到该商品（索引中无此货号或 ID）'
      statusEl.className = 'error-msg'
      setDocumentTitle('未找到')
      return
    }
    render(item)
  } catch (e) {
    statusEl.textContent = String(e.message || e)
    statusEl.className = 'error-msg'
    setDocumentTitle('加载失败')
  }
})()
