/* global window, document, fetch */

function params() {
  const u = new URL(window.location.href)
  return {
    sku_id: u.searchParams.get('sku_id'),
    spu_id: u.searchParams.get('spu_id'),
    raw: u.searchParams.get('raw'),
  }
}

async function loadJson(path) {
  const r = await fetch(path)
  if (!r.ok) {
    throw new Error(`${path} -> ${r.status}`)
  }
  return r.json()
}

function detailPageForQuery(q) {
  const isSku = q.length >= 8 && /[0-9]/.test(q)
  const name = isSku ? 'sku-detail.html' : 'spu-detail.html'
  const u = new URL(name, window.location.href)
  u.searchParams.delete('sku_id')
  u.searchParams.delete('spu_id')
  u.searchParams.delete('raw')
  u.searchParams.set(isSku ? 'sku_id' : 'spu_id', q)
  return u.toString()
}

async function run() {
  const out = document.getElementById('json-out')
  const rawOnly = document.getElementById('raw-only')
  const p = params()
  const base = 'data/'
  const forceJson = p.raw === '1' || (rawOnly && rawOnly.checked)

  if (!forceJson && (p.sku_id || p.spu_id)) {
    window.location.replace(detailPageForQuery(p.sku_id || p.spu_id))
    return
  }

  try {
    if (p.sku_id) {
      const data = await loadJson(`${base}sku/${p.sku_id}.json`)
      out.textContent = JSON.stringify(data, null, 2)
      return
    }
    if (p.spu_id) {
      const data = await loadJson(`${base}spu/${p.spu_id}.json`)
      out.textContent = JSON.stringify(data, null, 2)
      return
    }
    const idx = await loadJson(`${base}image_debug_index.json`)
    out.textContent = JSON.stringify(
      { hint: 'index summary', keys: Object.keys(idx).slice(0, 20) },
      null,
      2,
    )
  } catch (e) {
    out.textContent = String(e)
  }
}

document.getElementById('search-form').addEventListener('submit', (ev) => {
  ev.preventDefault()
  const q = document.getElementById('q').value.trim()
  if (!q) {
    return
  }
  const rawOnly = document.getElementById('raw-only')
  if (rawOnly && rawOnly.checked) {
    const u = new URL(window.location.href)
    u.searchParams.delete('sku_id')
    u.searchParams.delete('spu_id')
    u.searchParams.set('raw', '1')
    if (q.length >= 8 && /[0-9]/.test(q)) {
      u.searchParams.set('sku_id', q)
    } else {
      u.searchParams.set('spu_id', q)
    }
    window.location.href = u.toString()
    return
  }
  window.location.href = detailPageForQuery(q)
})

run()
