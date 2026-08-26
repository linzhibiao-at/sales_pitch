// 商品浏览页：左侧筛选 + 右侧 5 列商品网格，商品图链接到 detail.html?sku=
'use strict';

const PAGE_SIZE = 60; // 12 行 × 5 列

// 筛选维度配置：counted=true 的维度值来自 ES 聚合（带 count），其余来自固定枚举
const FILTERS = [
  { key: 'season', label: '季节' },
  { key: 'gender', label: '性别' },
  { key: 'age', label: '年龄' },
  { key: 'color_series', label: '色系' },
  { key: 'category_l2', label: '类目', counted: true },
  { key: 'series', label: '系列', counted: true },
];

const state = {};
FILTERS.forEach((f) => (state[f.key] = []));
state.up_time_since = '';
let loadedCount = 0;
let totalCount = 0;
let loading = false;

const $ = (id) => document.getElementById(id);

function pickSkuImage(sku) {
  // 镜像 web/app.js pickSkuImage：tryon_image → display_image → index_images[0]
  if (sku.tryon_image) return sku.tryon_image;
  if (sku.display_image) return sku.display_image;
  if (Array.isArray(sku.index_images) && sku.index_images.length) {
    return sku.index_images[0];
  }
  if (sku.index_image) return sku.index_image;
  return '';
}

function detailUrl(skuId) {
  const s = String(skuId || '').trim();
  if (!s) return '';
  // browse 页以 /products 入口提供，detail 仍由 /outfits-viewer 静态目录承载，
  // 故用绝对前缀（与 eval/review_detail.js 的 OUTFITS_VIEWER_BASE 约定一致）。
  return `/outfits-viewer/detail.html?sku=${encodeURIComponent(s)}`;
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function badgeList(sku) {
  const out = [];
  if (sku.role) out.push(esc(sku.role));
  if (sku.category_l2) out.push(esc(sku.category_l2));
  if (sku.series) out.push(esc(sku.series));
  if (Array.isArray(sku.color_series) && sku.color_series.length) {
    out.push(esc(sku.color_series.join('/')));
  }
  return out;
}

function renderCard(sku) {
  const img = pickSkuImage(sku);
  const href = detailUrl(sku.sku_id);
  const price = sku.price != null ? `¥${esc(sku.price)}` : '';
  const badges = badgeList(sku).map((b) => `<span>${b}</span>`).join('');
  const thumb = img
    ? `<img src="${esc(img)}" loading="lazy" alt="${esc(sku.title || '')}" />`
    : `<span class="ph">无图</span>`;
  return `
    <a class="item-card" href="${esc(href)}" target="_blank" rel="noopener">
      <div class="thumb">${thumb}</div>
      <div class="info">
        <div class="name">${esc(sku.title || sku.sku_id || '')}</div>
        ${price ? `<div class="price">${price}</div>` : ''}
        <div class="badges">${badges}</div>
        <div class="sku">${esc(sku.sku_id || '')}</div>
      </div>
    </a>`.trim();
}

function renderSidebar(facets) {
  const sidebar = $('filterSidebar');
  // 清掉旧的 filter-group（保留 h2 与重置按钮）
  sidebar.querySelectorAll('.filter-group').forEach((n) => n.remove());
  const resetBtn = $('resetBtn');

  // 上架时间下限（date）—— 单值输入，置于筛选组最上方
  const sinceGroup = document.createElement('div');
  sinceGroup.className = 'filter-group filter-group--since';
  sinceGroup.innerHTML = `
    <div class="group-title">上市时间 ≥</div>
    <input type="date" id="upTimeSince" data-key="up_time_since"
      class="filter-date" value="${esc(state.up_time_since || '')}" />`;
  sidebar.insertBefore(sinceGroup, resetBtn);

  FILTERS.forEach((f) => {
    const raw = facets[f.key] || [];
    const opts = f.counted
      ? raw.map((o) => ({ value: o.value, count: o.count }))
      : raw.map((v) => ({ value: v, count: null }));

    const group = document.createElement('div');
    group.className = 'filter-group';
    group.innerHTML = `<div class="group-title">${esc(f.label)}</div>`;
    const box = document.createElement('div');
    box.className = 'filter-options';
    opts.forEach((o) => {
      const id = `f-${f.key}-${String(o.value).replace(/[^a-z0-9一-龥]/gi, '')}`;
      const lbl = document.createElement('label');
      lbl.innerHTML = `<input type="checkbox" data-key="${esc(f.key)}" data-val="${esc(o.value)}" id="${id}" />
        <span>${esc(o.value)}</span>${o.count != null ? `<span class="count">${o.count}</span>` : ''}`;
      box.appendChild(lbl);
    });
    group.appendChild(box);
    sidebar.insertBefore(group, resetBtn);
  });

  sidebar.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const key = cb.dataset.key;
      const val = cb.dataset.val;
      const set = new Set(state[key]);
      if (cb.checked) set.add(val);
      else set.delete(val);
      state[key] = Array.from(set);
      resetAndFetch();
    });
  });

  const sinceInput = $('upTimeSince');
  if (sinceInput) {
    sinceInput.addEventListener('change', () => {
      state.up_time_since = (sinceInput.value || '').trim();
      resetAndFetch();
    });
  }
}

