const listEl = document.getElementById('outfit-list')
const sentinel = document.getElementById('load-sentinel')
const titleMeta = document.getElementById('title-meta')

const esc = (s) => {
  const d = document.createElement('div')
  d.textContent = s == null ? '' : String(s)
  return d.innerHTML
}

const showError = (msg) => {
  const b = document.createElement('div')
  b.className = 'error-banner'
  b.textContent = msg
  document.body.insertBefore(b, document.getElementById('scroll-root'))
}

;(async () => {
  const params = new URLSearchParams(window.location.search)
  const sku = (params.get('sku_id') || params.get('sku') || '').trim()
  if (!sku) {
    titleMeta.textContent = '缺少参数：请使用 ?sku_id=货号'
    showError('缺少参数：请使用 ?sku_id=货号')
    return
  }

  titleMeta.textContent = `检索 ${sku} 的固定搭配…`
  try {
    const url = `/api/outfits/by-sku/${encodeURIComponent(sku)}`
    const res = await fetch(url, { cache: 'no-store' })
    if (!res.ok) {
      throw new Error(`${url} ${res.status}`)
    }
    const payload = await res.json()
    const outfits = payload.outfits || []
    const total = Number(payload.total) || outfits.length
    titleMeta.textContent = `sku_id：${sku} · 共 ${total} 套搭配`
    if (!outfits.length) {
      sentinel.textContent = '未检索到该 SKU 的固定搭配'
      return
    }
    for (const outfit of outfits) {
      listEl.appendChild(renderOutfit(outfit))
    }
    sentinel.textContent = '已加载全部'
  } catch (e) {
    titleMeta.textContent = '加载失败'
    showError(String(e.message || e))
    console.error(e)
  }
})()
