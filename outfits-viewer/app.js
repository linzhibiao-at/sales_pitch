const scrollRoot = document.getElementById('scroll-root')
const listEl = document.getElementById('outfit-list')
const sentinel = document.getElementById('load-sentinel')
const titleMeta = document.getElementById('title-meta')
const sourceTabsEl = document.getElementById('source-tabs')
const colorSeriesTabsEl = document.getElementById('color-series-tabs')
const seasonTabsEl = document.getElementById('season-tabs')

const state = {
  offset: 0,
  pageSize: 80,
  total: null,
  loading: false,
  done: false,
  rendered: 0,
  activeSource: null,
  activeColorSeries: null,
  activeSeason: null,
  sources: [],
  colorSeries: [],
  seasons: [],
}

const readParamFromUrl = (key) => {
  const raw = new URLSearchParams(window.location.search).get(key)
  return raw && raw.trim() ? raw.trim() : null
}

const updateUrlParams = () => {
  const url = new URL(window.location.href)
  if (state.activeSource) {
    url.searchParams.set('source', state.activeSource)
  } else {
    url.searchParams.delete('source')
  }
  if (state.activeColorSeries) {
    url.searchParams.set('color_series', state.activeColorSeries)
  } else {
    url.searchParams.delete('color_series')
  }
  if (state.activeSeason) {
    url.searchParams.set('season', state.activeSeason)
  } else {
    url.searchParams.delete('season')
  }
  window.history.replaceState({}, '', url.toString())
}

const resetList = () => {
  state.offset = 0
  state.total = null
  state.done = false
  state.rendered = 0
  listEl.innerHTML = ''
  sentinel.textContent = '向下滚动加载更多'
}

const setLoading = (on) => {
  state.loading = on
  sentinel.classList.toggle('loading', on)
  sentinel.textContent = on ? '加载中' : state.done ? '已加载全部' : ''
}

const makeFilterTab = (container, { value, label, count, active, onClick }) => {
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = 'filter-tab'
  if (active) {
    btn.classList.add('active')
  }
  btn.dataset.value = value || ''
  btn.innerHTML = `${label}<span class="count">${count}</span>`
  btn.addEventListener('click', onClick)
  container.appendChild(btn)
}

const renderSourceTabs = () => {
  sourceTabsEl.innerHTML = ''
  const allCount = state.sources.reduce(
    (sum, item) => sum + Number(item.count || 0),
    0,
  )

  makeFilterTab(sourceTabsEl, {
    value: '',
    label: '全部来源',
    count: allCount,
    active: !state.activeSource,
    onClick: () => {
      switchSource(null)
    },
  })
  for (const item of state.sources) {
    const source = String(item.source || '').trim()
    if (!source) {
      continue
    }
    makeFilterTab(sourceTabsEl, {
      value: source,
      label: formatSourceLabel(source),
      count: Number(item.count || 0),
      active: state.activeSource === source,
      onClick: () => {
        switchSource(source)
      },
    })
  }
}

const renderColorSeriesTabs = () => {
  colorSeriesTabsEl.innerHTML = ''
  const allCount = state.colorSeries.reduce(
    (sum, item) => sum + Number(item.count || 0),
    0,
  )

  makeFilterTab(colorSeriesTabsEl, {
    value: '',
    label: '全部色系',
    count: allCount,
    active: !state.activeColorSeries,
    onClick: () => {
      switchColorSeries(null)
    },
  })
  for (const item of state.colorSeries) {
    const colorSeries = String(item.color_series || '').trim()
    if (!colorSeries) {
      continue
    }
    makeFilterTab(colorSeriesTabsEl, {
      value: colorSeries,
      label: colorSeries,
      count: Number(item.count || 0),
      active: state.activeColorSeries === colorSeries,
      onClick: () => {
        switchColorSeries(colorSeries)
      },
    })
  }
}

const renderSeasonTabs = () => {
  seasonTabsEl.innerHTML = ''
  const allCount = state.seasons.reduce(
    (sum, item) => sum + Number(item.count || 0),
    0,
  )

  makeFilterTab(seasonTabsEl, {
    value: '',
    label: '全部季节',
    count: allCount,
    active: !state.activeSeason,
    onClick: () => {
      switchSeason(null)
    },
  })
  for (const item of state.seasons) {
    const season = String(item.season || '').trim()
    if (!season) {
      continue
    }
    makeFilterTab(seasonTabsEl, {
      value: season,
      label: season,
      count: Number(item.count || 0),
      active: state.activeSeason === season,
      onClick: () => {
        switchSeason(season)
      },
    })
  }
}

const switchSource = async (source) => {
  const next = source || null
  if (state.activeSource === next) {
    return
  }
  state.activeSource = next
  updateUrlParams()
  renderSourceTabs()
  resetList()
  await loadColorSeries()
  renderColorSeriesTabs()
  await loadSeasons()
  renderSeasonTabs()
  loadNextChunk()
}

const switchColorSeries = (colorSeries) => {
  const next = colorSeries || null
  if (state.activeColorSeries === next) {
    return
  }
  state.activeColorSeries = next
  updateUrlParams()
  renderColorSeriesTabs()
  resetList()
  loadNextChunk()
}

