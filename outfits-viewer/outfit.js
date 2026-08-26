const statusEl = document.getElementById('status')
const singleEl = document.getElementById('outfit-single')
const titleMeta = document.getElementById('title-meta')
const pageTitle = document.getElementById('page-title')

const parseOutfitId = () => {
  const params = new URLSearchParams(window.location.search)
  const raw = (params.get('outfit_id') || params.get('idMatch') || params.get('id') || '').trim()
  if (!raw) {
    return null
  }
  return raw
}

const showStatus = (msg, isError) => {
  statusEl.textContent = msg
  statusEl.className = isError ? 'error-msg' : ''
  statusEl.style.display = 'block'
}

;(async () => {
  const want = parseOutfitId()
  if (want == null) {
    showStatus('请在地址栏加上参数，例如：outfit.html?outfit_id=8945', true)
    titleMeta.textContent = '缺少 outfit_id'
    return
  }

  titleMeta.textContent = `查找 outfit_id=${want} …`

  try {
    const res = await fetch(`/outfits/${encodeURIComponent(want)}`, { cache: 'no-store' })
    if (!res.ok) {
      showStatus(`未找到 outfit_id=${want}（API ${res.status}）`, true)
      titleMeta.textContent = '未找到'
      pageTitle.textContent = '单套搭配'
      return
    }
    const found = await res.json()

    statusEl.style.display = 'none'
    titleMeta.textContent = `${found.name || '搭配'} · ${want}`
    pageTitle.textContent = found.name || `搭配 #${want}`
    document.title = `${found.name || '搭配'} · #${want}`
    singleEl.appendChild(renderOutfit(found))
  } catch (e) {
    showStatus(String(e.message || e), true)
    titleMeta.textContent = '加载失败'
  }
})()
