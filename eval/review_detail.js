/** FILA 搭配评测 — 详情页 (review_detail.html) */

const $ = (id) => document.getElementById(id);

// ── 主题 ──
function toggleTheme() {
  const cur = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  const next = cur === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('fila_eval_theme', next);
}
$('btnTheme')?.addEventListener('click', toggleTheme);

// ── 工具 ──
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function pickSkuImage(row) {
  for (const k of ['tryon_image', 'display_image', 'index_image']) {
    const u = String(row[k] || '').trim();
    if (u) return u;
  }
  return '';
}

function pickOutfitHero(outfit) {
  for (const k of ['outfit_tryon_image', 'tryon_result_image', 'display_image', 'background_img', 'index_image']) {
    const u = String(outfit[k] || '').trim();
    if (u && (u.startsWith('http') || u.startsWith('data:'))) return u;
  }
  return '';
}

function formatRankNum(n) {
  const v = Number(n);
  if (Number.isNaN(v)) {
    return '—';
  }
  return v.toFixed(4);
}

function getOutfitRankInfo(outfit) {
  const breakdown = outfit.rank_score_breakdown || outfit._rank_breakdown;
  const total = outfit.rank_score ?? outfit._rank_score ?? breakdown?.total;
  const order = outfit.rank_order ?? outfit._rank_order;
  if (total == null && !(breakdown && breakdown.items && breakdown.items.length)) {
    return null;
  }
  return { total, order, breakdown };
}

function buildOutfitRankScoreBlock(outfit) {
  const info = getOutfitRankInfo(outfit);
  if (!info) {
    return null;
  }
  const { total, order, breakdown } = info;
  const wrap = document.createElement('div');
  wrap.className = 'outfit-rank-scores';

  const head = document.createElement('div');
  head.className = 'outfit-rank-head';
  const orderText = order != null ? `第 ${order} 名 · ` : '';
  const scoreTotal = breakdown?.total ?? total;
  head.textContent = `${orderText}排序总分 ${formatRankNum(scoreTotal)}`;
  wrap.appendChild(head);

  const items = breakdown?.items;
  if (Array.isArray(items) && items.length) {
    const list = document.createElement('ul');
    list.className = 'outfit-rank-items';
    items.forEach((it) => {
      const li = document.createElement('li');
      const label = it.label || it.key || '分项';
      const w = it.weight != null ? `×${it.weight}` : '';
      if (it.raw != null && it.weighted != null && it.raw !== it.weighted) {
        li.textContent = `${label}${w}：原始 ${formatRankNum(it.raw)} → 加权 ${formatRankNum(it.weighted)}`;
      } else {
        li.textContent = `${label}${w}：${formatRankNum(it.raw ?? it.weighted)}`;
      }
      if (it.brief) {
        const briefSpan = document.createElement('span');
        briefSpan.className = 'rank-item-brief';
        briefSpan.textContent = ` — ${it.brief}`;
        li.appendChild(briefSpan);
      }
      list.appendChild(li);
    });
    wrap.appendChild(list);
  }
  return wrap;
}

const OUTFITS_VIEWER_BASE =
  window.OUTFITS_VIEWER_BASE || `${window.location.origin}/outfits-viewer`;

function skuDetailUrl(skuId) {
  const id = String(skuId || '').trim();
  if (!id || id.startsWith('img_')) return '';
  return `${OUTFITS_VIEWER_BASE}/detail.html?sku=${encodeURIComponent(id)}`;
}

// ── 读取 URL 参数 ──
const params = new URLSearchParams(location.search);
const ts = params.get('ts') || '';
const dataFile = params.get('file') || '';

if (ts) {
  const backEl = $('backLink');
  if (backEl) backEl.href = `review.html?ts=${encodeURIComponent(ts)}`;
}

// 从文件名推导标题，如 top__polo.json → "top · polo"
function deriveLabel(file) {
  const name = file.replace(/\.json$/, '');
  const parts = name.split('__');
  return parts.join(' · ');
}
const label = deriveLabel(dataFile);