function buildParams(offset) {
  const params = new URLSearchParams();
  params.set('offset', String(offset));
  params.set('size', String(PAGE_SIZE));
  for (const [key, vals] of Object.entries(state)) {
    if (key === 'up_time_since') {
      if (state.up_time_since) params.set('up_time_since', state.up_time_since);
      continue;
    }
    for (const v of vals) params.append(key, v);
  }
  return params;
}

function updateMeta() {
  const active =
    FILTERS.reduce((n, f) => n + (state[f.key].length ? 1 : 0), 0) +
    (state.up_time_since ? 1 : 0);
  $('productMeta').textContent =
    totalCount > 0
      ? `共 ${totalCount} 个商品 · 已加载 ${loadedCount}（筛选维度 ${active}）`
      : active
        ? `共 0 个商品（当前筛选无匹配）`
        : '加载中…';
}

async function loadFacets() {
  try {
    const res = await fetch('/api/skus/facets', { cache: 'no-store' });
    const data = await res.json();
    renderSidebar(data);
  } catch (e) {
    $('productMeta').textContent = '筛选项加载失败：' + e.message;
  }
}

async function fetchPage(offset, append) {
  if (loading) return;
  loading = true;
  $('loadMoreBtn') && ($('loadMoreBtn').disabled = true);
  try {
    const params = buildParams(offset);
    const res = await fetch(`/api/skus?${params.toString()}`, { cache: 'no-store' });
    const data = await res.json();
    const skus = Array.isArray(data.skus) ? data.skus : [];
    totalCount = int(data.total);
    const grid = $('productGrid');
    if (!append) {
      grid.innerHTML = '';
      loadedCount = 0;
    }
    if (!skus.length && !append) {
      grid.innerHTML = '<div class="empty-hint">没有匹配的商品，试试调整筛选条件。</div>';
    } else {
      grid.insertAdjacentHTML('beforeend', skus.map(renderCard).join(''));
    }
    loadedCount += skus.length;
    updateMeta();

    const more = totalCount > loadedCount;
    $('loadMoreBar').style.display = more ? 'flex' : 'none';
    if (more) {
      $('loadMoreHint').textContent = `已加载 ${loadedCount} / ${totalCount}`;
    }
  } catch (e) {
    $('productMeta').textContent = '加载失败：' + e.message;
  } finally {
    loading = false;
    $('loadMoreBtn') && ($('loadMoreBtn').disabled = false);
  }
}

function int(v) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : 0;
}

function resetAndFetch() {
  loadedCount = 0;
  fetchPage(0, false);
}

function resetFilters() {
  FILTERS.forEach((f) => (state[f.key] = []));
  state.up_time_since = '';
  document
    .querySelectorAll('#filterSidebar input[type="checkbox"]')
    .forEach((cb) => (cb.checked = false));
  const sinceInput = $('upTimeSince');
  if (sinceInput) sinceInput.value = '';
  resetAndFetch();
}

document.addEventListener('DOMContentLoaded', () => {
  $('loadMoreBtn').addEventListener('click', () => fetchPage(loadedCount, true));
  $('resetBtn').addEventListener('click', resetFilters);
  loadFacets().then(() => fetchPage(0, false));
});
