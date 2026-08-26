const API_BASE = '';

/** 微导购穿搭展示（outfits-viewer）根路径，可通过 window.OUTFITS_VIEWER_BASE 覆盖 */
const OUTFITS_VIEWER_BASE =
  window.OUTFITS_VIEWER_BASE || `${window.location.origin}/outfits-viewer`;

/** 搭配 ID 显示前缀标签，可通过 window.OUTFIT_ID_LABEL 覆盖，如 '搭配编号：' */
const OUTFIT_ID_LABEL =
  window.OUTFIT_ID_LABEL != null ? String(window.OUTFIT_ID_LABEL) : 'ID：';

const PIPELINE = [
  { key: 'session_id', title: '会话' },
  { key: 'intent', title: '意图解析' },
  { key: 'recall', title: '搭配召回' },
  { key: 'coarse_rank', title: '粗排' },
  { key: 'ranking_reason', title: 'LLM排序和理由' },
  { key: 'tryon', title: '虚拟试穿' },
  { key: 'done', title: '完成' },
];

const $ = (id) => document.getElementById(id);

/** 不依赖 randomUUID（HTTP 等非安全上下文不可用） */
function newSessionId() {
  const c = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (c && typeof c.getRandomValues === 'function') {
    const bytes = new Uint8Array(16);
    c.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.slice(
    0,
    32,
  );
}

let sessionId = newSessionId();
let events = [];
let imageBase64 = null;
let tryonPersonImage = null;
/** 与 config.yaml recommend.show_outfit_rank_scores 一致，默认 false */
let showOutfitRankScores = false;
/** UI 模式：debug（完整调试台）| presentation（对外展示） */
let uiMode = 'debug';

/** 召回分支进度状态 */
let recallBranches = {};
/** recall_done 下发的 per-role 召回/去重计数（global 模式） */
let recallRoles = {};
/** 各路召回的 per-role 商品数：由 roles[role].channels 转置为 {path: {role: count}}（global 模式） */
let recallPathRoles = {};

const ROLE_ORDER = ['top', 'bottoms', 'dress', 'shoes', 'accessory'];
function roleSortKey(r) {
  const i = ROLE_ORDER.indexOf(r);
  return i < 0 ? 999 : i;
}
function sortedRoles(obj) {
  return Object.keys(obj || {}).sort((a, b) => roleSortKey(a) - roleSortKey(b));
}

function setStatus(text, cls = '') {
  const el = $('statusLine');
  el.textContent = text;
  el.className = `status-line ${cls}`.trim();
}

function formatMs(ms) {
  if (ms == null || Number.isNaN(ms)) {
    return '—';
  }
  const n = Number(ms);
  if (n < 1000) {
    return `${n}ms`;
  }
  return `${(n / 1000).toFixed(2)}s`;
}

function showPipelineTotal(totalMs) {
  const el = $('pipelineTotal');
  if (!el) {
    return;
  }
  el.classList.remove('hidden');
  el.textContent = `总耗时 ${formatMs(totalMs)}`;
}

function initPipeline() {
  const ol = $('pipelineSteps');
  ol.innerHTML = '';
  const totalEl = $('pipelineTotal');
  if (totalEl) {
    totalEl.classList.add('hidden');
    totalEl.textContent = '总耗时 —';
  }
  recallBranches = {};
  recallRoles = {};
  recallPathRoles = {};
  PIPELINE.forEach((step) => {
    const li = document.createElement('li');
    li.dataset.key = step.key;
    li.innerHTML = `
      <div class="step-title">${step.title}</div>
      <div class="step-time">—</div>
      <div class="step-detail">等待</div>
    `;
    ol.appendChild(li);
  });
}

function markStep(key, detail, state = 'done', elapsedMs = null) {
  const li = document.querySelector(`#pipelineSteps li[data-key="${key}"]`);
  if (!li) {
    return;
  }
  li.className = state;
  li.querySelector('.step-detail').textContent = detail;
  const timeEl = li.querySelector('.step-time');
  if (timeEl && elapsedMs != null) {
    timeEl.textContent = formatMs(elapsedMs);
  }
  const payload = events.find((e) => e.type === key);
  let old = li.querySelector('details');
  if (old) {
    old.remove();
  }
  if (payload && key !== 'text') {
    const det = document.createElement('details');
    det.innerHTML = `<summary>JSON</summary><pre>${escapeHtml(
      JSON.stringify(payload, null, 2),
    )}</pre>`;
    li.appendChild(det);
  }
}

function escapeHtml(s) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function pickSkuImage(row) {
  const tryon = String(row.tryon_image || '').trim();
  if (tryon) {
    return tryon;
  }
  for (const k of ['display_image', 'index_image']) {
    const u = String(row[k] || '').trim();
    if (u) {
      return u;
    }
  }
  return '';
}

function pickOutfitImage(o) {
  for (const k of ['display_image', 'tryon_image', 'index_image']) {
    const u = String(o[k] || '').trim();
    if (u) {
      return u;
    }
  }
  return '';
}

/** wear_match 的 idMatch：优先 id_match / idMatch，否则从 outfit_id wear_8945 取后缀；始终去掉 wear_ 前缀 */
function resolveOutfitIdMatch(outfit) {
  if (outfit == null) {
    return '';
  }
  let val = '';
  if (outfit.id_match != null && outfit.id_match !== '') {
    val = String(outfit.id_match).trim();
  } else if (outfit.idMatch != null && outfit.idMatch !== '') {
    val = String(outfit.idMatch).trim();
  } else {
    val = String(outfit.outfit_id || '').trim();
  }
  // 去掉 wear_ 前缀，只保留数字 ID
  if (val.startsWith('wear_')) {
    val = val.slice(5);
  }
  return val;
}

function outfitViewerUrl(outfit) {
  const oid = String(outfit.outfit_id || '');
  const idMatch = resolveOutfitIdMatch(outfit);
  // 合成搭配：用原固定搭配 id_match 生成链接
  if (oid.startsWith('synth_') || outfit.is_synthetic) {
    if (!idMatch) return '';
    return `${OUTFITS_VIEWER_BASE}/outfit.html?idMatch=${encodeURIComponent(idMatch)}`;
  }
  if (!idMatch) {
    return '';
  }
  return `${OUTFITS_VIEWER_BASE}/outfit.html?idMatch=${encodeURIComponent(idMatch)}`;
}

function skuViewerUrl(skuId) {
  const sku = String(skuId || '').trim();
  if (!sku || sku.startsWith('img_')) return '';
  return `${OUTFITS_VIEWER_BASE}/detail.html?sku=${encodeURIComponent(sku)}`;
}

function linkedImage(url, href, extraClass = '') {
  if (!url) {
    return '';
  }
  const cls = extraClass ? ` class="${extraClass}"` : '';
  const img = `<img${cls} src="${escapeAttr(url)}" alt="" loading="lazy" referrerpolicy="no-referrer" />`;
  if (!href) {
    return img;
  }
  return `<a href="${escapeAttr(href)}" target="_blank" rel="noopener noreferrer" class="img-link">${img}</a>`;
}

function pickOutfitHero(outfit) {
  const tryonResult = String(outfit.outfit_tryon_image || '').trim();
  if (tryonResult) {
    return tryonResult;
  }
  const bg = String(outfit.background_img || '').trim();
  if (bg.startsWith('http')) {
    return bg;
  }
  return pickOutfitImage(outfit);
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

/** 总价同一行后的排序摘要（总分 + 名次） */
function formatOutfitRankMetaSuffix(info) {
  if (!info || info.total == null) {
    return '';
  }
  const orderPart = info.order != null ? `（第${info.order}名）` : '';
  return ` · 排序总分 ${formatRankNum(info.total)}${orderPart}`;
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
      // 当 raw 和 weighted 相同时（如 weight=1），只显示一次分数
      if (it.raw != null && it.weighted != null && it.raw !== it.weighted) {
        li.textContent = `${label}${w}：原始 ${formatRankNum(it.raw)} → 加权 ${formatRankNum(it.weighted)}`;
      } else {
        li.textContent = `${label}${w}：${formatRankNum(it.raw ?? it.weighted)}`;
      }
      // 显示 brief（LLM 打分理由）
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

function renderOutfitRow(outfit) {
  const row = document.createElement('article');
  row.className = 'outfit-row';
  const idMatch = resolveOutfitIdMatch(outfit);
  if (idMatch) {
    row.dataset.idMatch = idMatch;
  }

  const items = outfit.items || [];
  const heroUrl = pickOutfitHero(outfit);
  const outfitHref = outfitViewerUrl(outfit);

  const heroWrap = document.createElement('div');
  heroWrap.className = 'outfit-hero-wrap';

  let heroEl;
  if (outfitHref) {
    heroEl = document.createElement('a');
    heroEl.href = outfitHref;
    heroEl.target = '_blank';
    heroEl.rel = 'noopener noreferrer';
  } else {
    heroEl = document.createElement('div');
  }
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


  const main = document.createElement('div');
  main.className = 'outfit-main';

  const title = document.createElement('h2');
  title.className = 'outfit-title';
  if (outfitHref) {
    const link = document.createElement('a');
    link.href = outfitHref;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = outfit.name || outfit.outfit_id || '搭配';
    title.appendChild(link);
  } else {
    title.textContent = outfit.name || outfit.outfit_id || '搭配';
  }

  const sub = document.createElement('div');
  sub.className = 'outfit-sub';
  const metaParts = [];
  const oid = String(outfit.outfit_id || '').trim();
  const isSynth = oid.startsWith('synth_') || outfit.is_synthetic;
  // outfit_id 始终显示
  if (oid) {
    const idSpan = document.createElement('span');
    idSpan.className = 'outfit-id-label';
    idSpan.textContent = 'ID：';
    sub.appendChild(idSpan);
    // 非合成搭配：outfit_id 本身可点击
    if (!isSynth && outfitHref) {
      const idLink = document.createElement('a');
      idLink.href = outfitHref;
      idLink.target = '_blank';
      idLink.rel = 'noopener noreferrer';
      idLink.className = 'outfit-id-link';
      idLink.textContent = oid;
      sub.appendChild(idLink);
    } else {
      const idVal = document.createElement('span');
      idVal.textContent = oid;
      sub.appendChild(idVal);
    }
    // 合成搭配：追加原固定搭配 outfit_id（wear_XXXX 格式，可点击）
    if (isSynth && idMatch && idMatch !== oid) {
      const origOid = idMatch.startsWith('wear_') ? idMatch : `wear_${idMatch}`;
      const arrow = document.createElement('span');
      arrow.className = 'outfit-id-arrow';
      arrow.textContent = ' → ';
      sub.appendChild(arrow);
      if (outfitHref) {
        const origLink = document.createElement('a');
        origLink.href = outfitHref;
        origLink.target = '_blank';
        origLink.rel = 'noopener noreferrer';
        origLink.className = 'outfit-id-link';
        origLink.textContent = origOid;
        sub.appendChild(origLink);
      } else {
        const origVal = document.createElement('span');
        origVal.textContent = origOid;
        sub.appendChild(origVal);
      }
    }
  }
  const recallLabel = formatRecallSource(outfit);
  if (recallLabel) {
    metaParts.push(recallLabel);
  }
  if (outfit.is_synthetic) {
    metaParts.push('拼套');
  }
  const rankInfo = showOutfitRankScores ? getOutfitRankInfo(outfit) : null;
  if (outfit.price_total != null) {
    let priceLine = `总价 ¥${outfit.price_total}`;
    if (rankInfo) {
      priceLine += formatOutfitRankMetaSuffix(rankInfo);
    }
    metaParts.push(priceLine);
  } else if (rankInfo) {
    metaParts.push(formatOutfitRankMetaSuffix(rankInfo).replace(/^ · /, ''));
  }
  if (metaParts.length) {
    const sep = document.createElement('span');
    sep.className = 'outfit-meta-sep';
    sep.textContent = ' · ';
    if (oid) sub.appendChild(sep);
    const rest = document.createElement('span');
    rest.textContent = metaParts.join(' · ');
    sub.appendChild(rest);
  }

  main.appendChild(title);
  main.appendChild(sub);

  if (showOutfitRankScores) {
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
  }

  if (outfit.reason) {
    const reasonWrap = document.createElement('div');
    reasonWrap.className = 'outfit-reason-wrap';
    const reason = document.createElement('p');
    reason.className = 'outfit-reason-block';
    reason.textContent = outfit.reason;
    reasonWrap.appendChild(reason);

    const regenBtn = document.createElement('button');
    regenBtn.type = 'button';
    regenBtn.className = 'btn-regen-reason';
    regenBtn.textContent = '重新生成';
    regenBtn.addEventListener('click', () => {
      regenerateReason(outfit.outfit_id, reason, regenBtn, row);
    });
    reasonWrap.appendChild(regenBtn);
    main.appendChild(reasonWrap);
  } else {
    // 没有 reason 时也提供生成按钮
    const reasonWrap = document.createElement('div');
    reasonWrap.className = 'outfit-reason-wrap';
    const reason = document.createElement('p');
    reason.className = 'outfit-reason-block';
    reason.textContent = '';
    reasonWrap.appendChild(reason);

    const regenBtn = document.createElement('button');
    regenBtn.type = 'button';
    regenBtn.className = 'btn-regen-reason';
    regenBtn.textContent = '生成理由';
    regenBtn.addEventListener('click', () => {
      regenerateReason(outfit.outfit_id, reason, regenBtn, row);
    });
    reasonWrap.appendChild(regenBtn);
    main.appendChild(reasonWrap);
  }

  const grid = document.createElement('div');
  grid.className = 'items-grid';
  items.forEach((it) => {
    const skuHref = skuViewerUrl(it.sku_id);
    let card;
    if (skuHref) {
      card = document.createElement('a');
      card.href = skuHref;
      card.target = '_blank';
      card.rel = 'noopener noreferrer';
      card.className = `item-card item-card-link${it.is_master ? ' master' : ''}`;
    } else {
      card = document.createElement('div');
      card.className = `item-card${it.is_master ? ' master' : ''}`;
    }

    const thumb = document.createElement('div');
    thumb.className = 'thumb';
    const thumbUrl = pickSkuImage(it);
    if (thumbUrl) {
      const im = document.createElement('img');
      im.src = thumbUrl;
      im.alt = it.title || it.sku_id || '';
      im.loading = 'lazy';
      im.referrerPolicy = 'no-referrer';
      thumb.appendChild(im);
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
  });
  main.appendChild(grid);

  row.appendChild(heroWrap);
  row.appendChild(main);
  return row;
}

async function regenerateReason(outfitId, reasonEl, btn, rowEl) {
  if (!outfitId) return;
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const resp = await fetch(`${API_BASE}/regenerate-reason`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ outfit_id: outfitId }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    if (data.reason) {
      reasonEl.textContent = data.reason;
    }
    // 更新 item 级别的 reason
    const itemReasons = data.item_reasons || {};
    if (Object.keys(itemReasons).length && rowEl) {
      rowEl.querySelectorAll('.item-card, .item-card-link').forEach((card) => {
        const skuEl = card.querySelector('.sku');
        if (!skuEl) return;
        const skuId = skuEl.textContent.trim();
        if (skuId && itemReasons[skuId]) {
          let itemReasonEl = card.querySelector('.item-reason-line');
          if (!itemReasonEl) {
            itemReasonEl = document.createElement('div');
            itemReasonEl.className = 'item-reason-line';
            card.querySelector('.info')?.appendChild(itemReasonEl);
          }
          itemReasonEl.textContent = itemReasons[skuId];
        }
      });
    }
    btn.textContent = '重新生成';
  } catch (err) {
    btn.textContent = '失败';
    setTimeout(() => { btn.textContent = origText; }, 2000);
  } finally {
    btn.disabled = false;
  }
}

function renderOutfits(outfits) {
  const list = $('outfitGrid');
  if (!outfits || !outfits.length) {
    list.innerHTML = '<p class="empty-hint">暂无搭配结果</p>';
    return;
  }
  list.innerHTML = '';
  outfits.forEach((o) => {
    list.appendChild(renderOutfitRow(o));
  });
}

function escapeAttr(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;');
}

/* ── 意图调试面板 ── */

const RECALL_PATH_LABELS = {
  image_vector: '相似固定搭配',
  text_vector: '文本向量',
  query2es: 'Query2ES',
  complementary_model: '互补模型',
};

const ROLE_LABELS = {
  top: '上装',
  bottoms: '下装',
  dress: '连衣裙',
  shoes: '鞋',
  accessory: '配饰',
};

function roleLabel(role) {
  return ROLE_LABELS[role] || role || '?';
}

const RECALL_SOURCE_LABELS = {
  anchor_graph: '相似固定搭配',
  OUTFIT_ANCHOR_GRAPH: '相似固定搭配',
  image_vector: '相似固定搭配',
  text_vector_compose: '文本向量',
  OUTFIT_TEXT_VECTOR_COMPOSE: '文本向量',
  query2es_compose: 'Query2ES',
  OUTFIT_QUERY2ES_COMPOSE: 'Query2ES',
  complementary_model_compose: '互补模型',
  OUTFIT_COMPLEMENTARY_MODEL: '互补模型',
};

function formatRecallSource(outfit) {
  if (!outfit) return '';
  if (outfit.recall_source_label) {
    return String(outfit.recall_source_label);
  }
  const src = String(outfit.recall_source || '').trim();
  if (!src) return '';
  return RECALL_SOURCE_LABELS[src] || src;
}

function renderIntentDebug(ev) {
  const panel = $('intentDebugPanel');
  if (!panel) return;
  panel.classList.remove('hidden');

  const method = ev.method || '—';
  const confidence = ev.confidence != null ? Number(ev.confidence).toFixed(4) : '—';
  const imageOverride = ev.image_override ? '是' : '否';
  const overrideSlots = (ev.image_override_slots || []).join(', ') || '无';

  let html = `
    <div class="intent-debug-meta">
      <span>提取方法: <strong>${escapeHtml(method)}</strong></span>
      <span>置信度: <strong>${confidence}</strong></span>
      <span>图覆盖: <strong>${imageOverride}</strong></span>
      <span>覆盖字段: <strong>${escapeHtml(overrideSlots)}</strong></span>
    </div>
  `;

  // 锚点结构化属性（意图模块融合产出，召回统一消费）
  const anchorAttrs = ev.anchor_attrs;
  if (anchorAttrs && typeof anchorAttrs === 'object') {
    const attrLabels = { length_class: '长度', coverage: '覆盖', scene_domain: '场景' };
    const attrSpans = ['length_class', 'coverage', 'scene_domain']
      .map((k) => {
        const v = anchorAttrs[k];
        const label = attrLabels[k] || k;
        const shown = v != null && v !== '' ? escapeHtml(String(v)) : '—';
        return `<span class="anchor-attr"><em>${label}</em> <strong>${shown}</strong></span>`;
      })
      .join(' ');
    html += `<div class="anchor-attrs-summary">锚点属性: ${attrSpans}</div>`;
  }

  const slotsDetail = ev.slots_detail;
  if (slotsDetail && typeof slotsDetail === 'object') {
    // target_slots：{role: {positive:{slot:val}, negative:{slot:[vals]}}}
    const renderTargetSlots = (obj) => {
      const roles = Object.keys(obj || {});
      if (!roles.length) return '<span class="slot-empty">—</span>';
      const renderSlotChips = (slotMap, cls) => Object.entries(slotMap || {}).map(([k, v]) => {
        const vals = Array.isArray(v) ? v.join(',') : String(v ?? '');
        return `<span class="per-role-slot ${cls}"><em>${escapeHtml(k)}</em>=${escapeHtml(vals)}</span>`;
      }).join(' ');
      return roles.map((role) => {
        const pn = obj[role] || {};
        const roleLabel = role === '*' ? '全局(*)' : escapeHtml(role);
        const posChips = renderSlotChips(pn.positive, 'pos');
        const negChips = renderSlotChips(pn.negative, 'neg');
        return `<div class="per-role-row">
          <span class="per-role-name">${roleLabel}</span>
          ${posChips ? `<span class="per-role-pos">正:${posChips}</span>` : ''}
          ${negChips ? `<span class="per-role-neg">否:${negChips}</span>` : ''}
        </div>`;
      }).join('');
    };

    html += `<table class="intent-slots-table"><thead><tr>
      <th>Slot</th><th>值</th><th>来源</th>
    </tr></thead><tbody>`;
    for (const [slot, info] of Object.entries(slotsDetail)) {
      const isPerRoleSlot = slot === 'target_slots';
      let valuesHtml;
      if (isPerRoleSlot) {
        valuesHtml = renderTargetSlots(info.values);
      } else {
        const rawValues = info.values || [];
        const isTagSlot = ['style_tags', 'occasion_tags'].includes(slot);
        if (isTagSlot && rawValues.length) {
          const cls = slot === 'style_tags' ? 'intent-tag style' : 'intent-tag occasion';
          valuesHtml = rawValues
            .map((v) => `<span class="${cls}">${escapeHtml(String(v))}</span>`)
            .join(' ');
        } else {
          valuesHtml = escapeHtml(rawValues.join(', ') || '—');
        }
      }
      const source = info.source || '—';
      html += `<tr>
        <td>${escapeHtml(slot)}</td>
        <td>${valuesHtml}</td>
        <td>${escapeHtml(source)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  }

  // 两路来源对比表（图搜 + LLM；Trie 不再参与决策）
  const sourceSlots = ev.source_slots;
  if (sourceSlots && typeof sourceSlots === 'object') {
    const sources = ['image', 'llm'];
    const sourceLabels = { image: '图搜', llm: 'LLM' };
    const allKeys = new Set();
    for (const src of sources) {
      if (sourceSlots[src]) {
        for (const k of Object.keys(sourceSlots[src])) allKeys.add(k);
      }
    }
    if (allKeys.size > 0) {
      html += `<h3 class="source-slots-title">各来源 Slot 提取结果</h3>`;
      html += `<table class="intent-slots-table source-slots-table"><thead><tr>
        <th>Slot</th>`;
      for (const src of sources) {
        html += `<th class="source-col source-col-${src}">${sourceLabels[src]}</th>`;
      }
      html += `</tr></thead><tbody>`;
      const sortedKeys = [...allKeys].sort();
      for (const slot of sortedKeys) {
        html += `<tr><td>${escapeHtml(slot)}</td>`;
        for (const src of sources) {
          const vals = (sourceSlots[src] && sourceSlots[src][slot]) || [];
          const isTagSlot = ['style_tags', 'occasion_tags'].includes(slot);
          let cellHtml;
          if (isTagSlot && vals.length) {
            const cls = slot === 'style_tags' ? 'intent-tag style' : 'intent-tag occasion';
            cellHtml = vals
              .map((v) => `<span class="${cls}">${escapeHtml(String(v))}</span>`)
              .join(' ');
          } else {
            cellHtml = vals.length ? escapeHtml(vals.join(', ')) : '<span class="slot-empty">—</span>';
          }
          html += `<td class="source-col source-col-${src}">${cellHtml}</td>`;
        }
        html += '</tr>';
      }
      html += '</tbody></table>';
    }
  }

  // 意图解析模块最终的 JSON（resolved UserIntent，含 target_slots positive/negative）
  if (ev.intent && typeof ev.intent === 'object') {
    const json = escapeHtml(JSON.stringify(ev.intent, null, 2));
    html += `<details class="intent-json-block">
      <summary>意图解析最终 JSON</summary>
      <pre class="intent-json-pre">${json}</pre>
    </details>`;
  }

  panel.querySelector('.intent-debug-content').innerHTML = html;
}

/* ── ES Query 调试 ── */

function removeShould(query) {
  if (!query || typeof query !== 'object') return query;
  const clone = JSON.parse(JSON.stringify(query));
  // 只去掉顶层 bool 中和 filter 同层的 should，保留 filter 内嵌套的 should
  const bool = clone.bool || (clone.query && clone.query.bool);
  if (bool && bool.filter && bool.should) {
    delete bool.should;
    delete bool.minimum_should_match;
  }
  return clone;
}

function buildCurl(esHost, esIndex, esQuery) {
  const noShould = removeShould(esQuery);
  const wrapped = noShould.query ? noShould : { query: noShould };
  const body = JSON.stringify(wrapped, null, 2);
  return `curl -s -XPOST '${esHost}/${esIndex}/_search?pretty' \\\n  -u "$ES_USERNAME:$ES_PASSWORD" \\\n  -H 'Content-Type: application/json' \\\n  -d '\n${body}\n'`;
}

function renderEsDebug(ev) {
  const panel = $('esDebugPanel');
  if (!panel) return;
  panel.classList.remove('hidden');

  const queries = ev.queries || [];
  const esHost = ev.es_host || 'http://127.0.0.1:9200';
  const esIndex = ev.es_index || '';

  if (!queries.length) {
    panel.querySelector('.es-debug-content').innerHTML = '<p>无 ES 查询</p>';
    return;
  }

  let html = '';
  queries.forEach((q, idx) => {
    const role = escapeHtml(q.role || '—');
    const source = escapeHtml(q.source || '—');
    const json = JSON.stringify(q.es_query || {}, null, 2);
    const curl = buildCurl(esHost, esIndex, q.es_query || {});
    html += `
      <div class="es-query-block">
        <div class="es-query-header">
          <span>Target Role: <strong>${role}</strong></span>
          <span>来源: <strong>${source}</strong></span>
        </div>
        <div class="es-query-tabs">
          <button type="button" class="es-tab active" data-tab="query" data-idx="${idx}">ES Query</button>
          <button type="button" class="es-tab" data-tab="curl" data-idx="${idx}">curl (去 should)</button>
          <button type="button" class="btn ghost small es-copy-btn" data-idx="${idx}">复制</button>
        </div>
        <pre class="es-query-json es-tab-pane active" data-idx="${idx}" data-tab="query">${escapeHtml(json)}</pre>
        <pre class="es-query-json es-tab-pane" data-idx="${idx}" data-tab="curl" style="display:none">${escapeHtml(curl)}</pre>
      </div>
    `;
  });
  panel.querySelector('.es-debug-content').innerHTML = html;

  /* tab 切换 */
  panel.querySelectorAll('.es-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      const idx = tab.dataset.idx;
      const target = tab.dataset.tab;
      const block = tab.closest('.es-query-block');
      block.querySelectorAll('.es-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === target));
      block.querySelectorAll('.es-tab-pane').forEach(p => {
        const show = p.dataset.tab === target;
        p.style.display = show ? '' : 'none';
        p.classList.toggle('active', show);
      });
    });
  });

  /* 复制：复制当前激活 tab 的内容 */
  panel.querySelectorAll('.es-copy-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const idx = btn.dataset.idx;
      const block = btn.closest('.es-query-block');
      const activePre = block.querySelector('.es-tab-pane.active');
      if (!activePre) return;
      const text = activePre.textContent || '';
      let ok = false;
      if (navigator.clipboard && window.isSecureContext) {
        try { await navigator.clipboard.writeText(text); ok = true; } catch {}
      }
      if (!ok) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
        document.body.appendChild(ta);
        ta.select();
        try { ok = document.execCommand('copy'); } catch {}
        document.body.removeChild(ta);
      }
      const orig = btn.textContent;
      btn.textContent = ok ? '已复制' : '复制失败';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    });
  });
}

/* ── 召回分支进度 ── */

function renderRecallBranches() {
  const li = document.querySelector('#pipelineSteps li[data-key="recall"]');
  if (!li) return;

  let branchEl = li.querySelector('.recall-branches');
  if (!branchEl) {
    branchEl = document.createElement('div');
    branchEl.className = 'recall-branches';
    li.appendChild(branchEl);
  }

  const paths = ['image_vector', 'text_vector', 'query2es', 'complementary_model'];
  const lines = [];
  paths.forEach((p) => {
    const b = recallBranches[p];
    const label = RECALL_PATH_LABELS[p] || p;
    if (!b) {
      lines.push(`<div class="recall-branch pending"><span class="branch-icon">○</span> ${label}: 等待</div>`);
      return;
    }
    const icon = b.status === 'done' ? '✓' : '…';
    const cls = b.status === 'done' ? 'done' : 'running';
    const unitLabel = b.unit === 'skus' ? '件' : '套';
    lines.push(`<div class="recall-branch ${cls}"><span class="branch-icon">${icon}</span> ${label}: ${b.count}${unitLabel} (${formatMs(b.elapsed_ms)})</div>`);
    // 各路召回的 per-role 商品数（global 模式，recall_done 下发；与通路总数同口径）
    const roleCnts = recallPathRoles[p] || {};
    const roleEntries = sortedRoles(roleCnts)
      .map((r) => `${roleLabel(r)}${roleCnts[r] || 0}`);
    if (roleEntries.length) {
      lines.push(`<div class="recall-branch role-sub"><span class="branch-icon">↳</span> ${roleEntries.join(' · ')}</div>`);
    }
  });

  // per-role 召回/去重明细（global 模式，recall_done 下发）
  const roleKeys = sortedRoles(recallRoles);
  if (roleKeys.length) {
    lines.push('<div class="recall-branch roles-sep">— 分 role 召回 → 去重 —</div>');
    roleKeys.forEach((r) => {
      const rc = recallRoles[r] || {};
      lines.push(
        `<div class="recall-branch done role-row"><span class="branch-icon">•</span> ${roleLabel(r)}: ${rc.before || 0}→${rc.after || 0}件</div>`,
      );
    });
  }

  branchEl.innerHTML = lines.join('');
}

function appendEventLog(ev) {
  events.push(ev);
  $('eventLog').textContent = JSON.stringify(events, null, 2);
}

function handleEvent(ev) {
  appendEventLog(ev);
  const t = ev.type;
  if (t === 'session_id') {
    sessionId = ev.session_id || sessionId;
    $('sessionId').textContent = sessionId;
    markStep(
      'session_id',
      `session ${sessionId.slice(0, 12)}…`,
      'done',
      ev.elapsed_ms,
    );
    return;
  }
  if (t === 'intent') {
    const intent = ev.intent || {};
    const anchorAttrs = ev.anchor_attrs || {};
    const parts = [`${intent.query_type || '?'} · roles ${(intent.target_roles || []).join(',')}`];
    const styleTags = intent.style_tags || [];
    const occasionTags = intent.occasion_tags || [];
    // 锚点结构化属性标签：长度/覆盖/场景（来自意图模块融合产出）
    const attrEntries = [
      ['length', anchorAttrs.length_class],
      ['覆盖', anchorAttrs.coverage],
      ['场景', anchorAttrs.scene_domain],
    ].filter(([, v]) => v != null && v !== '' && v !== 'n/a');
    const allTags = [
      ...attrEntries.map(([label, v]) => ({ label: `${label}:${v}`, cls: 'intent-tag attr' })),
      ...occasionTags.map((t) => ({ label: t, cls: 'intent-tag occasion' })),
      ...styleTags.map((t) => ({ label: t, cls: 'intent-tag style' })),
    ];
    const tagHtml = allTags
      .map((t) => `<span class="${t.cls}">${escapeHtml(t.label)}</span>`)
      .join('');
    markStep(
      'intent',
      `${parts[0]}`,
      'done',
      ev.elapsed_ms,
    );
    if (tagHtml) {
      const li = document.querySelector('#pipelineSteps li[data-key="intent"]');
      if (li) {
        const detailEl = li.querySelector('.step-detail');
        if (detailEl) {
          detailEl.innerHTML = escapeHtml(parts[0]) + ' ' + tagHtml;
        }
      }
    }
    renderIntentDebug(ev);
    return;
  }
  if (t === 'recall_progress') {
    recallBranches[ev.path] = {
      status: ev.status || 'done',
      count: ev.count || 0,
      unit: ev.unit || 'outfits',
      elapsed_ms: ev.elapsed_ms || 0,
    };
    markStep('recall', '召回中…', 'running');
    renderRecallBranches();
    return;
  }
  if (t === 'recall_done') {
    const before = ev.before_dedupe || 0;
    const after = ev.after_dedupe || 0;
    const mode = ev.mode || 'per_channel';
    let summary;
    if (mode === 'global') {
      const composed = ev.composed_outfit_count || 0;
      const hits = ev.multi_channel_hits || 0;
      const roles = ev.roles || {};
      const roleParts = Object.keys(roles).map((r) => {
        const rc = roles[r] || {};
        return `${roleLabel(r)} ${rc.before || 0}→${rc.after || 0}件`;
      });
      summary = (roleParts.length ? `召回 ${roleParts.join(' · ')}` : `召回商品 ${ev.recalled_sku_count || 0}件`)
        + ` · 组合搭配 ${composed}套`
        + (hits ? `（多路命中 ${hits}）` : '')
        + ` · 去重 ${before}→${after}`;
    } else {
      summary = `去重: ${before}→${after}`;
    }
    recallRoles = (mode === 'global') ? (ev.roles || {}) : {};
    // 各路召回的 per-role 商品数：由 roles[role].channels 转置为 {path: {role: count}}
    recallPathRoles = {};
    if (mode === 'global') {
      for (const [role, rc] of Object.entries(ev.roles || {})) {
        const ch = (rc && rc.channels) || {};
        for (const [path, cnt] of Object.entries(ch)) {
          if (!recallPathRoles[path]) recallPathRoles[path] = {};
          recallPathRoles[path][role] = cnt;
        }
      }
    }
    markStep(
      'recall',
      summary,
      'done',
      ev.elapsed_ms,
    );
    renderRecallBranches();
    return;
  }
  if (t === 'es_debug') {
    renderEsDebug(ev);
    return;
  }
  if (t === 'coarse_rank_start') {
    markStep('coarse_rank', '规则打分中…', 'running');
    return;
  }
  if (t === 'coarse_rank_done') {
    markStep(
      'coarse_rank',
      `${ev.input_count || 0}→${ev.output_count || 0} 套`,
      'done',
      ev.elapsed_ms,
    );
    return;
  }
  if (t === 'ranking_reason_start') {
    markStep('ranking_reason', '处理中…', 'running');
    return;
  }
  if (t === 'ranking_reason_done') {
    markStep(
      'ranking_reason',
      `${ev.input_count || 0}→${ev.output_count || 0} 套`,
      'done',
      ev.elapsed_ms,
    );
    return;
  }
  if (t === 'tryon_progress') {
    if (ev.status === 'running') {
      markStep('tryon', '试穿中…', 'running');
    } else if (ev.status === 'done') {
      markStep(
        'tryon',
        `${ev.success_count || 0}/${ev.total_count || 0} 套`,
        'done',
        ev.elapsed_ms,
      );
    }
    return;
  }
  if (t === 'outfit_results') {
    // 异步渲染，不阻塞后续 SSE 事件（如 done）的及时处理
    setTimeout(() => renderOutfits(ev.outfits || []), 0);
    return;
  }
  if (t === 'anchor_skus') {
    // backward compat: still handle if backend sends it
    return;
  }
  if (t === 'sku_results') {
    // backward compat: ignored
    return;
  }
  if (t === 'text') {
    // text reason - no dedicated step now, handled via outfit_results
    return;
  }
  if (t === 'done') {
    markStep('done', '流结束', 'done', ev.elapsed_ms);
    if (ev.total_ms != null) {
      showPipelineTotal(ev.total_ms);
    }
    setStatus('推荐完成', 'done');
    $('btnSubmit').disabled = false;
  }
}

async function readImageFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result;
      if (typeof dataUrl !== 'string') {
        reject(new Error('read failed'));
        return;
      }
      const comma = dataUrl.indexOf(',');
      resolve(comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl);
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function runChat() {
  const message = $('message').value.trim();
  const selected = $('selectedSku').value.trim();
  if (!message && !imageBase64 && !selected) {
    setStatus('请输入描述或上传图片', 'error');
    return;
  }

  $('btnSubmit').disabled = true;
  events = [];
  initPipeline();
  $('outfitGrid').innerHTML = '';
  $('eventLog').textContent = '';
  // 隐藏意图调试面板
  const intentPanel = $('intentDebugPanel');
  if (intentPanel) intentPanel.classList.add('hidden');
  const esPanel = $('esDebugPanel');
  if (esPanel) esPanel.classList.add('hidden');
  setStatus('请求中…', 'busy');

  const body = {
    session_id: sessionId,
    message,
  };
  if (imageBase64) {
    body.image_base64 = imageBase64;
  }
  if (selected) {
    body.selected_sku_id = selected;
  }
  body.enable_llm_rank_reason = $('toggleLlmRankReason').checked;
  body.enable_tryon = $('toggleTryon').checked;
  // 对外展示模式：强制开启 LLM 排序+理由、关闭虚拟试穿
  if (uiMode === 'presentation') {
    body.enable_llm_rank_reason = true;
    body.enable_tryon = false;
  }
  if (tryonPersonImage) {
    body.tryon_person_image = tryonPersonImage;
  }
  const selectedModel = $('modelSelect')?.value;
  if (selectedModel) {
    body.llm_model = selectedModel;
  }

  try {
    const resp = await fetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop() || '';
      for (const chunk of parts) {
        const line = chunk.split('\n').find((l) => l.startsWith('data:'));
        if (!line) {
          continue;
        }
        const raw = line.slice(5).trim();
        try {
          handleEvent(JSON.parse(raw));
        } catch {
          /* skip bad chunk */
        }
      }
    }
    if (!events.some((e) => e.type === 'done')) {
      setStatus('流已结束（未收到 done）', 'warn');
      $('btnSubmit').disabled = false;
    }
  } catch (err) {
    setStatus(`失败: ${err.message}`, 'error');
    markStep('done', String(err.message), 'error');
    $('btnSubmit').disabled = false;
  }
}

function clearAll() {
  $('message').value = '';
  $('selectedSku').value = '';
  $('imageFile').value = '';
  imageBase64 = null;
  $('imagePreview').classList.add('hidden');
  $('imagePreview').innerHTML = '';
  $('tryonPersonFile').value = '';
  tryonPersonImage = null;
  $('tryonPersonPreview').classList.add('hidden');
  $('tryonPersonPreview').innerHTML = '';
  events = [];
  initPipeline();
  $('outfitGrid').innerHTML = '';
  $('eventLog').textContent = '';
  const intentPanel = $('intentDebugPanel');
  if (intentPanel) intentPanel.classList.add('hidden');
  const esPanel2 = $('esDebugPanel');
  if (esPanel2) esPanel2.classList.add('hidden');
  setStatus('就绪');
}

$('imageFile').addEventListener('change', async (e) => {
  const file = e.target.files?.[0];
  if (!file) {
    imageBase64 = null;
    $('imagePreview').classList.add('hidden');
    return;
  }
  imageBase64 = await readImageFile(file);
  const prev = $('imagePreview');
  prev.classList.remove('hidden');
  prev.innerHTML = `<img src="${URL.createObjectURL(file)}" alt="preview" />`;
});

$('tryonPersonFile').addEventListener('change', async (e) => {
  const file = e.target.files?.[0];
  if (!file) {
    tryonPersonImage = null;
    $('tryonPersonPreview').classList.add('hidden');
    return;
  }
  tryonPersonImage = await readImageFile(file);
  const prev = $('tryonPersonPreview');
  prev.classList.remove('hidden');
  prev.innerHTML = `<img src="${URL.createObjectURL(file)}" alt="模特图预览" />`;
});

$('btnSubmit').addEventListener('click', runChat);
$('btnClear').addEventListener('click', clearAll);
$('btnCopyEvents').addEventListener('click', async () => {
  const text = JSON.stringify(events, null, 2);
  let ok = false;
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); ok = true; } catch {}
  }
  if (!ok) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch {}
    document.body.removeChild(ta);
  }
  setStatus(ok ? '事件 JSON 已复制' : '复制失败', ok ? 'done' : 'error');
});

const THEME_STORAGE_KEY = 'fila_agent_html_theme';

function applyTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  const btn = $('btnTheme');
  if (btn) {
    const isLight = next === 'light';
    btn.setAttribute('aria-label', isLight ? '切换到黑夜模式' : '切换到白天模式');
    btn.title = isLight ? '切换到黑夜模式' : '切换到白天模式';
  }
}

function initTheme() {
  const saved = localStorage.getItem(THEME_STORAGE_KEY);
  if (saved === 'light' || saved === 'dark') {
    applyTheme(saved);
    return;
  }
  applyTheme('light');
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  const next = current === 'light' ? 'dark' : 'light';
  applyTheme(next);
  localStorage.setItem(THEME_STORAGE_KEY, next);
}

async function loadUiConfig() {
  try {
    const res = await fetch(`${API_BASE}/api/ui-config`);
    if (res.ok) {
      const data = await res.json();
      showOutfitRankScores = Boolean(data.show_outfit_rank_scores);
      uiMode = data.ui_mode || 'debug';
      if (data.recall_path_labels && typeof data.recall_path_labels === 'object') {
        Object.assign(RECALL_PATH_LABELS, data.recall_path_labels);
      }
      if (data.recall_source_labels && typeof data.recall_source_labels === 'object') {
        Object.assign(RECALL_SOURCE_LABELS, data.recall_source_labels);
      }
      // 填充模型下拉框
      const sel = $('modelSelect');
      if (sel && Array.isArray(data.available_models)) {
        sel.innerHTML = '';
        for (const m of data.available_models) {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          if (m === data.default_model) opt.selected = true;
          sel.appendChild(opt);
        }
      }
      // presentation 模式：隐藏调试元素，覆盖默认值
      if (uiMode === 'presentation') {
        applyPresentationMode(data);
      }
    }
  } catch {
    showOutfitRankScores = false;
  }
}

function applyPresentationMode(data) {
  document.body.classList.add('presentation');
  // 页面标题去掉 Debug 字样
  document.title = 'FILA 穿搭推荐';
  const subtitle = document.querySelector('.subtitle');
  if (subtitle) subtitle.classList.add('hide-in-presentation');
  // 隐藏 header 中的调试链接（保留"穿搭浏览"与"批量评测"与"商品浏览"）
  document.querySelectorAll('.header-links a').forEach((a) => {
    const text = a.textContent.trim();
    if (text !== '穿搭浏览' && text !== '批量评测' && text !== '商品浏览') {
      a.classList.add('hide-in-presentation');
    }
  });
  // 对外版本保留"需求描述"文本输入（.input-col-message），不隐藏
  // 隐藏模特图/自拍输入框（第二个 input-col-image）
  const imageCols = document.querySelectorAll('.input-col-image');
  if (imageCols.length > 1) imageCols[1].classList.add('hide-in-presentation');
  // 隐藏调试开关区域（LLM排序、虚拟试穿 toggle、模型选择）
  const togglesCol = document.querySelector('.input-col-toggles');
  if (togglesCol) togglesCol.classList.add('hide-in-presentation');
  // 隐藏意图调试面板
  const intentPanel = $('intentDebugPanel');
  if (intentPanel) intentPanel.classList.add('hide-in-presentation');
  // 隐藏 ES Query 调试面板
  const esPanel = $('esDebugPanel');
  if (esPanel) esPanel.classList.add('hide-in-presentation');
  // 隐藏 SSE 事件日志面板
  const debugPanel = document.querySelector('.panel-debug');
  if (debugPanel) debugPanel.classList.add('hide-in-presentation');
  // "对话输入"改为"输入"
  const inputTitle = document.querySelector('.panel-input h2');
  if (inputTitle) inputTitle.textContent = '输入';
  // 从 PIPELINE 中移除 tryon 步骤
  const tryonIdx = PIPELINE.findIndex((s) => s.key === 'tryon');
  if (tryonIdx !== -1) PIPELINE.splice(tryonIdx, 1);
  // 强制关闭排序得分展示
  showOutfitRankScores = false;
  // 设置 toggle 默认值
  const toggleLlm = $('toggleLlmRankReason');
  if (toggleLlm) toggleLlm.checked = true;
  const toggleTryon = $('toggleTryon');
  if (toggleTryon) toggleTryon.checked = false;
}

initTheme();
$('btnTheme')?.addEventListener('click', toggleTheme);
$('sessionId').textContent = sessionId;
loadUiConfig().then(() => {
  initPipeline();
});