$('pageTitle').textContent = label || '搭配评测详情';
$('resultTitle').textContent = label || '搭配结果';
document.title = `${label || '详情'} — FILA 搭配评测`;

// ── 评审数据 ──
// key: `${input_sku_id}_${outfit_id}` → Array<review> (按时间倒序)
const reviewsByKey = new Map();
const ROLE_KEY = 'fila_eval_role';
const NAME_KEY = 'fila_eval_name';
let currentRole = localStorage.getItem(ROLE_KEY) || '';
let currentName = localStorage.getItem(NAME_KEY) || '';

function reviewKey(inputSkuId, outfitId) {
  return `${inputSkuId}_${outfitId}`;
}

function roleTagClass(role) {
  switch (role) {
    case '产品经理': return 'role-pm';
    case '设计师':   return 'role-designer';
    case '项目经理': return 'role-pm-proj';
    case '工程师':   return 'role-engineer';
    default:         return 'role-other';
  }
}

async function loadReviews() {
  if (!dataFile) return;
  const reviewFile = ts ? `${ts}/${dataFile}` : dataFile;
  try {
    const resp = await fetch(`/eval/api/reviews?file=${encodeURIComponent(reviewFile)}`);
    if (!resp.ok) return;
    const list = await resp.json();
    reviewsByKey.clear();
    // 后端按 id DESC 返回，相同 key 内保持插入顺序即为时间倒序
    for (const r of list || []) {
      const k = reviewKey(r.input_sku_id, r.outfit_id);
      if (!reviewsByKey.has(k)) reviewsByKey.set(k, []);
      reviewsByKey.get(k).push(r);
    }
  } catch (_) { /* ignore */ }
}

