const SOURCE_LABELS = {
  cc_material: '素材中心',
  micro_guide: 'cc_material_product',
  dphs_outfits: 'ppt',
  outfits_unique: '设计师搭配',
}

const formatSourceLabel = (source) => SOURCE_LABELS[source] || source || '未知'

const outfitDisplayId = (outfit) => outfit.idMatch || outfit.outfit_id || ''

const outfitColorSeriesTags = (outfit) => {
  const tags = outfit.color_series_tags
  if (Array.isArray(tags) && tags.length) {
    return tags.map((t) => String(t).trim()).filter(Boolean)
  }
  return []
}

const pickHeroUrl = (outfit) => {
  if (outfit.leftHeroUrl) {
    return outfit.leftHeroUrl
  }
  const bg = outfit.backgroundImg
  if (Array.isArray(bg) && bg.length) {
    return bg[0]
  }
  if (typeof bg === 'string' && bg.startsWith('http')) {
    return bg
  }
  if (outfit.display_image) {
    return outfit.display_image
  }
  if (outfit.index_image) {
    return outfit.index_image
  }
  const items = outfit.items || []
  const master = items.find((i) => i.isMaster) || items[0]
  const pickFrom = (it) => {
    if (!it) {
      return null
    }
    const im = it.images || {}
    if (im.outfitCd && im.outfitCd[0]) {
      return im.outfitCd[0]
    }
    if (im.outfitCps && im.outfitCps[0]) {
      return im.outfitCps[0]
    }
    if (im.cover) {
      return im.cover
    }
    if (it.tryon_image) {
      return it.tryon_image
    }
    if (it.display_image) {
      return it.display_image
    }
    return null
  }
  const u = pickFrom(master)
  if (u) {
    return u
  }
  for (const it of items) {
    const v = pickFrom(it)
    if (v) {
      return v
    }
  }
  return null
}

const pickItemThumb = (item) => {
  const ai = item.aiSelect
  if (ai && typeof ai.path === 'string' && ai.path.trim()) {
    return ai.path.trim()
  }
  const im = item.images || {}
  if (im.cover) {
    return im.cover
  }
  if (im.swatch) {
    return im.swatch
  }
  if (im.outfitCd && im.outfitCd[0]) {
    return im.outfitCd[0]
  }
  if (item.tryon_image) {
    return item.tryon_image
  }
  if (item.display_image) {
    return item.display_image
  }
  return null
}

const renderOutfit = (outfit) => {
  const displayId = outfitDisplayId(outfit)
  const row = document.createElement('article')
  row.className = 'outfit-row'
  row.dataset.idMatch = String(displayId)

  const items = outfit.items || []
  const heroWrap = document.createElement('div')
  heroWrap.className = 'outfit-hero-wrap'
  const hero = document.createElement('div')
  hero.className = 'outfit-hero'
  const heroUrl = pickHeroUrl(outfit)
  if (heroUrl) {
    const img = document.createElement('img')
    img.alt = outfit.name || `搭配 ${displayId}`
    img.loading = 'lazy'
    img.decoding = 'async'
    img.src = heroUrl
    hero.appendChild(img)
  } else {
    const ph = document.createElement('div')
    ph.className = 'placeholder'
    ph.textContent = '暂无穿搭图'
    hero.appendChild(ph)
  }
  heroWrap.appendChild(hero)
  const masterForHero = items.find((i) => i.isMaster) || items[0]
  const heroAlias = ((masterForHero && masterForHero.attrAlias) || '').trim()
  if (heroAlias) {
    const cap = document.createElement('div')
    cap.className = 'outfit-hero-alias'
    cap.textContent = heroAlias
    heroWrap.appendChild(cap)
  }

  const main = document.createElement('div')
  main.className = 'outfit-main'
  const h2 = document.createElement('h2')
  h2.className = 'outfit-title'
  h2.textContent = outfit.name || `搭配 #${displayId}`
  const sub = document.createElement('div')
  sub.className = 'outfit-sub'
  const parts = [
    outfit.source && `来源：${formatSourceLabel(outfit.source)}`,
    outfit.shopName && `店铺：${outfit.shopName}`,
    displayId && `ID：${displayId}`,
    outfit.flags?.hasPdpOutfitImage && 'PDP穿搭图',
    outfit.flags?.hasCpsOutfitImage && 'CPS穿搭图',
  ].filter(Boolean)
  sub.textContent = parts.join(' · ')

  const colorTags = outfitColorSeriesTags(outfit)
  if (colorTags.length) {
    const tagRow = document.createElement('div')
    tagRow.className = 'outfit-color-tags'
    for (const tag of colorTags) {
      const chip = document.createElement('span')
      chip.className = 'color-series-chip'
      chip.textContent = tag
      tagRow.appendChild(chip)
    }
    main.appendChild(tagRow)
  }

  const grid = document.createElement('div')
  grid.className = 'items-grid'
  for (const item of items) {
    const alias = (item.attrAlias || '').trim()
    let card
    let href = ''
    if (alias.length > 0) {
      href = `detail.html?sku=${encodeURIComponent(alias)}`
    }
    if (href) {
      card = document.createElement('a')
      card.href = href
      card.target = '_blank'
      card.rel = 'noopener noreferrer'
      card.className = 'item-card item-card-link' + (item.isMaster ? ' master' : '')
    } else {
      card = document.createElement('div')
      card.className = 'item-card' + (item.isMaster ? ' master' : '')
    }
    const thumb = document.createElement('div')
    thumb.className = 'thumb'
    const url = pickItemThumb(item)
    if (url) {
      const im = document.createElement('img')
      im.alt = item.title || item.attrAlias || ''
      im.loading = 'lazy'
      im.decoding = 'async'
      im.src = url
      thumb.appendChild(im)
    } else {
      const ph = document.createElement('span')
      ph.style.cssText = 'color:#555;font-size:0.7rem;padding:8px;'
      ph.textContent = '无图'
      thumb.appendChild(ph)
    }
    const info = document.createElement('div')
    info.className = 'info'
    const name = document.createElement('div')
    name.className = 'name'
    name.textContent = item.title || '—'
    const sku = document.createElement('div')
    sku.className = 'sku'
    sku.textContent = item.attrAlias || ''
    info.appendChild(name)
    info.appendChild(sku)
    card.appendChild(thumb)
    card.appendChild(info)
    grid.appendChild(card)
  }

  main.appendChild(h2)
  main.appendChild(sub)
  main.appendChild(grid)
  row.appendChild(heroWrap)
  row.appendChild(main)
  return row
}