const switchSeason = (season) => {
  const next = season || null
  if (state.activeSeason === next) {
    return
  }
  state.activeSeason = next
  updateUrlParams()
  renderSeasonTabs()
  resetList()
  loadNextChunk()
}

const loadSources = async () => {
  const res = await fetch('/api/outfits/sources', { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`/api/outfits/sources ${res.status}`)
  }
  const payload = await res.json()
  state.sources = payload.sources || []
  const available = new Set(
    state.sources.map((item) => String(item.source || '').trim()).filter(Boolean),
  )
  const fromUrl = readParamFromUrl('source')
  state.activeSource = fromUrl && available.has(fromUrl) ? fromUrl : null
  renderSourceTabs()
}

const loadColorSeries = async () => {
  const params = new URLSearchParams()
  if (state.activeSource) {
    params.set('source', state.activeSource)
  }
  const query = params.toString()
  const url = query
    ? `/api/outfits/color-series?${query}`
    : '/api/outfits/color-series'
  const res = await fetch(url, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`/api/outfits/color-series ${res.status}`)
  }
  const payload = await res.json()
  state.colorSeries = payload.color_series || []
  const available = new Set(
    state.colorSeries
      .map((item) => String(item.color_series || '').trim())
      .filter(Boolean),
  )
  const fromUrl = readParamFromUrl('color_series')
  state.activeColorSeries = fromUrl && available.has(fromUrl) ? fromUrl : null
  renderColorSeriesTabs()
}

const loadSeasons = async () => {
  const params = new URLSearchParams()
  if (state.activeSource) {
    params.set('source', state.activeSource)
  }
  const query = params.toString()
  const url = query
    ? `/api/outfits/season?${query}`
    : '/api/outfits/season'
  const res = await fetch(url, { cache: 'no-store' })
  if (!res.ok) {
    throw new Error(`/api/outfits/season ${res.status}`)
  }
  const payload = await res.json()
  state.seasons = payload.season || []
  const available = new Set(
    state.seasons
      .map((item) => String(item.season || '').trim())
      .filter(Boolean),
  )
  const fromUrl = readParamFromUrl('season')
  state.activeSeason = fromUrl && available.has(fromUrl) ? fromUrl : null
  renderSeasonTabs()
}

const buildFilterLabel = () => {
  const parts = []
  if (state.activeSource) {
    parts.push(formatSourceLabel(state.activeSource))
  } else {
    parts.push('全部来源')
  }
  if (state.activeColorSeries) {
    parts.push(state.activeColorSeries)
  } else {
    parts.push('全部色系')
  }
  if (state.activeSeason) {
    parts.push(state.activeSeason)
  } else {
    parts.push('全部季节')
  }
  return parts.join(' · ')
}

const loadNextChunk = async () => {
  if (state.loading || state.done) {
    return
  }
  if (state.total != null && state.offset >= state.total) {
    state.done = true
    sentinel.textContent = '已加载全部'
    return
  }
  setLoading(true)
  try {
    const params = new URLSearchParams({
      offset: String(state.offset),
      size: String(state.pageSize),
    })
    if (state.activeSource) {
      params.set('source', state.activeSource)
    }
    if (state.activeColorSeries) {
      params.set('color_series', state.activeColorSeries)
    }
    if (state.activeSeason) {
      params.set('season', state.activeSeason)
    }
    const res = await fetch(`/api/outfits?${params.toString()}`, { cache: 'no-store' })
    if (!res.ok) {
      throw new Error(`/api/outfits ${res.status}`)
    }
    const payload = await res.json()
    const chunk = payload.outfits || []
    state.total = Number(payload.total) || 0
    for (const outfit of chunk) {
      listEl.appendChild(renderOutfit(outfit))
    }
    state.rendered += chunk.length
    state.offset += chunk.length
    titleMeta.textContent = `${buildFilterLabel()} · 已展示 ${state.rendered} / ${state.total} 套`
    if (!chunk.length || state.offset >= state.total) {
      state.done = true
      sentinel.textContent = '已加载全部'
    }
  } catch (e) {
    sentinel.textContent = `加载失败：${e.message}`
    console.error(e)
  } finally {
    setLoading(false)
  }
}

const io = new IntersectionObserver(
  (entries) => {
    for (const en of entries) {
      if (en.isIntersecting) {
        loadNextChunk()
      }
    }
  },
  {
    root: scrollRoot,
    rootMargin: '240px 0px',
    threshold: 0,
  },
)

const showError = (msg) => {
  const b = document.createElement('div')
  b.className = 'error-banner'
  b.textContent = msg
  document.body.insertBefore(b, sourceTabsEl)
}

;(async () => {
  try {
    titleMeta.textContent = '从 ES 加载搭配'
    await loadSources()
    await loadColorSeries()
    await loadSeasons()
    io.observe(sentinel)
    await loadNextChunk()
  } catch (e) {
    showError(String(e.message || e))
    titleMeta.textContent = '初始化失败'
  }
})()