async function submitReview(inputSkuId, outfitId, rating, comment, statusEl, panelEl) {
  statusEl.textContent = '提交中...';
  statusEl.className = 'review-status';
  const reviewFile = ts ? `${ts}/${dataFile}` : dataFile;
  try {
    const resp = await fetch('/eval/api/reviews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        data_file: reviewFile,
        input_sku_id: inputSkuId,
        outfit_id: outfitId,
        rating: rating || null,
        comment: comment || null,
        reviewer_role: currentRole || null,
        reviewer_name: currentName || null,
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const saved = await resp.json();
    const k = reviewKey(inputSkuId, outfitId);
    if (!reviewsByKey.has(k)) reviewsByKey.set(k, []);
    reviewsByKey.get(k).unshift(saved);
    statusEl.textContent = `已保存 · ${new Date(saved.updated_at).toLocaleString('zh-CN')}`;
    statusEl.className = 'review-status review-status-ok';
    // 重新渲染该 outfit 的评审面板
    rerenderReviewPanel(panelEl, inputSkuId, outfitId);
  } catch (err) {
    statusEl.textContent = `提交失败: ${err.message}`;
    statusEl.className = 'review-status review-status-err';
  }
}

async function deleteReviewById(reviewId, panelEl, inputSkuId, outfitId) {
  try {
    const resp = await fetch(`/eval/api/reviews?id=${encodeURIComponent(reviewId)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const k = reviewKey(inputSkuId, outfitId);
    const arr = reviewsByKey.get(k) || [];
    const idx = arr.findIndex((r) => String(r.id) === String(reviewId));
    if (idx >= 0) arr.splice(idx, 1);
    if (arr.length === 0) reviewsByKey.delete(k);
    rerenderReviewPanel(panelEl, inputSkuId, outfitId);
  } catch (err) {
    alert(`删除失败: ${err.message}`);
  }
}

// ── 加载该中类的数据文件 ──
async function loadData() {
  if (!dataFile) {
    $('resultList').innerHTML = '<p class="empty-hint">缺少 file 参数</p>';
    return;
  }
  if (!currentRole) {
    const banner = document.createElement('div');
    banner.className = 'role-missing-banner';
    banner.innerHTML = '尚未选择评审角色，<a href="review.html">返回总览</a>选择角色后再评审。提交时评审意见将不带角色标签。';
    document.querySelector('main.layout').insertBefore(banner, $('resultTitle').parentNode);
  }
  try {
    const dataPath = ts ? `./results/${encodeURIComponent(ts)}/${dataFile}` : `./results/${dataFile}`;
    const [resp] = await Promise.all([
      fetch(dataPath),
      loadReviews(),
    ]);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const results = await resp.json();
    await hydrateOutfits(results);
    renderResults(results);
  } catch (err) {
    $('resultList').innerHTML = `<p class="empty-hint">加载失败: ${escapeHtml(err.message)}</p>`;
  }
}

async function hydrateOutfits(results) {
  const ids = [];
  for (const result of results || []) {
    if (Array.isArray(result.outfits) && result.outfits.length) {
      continue;
    }
    for (const oid of result.outfit_ids || []) {
      if (oid && !ids.includes(oid)) ids.push(oid);
    }
  }
  if (!ids.length) return;
  const resp = await fetch('/api/outfits/mget', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ outfit_ids: ids }),
  });
  if (!resp.ok) throw new Error(`/api/outfits/mget HTTP ${resp.status}`);
  const payload = await resp.json();
  const byId = new Map((payload.outfits || []).map((o) => [String(o.outfit_id || o.idMatch || ''), o]));
  for (const result of results || []) {
    if (Array.isArray(result.outfits) && result.outfits.length) {
      continue;
    }
    const metaById = new Map((result.outfit_meta || []).map((m) => [String(m.outfit_id || ''), m]));
    result.outfits = (result.outfit_ids || [])
      .map((oid) => {
        const meta = metaById.get(String(oid)) || {};
        const outfit = byId.get(String(oid)) || meta.snapshot;
        if (!outfit) return null;
        const { snapshot, ...overlay } = meta;
        return { ...outfit, ...overlay };
      })
      .filter(Boolean);
  }
}

// ── 渲染（分批，避免长时间阻塞） ──
function renderResults(results) {
  const container = $('resultList');
  if (!results.length) {
    container.innerHTML = '<p class="empty-hint">该分类下暂无评测结果</p>';
    return;
  }
  container.innerHTML = '';
  let idx = 0;
  function renderBatch() {
    const end = Math.min(idx + 2, results.length);
    while (idx < end) {
      container.appendChild(renderInputSkuSection(results[idx]));
      idx++;
    }
    if (idx < results.length) {
      requestAnimationFrame(renderBatch);
    }
  }
  renderBatch();
}

function renderInputSkuSection(result) {
  const section = document.createElement('div');
  section.className = 'input-sku-section';
  const sku = result.input_sku || {};

  // 输入 SKU 头
  const header = document.createElement('div');
  header.className = 'input-sku-header';

  const img = document.createElement('img');
  img.className = 'input-sku-img';
  img.src = sku.tryon_image || '';
  img.alt = sku.title || '';
  img.loading = 'lazy';
  img.referrerPolicy = 'no-referrer';
  header.appendChild(img);

  const info = document.createElement('div');
  info.className = 'input-sku-info';
  info.innerHTML = `
    <div class="input-sku-title">${escapeHtml(sku.title || '—')}</div>
    <div class="input-sku-meta">
      SKU: <code>${escapeHtml(result.input_sku_id)}</code>
      &nbsp;·&nbsp; 性别: <strong>${escapeHtml(sku.gender || '—')}</strong>
      &nbsp;·&nbsp; 角色: <strong>${escapeHtml(sku.role || '—')}</strong>
      &nbsp;·&nbsp; 价格: ¥${sku.price || '—'}
    </div>
  `;
  header.appendChild(info);
  section.appendChild(header);

  // 错误
  if (result.error) {
    const errEl = document.createElement('div');
    errEl.className = 'input-sku-error';
    errEl.textContent = `处理失败: ${result.error}`;
    section.appendChild(errEl);
    return section;
  }

  // 意图调试（默认折叠）
  const intentDebug = result.intent_debug;
  if (intentDebug && Object.keys(intentDebug).length) {
    section.appendChild(renderIntentDebug(intentDebug, result.intent));
  }

  // ES Query 调试（默认折叠）
  const esDebug = result.es_debug;
  if (esDebug && esDebug.length) {
    section.appendChild(renderEsDebug(esDebug));
  }

  // 搭配结果
  const outfits = result.outfits || [];
  const countLabel = document.createElement('div');
  countLabel.className = 'outfit-count-label';
  const totalMs = result.timings?.total_ms;
  countLabel.textContent = `推荐搭配 ${outfits.length} 套` +
    (totalMs ? ` · 耗时 ${totalMs < 1000 ? totalMs + 'ms' : (totalMs / 1000).toFixed(2) + 's'}` : '');
  section.appendChild(countLabel);

  if (!outfits.length) {
    const hint = document.createElement('p');
    hint.className = 'empty-hint';
    hint.textContent = '无搭配结果';
    section.appendChild(hint);
    return section;
  }

  const list = document.createElement('div');
  list.className = 'outfit-list';
  for (const o of outfits) {
    list.appendChild(renderOutfitRow(o, result.input_sku_id));
  }
  section.appendChild(list);
  return section;
}

function renderOutfitRow(outfit, inputSkuId) {
  const row = document.createElement('article');
  row.className = 'outfit-row';
  const items = outfit.items || [];
  const heroUrl = pickOutfitHero(outfit);

  // Hero 图
  const heroWrap = document.createElement('div');
  heroWrap.className = 'outfit-hero-wrap';
  const heroEl = document.createElement('div');
  heroEl.className = 'outfit-hero';
  if (heroUrl) {
    const img = document.createElement('img');
    img.src = heroUrl;
    img.alt = outfit.name || outfit.outfit_id || '搭配';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    heroEl.appendChild(img);
  } else {
    const ph = document.createElement('div');
    ph.className = 'placeholder';
    ph.textContent = '暂无穿搭图';
    heroEl.appendChild(ph);
  }
  heroWrap.appendChild(heroEl);

  // 主体
  const main = document.createElement('div');
  main.className = 'outfit-main';

  const title = document.createElement('h2');
  title.className = 'outfit-title';
  title.textContent = outfit.name || outfit.outfit_id || '搭配';

  const sub = document.createElement('div');
  sub.className = 'outfit-sub';
  const meta = [];
  if (outfit.outfit_id) meta.push(`ID: ${outfit.outfit_id}`);
  if (outfit.recall_source_label || outfit.recall_source) {
    meta.push(outfit.recall_source_label || outfit.recall_source);
  }
  if (outfit.is_synthetic) meta.push('拼套');
  if (outfit.price_total != null) meta.push(`总价 ¥${outfit.price_total}`);
  sub.textContent = meta.join(' · ');

  main.appendChild(title);
  main.appendChild(sub);

  if (outfit.reason) {
    const reason = document.createElement('p');
    reason.className = 'outfit-reason-block';
    reason.textContent = outfit.reason;
    main.appendChild(reason);
  }

  const rankBlock = buildOutfitRankScoreBlock(outfit);
  if (rankBlock) {
    const details = document.createElement('details');
    details.className = 'outfit-rank-details';
    const summary = document.createElement('summary');
    summary.textContent = '排序得分';
    details.appendChild(summary);
    details.appendChild(rankBlock);
    main.appendChild(details);
  }

  // 单品网格
  const grid = document.createElement('div');
  grid.className = 'items-grid';
  for (const it of items) {
    const card = document.createElement('div');
    card.className = `item-card${it.is_master ? ' master' : ''}`;

    const thumb = document.createElement('div');
    thumb.className = 'thumb';
    const thumbUrl = pickSkuImage(it);
    const detailHref = skuDetailUrl(it.sku_id);
    if (thumbUrl) {
      const im = document.createElement('img');
      im.src = thumbUrl;
      im.alt = it.title || it.sku_id || '';
      im.loading = 'lazy';
      im.referrerPolicy = 'no-referrer';
      if (detailHref) {
        const a = document.createElement('a');
        a.href = detailHref;
        a.target = '_blank';
        a.appendChild(im);
        thumb.appendChild(a);
      } else {
        thumb.appendChild(im);
      }
    } else {
      const empty = document.createElement('span');
      empty.className = 'thumb-empty';
      empty.textContent = '无图';
      thumb.appendChild(empty);
    }

    const info = document.createElement('div');
    info.className = 'info';
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = it.title || '—';
    const sku = document.createElement('div');
    sku.className = 'sku';
    sku.textContent = it.sku_id || '';
    info.appendChild(name);
    info.appendChild(sku);
    if (it.reason) {
      const itemReason = document.createElement('div');
      itemReason.className = 'item-reason-line';
      itemReason.textContent = it.reason;
      info.appendChild(itemReason);
    }

    card.appendChild(thumb);
    card.appendChild(info);
    grid.appendChild(card);
  }
  main.appendChild(grid);

  // ── 评审面板 ──
  const outfitId = outfit.outfit_id || '';
  const reviewPanel = document.createElement('div');
  reviewPanel.className = 'review-panel';
  reviewPanel.dataset.outfitId = outfitId;
  reviewPanel.dataset.inputSkuId = inputSkuId;

  renderReviewPanelContent(reviewPanel, inputSkuId, outfitId);

  row.appendChild(heroWrap);
  row.appendChild(main);
  row.appendChild(reviewPanel);
  return row;
}

function renderReviewPanelContent(panelEl, inputSkuId, outfitId) {
  panelEl.innerHTML = '';

  const k = reviewKey(inputSkuId, outfitId);
  const allReviews = reviewsByKey.get(k) || [];
  // 每个人只能看到自己的评审：按 角色 + 姓名 匹配
  const myReviews = allReviews.filter((r) => isOwnReview(r));
  const hasOwnHistory = myReviews.length > 0;

  // 标题 + 当前角色徽章
  const titleRow = document.createElement('div');
  titleRow.className = 'review-panel-title-row';
  const reviewTitle = document.createElement('div');
  reviewTitle.className = 'review-panel-title';
  reviewTitle.textContent = '评审';
  titleRow.appendChild(reviewTitle);
  if (currentRole) {
    const badge = document.createElement('span');
    badge.className = `role-tag ${roleTagClass(currentRole)}`;
    badge.textContent = currentName ? `${currentRole} · ${currentName}` : currentRole;
    titleRow.appendChild(badge);
  }
  panelEl.appendChild(titleRow);

  // ── 当前角色编辑区（始终新建一条） ──
  const editorCard = document.createElement('div');
  editorCard.className = 'review-editor';

  // 星级评分
  const starsWrap = document.createElement('div');
  starsWrap.className = 'review-stars';
  let selectedRating = 0;

  const starEls = [];
  for (let i = 1; i <= 5; i++) {
    const star = document.createElement('span');
    star.className = 'review-star';
    star.textContent = '\u2605';
    star.dataset.value = i;
    star.addEventListener('click', () => {
      selectedRating = i;
      starEls.forEach((s, idx) => {
        s.className = 'review-star' + (idx < i ? ' active' : '');
      });
    });
    star.addEventListener('mouseenter', () => {
      starEls.forEach((s, idx) => {
        s.classList.toggle('hover', idx < i);
      });
    });
    starEls.push(star);
    starsWrap.appendChild(star);
  }
  starsWrap.addEventListener('mouseleave', () => {
    starEls.forEach((s) => s.classList.remove('hover'));
  });
  editorCard.appendChild(starsWrap);

  // 评语文本框
  const textarea = document.createElement('textarea');
  textarea.className = 'review-comment';
  textarea.placeholder = '输入评审意见...';
  textarea.rows = 3;
  editorCard.appendChild(textarea);

  // 状态提示
  const statusEl = document.createElement('div');
  statusEl.className = 'review-status';

  // 提交按钮
  const submitBtn = document.createElement('button');
  submitBtn.className = 'review-submit-btn';
  submitBtn.textContent = hasOwnHistory ? '追加评审' : '提交评审';
  submitBtn.addEventListener('click', () => {
    submitReview(inputSkuId, outfitId, selectedRating, textarea.value, statusEl, panelEl);
  });

  const actions = document.createElement('div');
  actions.className = 'review-actions';
  actions.appendChild(submitBtn);
  actions.appendChild(statusEl);
  editorCard.appendChild(actions);
  panelEl.appendChild(editorCard);

  // ── 历史评审列表（只读，仅当前用户自己的，按时间倒序） ──
  if (myReviews.length) {
    const historyWrap = document.createElement('details');
    historyWrap.className = 'review-history';
    historyWrap.open = true;
    const summary = document.createElement('summary');
    summary.textContent = `我的历史评审 (${myReviews.length})`;
    historyWrap.appendChild(summary);

    const list = document.createElement('div');
    list.className = 'review-history-list';
    for (const r of myReviews) {
      list.appendChild(renderHistoryItem(r, panelEl, inputSkuId, outfitId));
    }
    historyWrap.appendChild(list);
    panelEl.appendChild(historyWrap);
  }
}

function isOwnReview(r) {
  if (!currentRole) return false;
  if ((r.reviewer_role || '') !== currentRole) return false;
  // 姓名匹配：若当前用户填了姓名，必须一致；若未填，则只匹配同样未填姓名的
  if (currentName) {
    return (r.reviewer_name || '') === currentName;
  }
  return !(r.reviewer_name || '');
}

function renderHistoryItem(r, panelEl, inputSkuId, outfitId) {
  const item = document.createElement('div');
  item.className = 'review-history-item';

  const head = document.createElement('div');
  head.className = 'review-history-head';
  const role = r.reviewer_role || '';
  const tag = document.createElement('span');
  tag.className = `role-tag ${roleTagClass(role)}`;
  tag.textContent = role || (r.reviewer ? r.reviewer : '未标注');
  head.appendChild(tag);
  if (r.reviewer_name) {
    const nameSpan = document.createElement('span');
    nameSpan.className = 'review-history-name';
    nameSpan.textContent = r.reviewer_name;
    head.appendChild(nameSpan);
  }
  if (r.rating) {
    const stars = document.createElement('span');
    stars.className = 'review-history-stars';
    stars.textContent = '\u2605'.repeat(r.rating);
    head.appendChild(stars);
  }
  const time = document.createElement('span');
  time.className = 'review-history-time';
  time.textContent = new Date(r.updated_at || r.created_at).toLocaleString('zh-CN');
  head.appendChild(time);

  // 该条为本人的评审（列表已按 isOwnReview 过滤），允许删除
  if (r.id != null) {
    const delBtn = document.createElement('button');
    delBtn.className = 'review-history-del';
    delBtn.textContent = '删除';
    delBtn.title = '删除自己提交的这条评审';
    delBtn.addEventListener('click', () => {
      if (confirm('删除这条评审？')) deleteReviewById(r.id, panelEl, inputSkuId, outfitId);
    });
    head.appendChild(delBtn);
  }
  item.appendChild(head);

  if (r.comment) {
    const c = document.createElement('div');
    c.className = 'review-history-comment';
    c.textContent = r.comment;
    item.appendChild(c);
  }
  return item;
}

function rerenderReviewPanel(panelEl, inputSkuId, outfitId) {
  renderReviewPanelContent(panelEl, inputSkuId, outfitId);
}

// ── 意图调试面板 ──
// target_slots 的 values 是 {role: {positive:{slot:val|vals}, negative:{slot:[vals]}}}
// 这种 dict 结构，渲染成可读的「正/否」chip 文本。
function formatTargetSlots(obj) {
  const roles = Object.keys(obj || {});
  if (!roles.length) return '—';
  const fmtSlotChips = (slotMap) => Object.entries(slotMap || {}).map(([k, v]) => {
    const vals = Array.isArray(v) ? v.join(',') : String(v ?? '');
    return `${escapeHtml(k)}=${escapeHtml(vals)}`;
  }).join(' ');
  const parts = roles.map((role) => {
    const pn = obj[role] || {};
    const roleLabel = role === '*' ? '全局(*)' : escapeHtml(role);
    const pos = fmtSlotChips(pn.positive);
    const neg = fmtSlotChips(pn.negative);
    const segs = [];
    if (pos) segs.push(`正:${pos}`);
    if (neg) segs.push(`否:${neg}`);
    return `[${roleLabel} ${segs.join(' ') || '—'}]`;
  });
  return parts.join(' ');
}

function renderIntentDebug(debug, intent) {
  const details = document.createElement('details');
  details.className = 'debug-panel';
  const summary = document.createElement('summary');
  summary.textContent = '意图调试';
  details.appendChild(summary);

  const content = document.createElement('div');
  content.className = 'debug-content';

  const method = debug.method || '—';
  const confidence = debug.confidence != null ? Number(debug.confidence).toFixed(4) : '—';
  const llmFallback = debug.llm_fallback ? '是' : '否';
  const imageOverride = debug.image_override ? '是' : '否';
  const overrideSlots = (debug.image_override_slots || []).join(', ') || '无';

  let html = `
    <div class="debug-meta">
      <span>提取方法: <strong>${escapeHtml(method)}</strong></span>
      <span>置信度: <strong>${confidence}</strong></span>
      <span>LLM Fallback: <strong>${llmFallback}</strong></span>
      <span>图覆盖: <strong>${imageOverride}</strong></span>
      <span>覆盖字段: <strong>${escapeHtml(overrideSlots)}</strong></span>
    </div>
  `;

  // slots detail 表格
  const slotsDetail = debug.slots_detail;
  if (slotsDetail && typeof slotsDetail === 'object' && Object.keys(slotsDetail).length) {
    html += `<table class="debug-table"><thead><tr>
      <th>Slot</th><th>值</th><th>来源</th><th>词典命中</th>
    </tr></thead><tbody>`;
    for (const [slot, info] of Object.entries(slotsDetail)) {
      // target_slots 的 values 是 {role: {positive:{slot:val}, negative:{slot:[vals]}}}
      // 结构（dict 而非 array），不能用 .join；其它 slot 的 values 应为 array。
      let values;
      if (slot === 'target_slots') {
        values = formatTargetSlots(info.values);
      } else {
        const raw = info.values;
        const joined = Array.isArray(raw)
          ? raw.map((v) => escapeHtml(String(v))).join(', ')
          : (raw == null ? '' : escapeHtml(String(raw)));
        values = joined || '—';
      }
      const source = info.source || '—';
      const hits = (info.dict_hits || []).join(', ') || '—';
      html += `<tr>
        <td>${escapeHtml(slot)}</td>
        <td>${values}</td>
        <td>${escapeHtml(source)}</td>
        <td>${escapeHtml(hits)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  }

  // intent 原始数据
  if (intent && Object.keys(intent).length) {
    html += `<details class="debug-raw"><summary>Intent JSON</summary><pre>${escapeHtml(JSON.stringify(intent, null, 2))}</pre></details>`;
  }

  content.innerHTML = html;
  details.appendChild(content);
  return details;
}

// ── ES Query 调试面板 ──
function renderEsDebug(queries) {
  const details = document.createElement('details');
  details.className = 'debug-panel';
  const summary = document.createElement('summary');
  summary.textContent = `ES Query 调试 (${queries.length})`;
  details.appendChild(summary);

  const content = document.createElement('div');
  content.className = 'debug-content';

  let html = '';
  queries.forEach((q) => {
    const role = escapeHtml(q.role || '—');
    const source = escapeHtml(q.source || '—');
    const hits = q.hits != null ? q.hits : '—';
    const esQuery = q.es_query;
    html += `
      <div class="es-query-block">
        <div class="es-query-header">
          <span>Target Role: <strong>${role}</strong></span>
          <span>来源: <strong>${source}</strong></span>
          <span>命中: <strong>${hits}</strong></span>
        </div>
        ${esQuery ? `<pre class="es-query-json">${escapeHtml(JSON.stringify(esQuery, null, 2))}</pre>` : ''}
      </div>
    `;
  });

  content.innerHTML = html;
  details.appendChild(content);
  return details;
}

loadData();
