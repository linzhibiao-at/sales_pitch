/** FILA 搭配评测 — 总览页 (review.html) */

const $ = (id) => document.getElementById(id);

// ── 主题 ──
function toggleTheme() {
  const cur = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  const next = cur === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('fila_eval_theme', next);
}
$('btnTheme')?.addEventListener('click', toggleTheme);

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── 评审角色 ──
const ROLE_KEY = 'fila_eval_role';
const NAME_KEY = 'fila_eval_name';
let currentRole = localStorage.getItem(ROLE_KEY) || '';
let currentName = localStorage.getItem(NAME_KEY) || '';

function applyRoleBadge() {
  const badge = $('roleBadge');
  if (!badge) return;
  if (currentRole) {
    badge.hidden = false;
    badge.textContent = currentName ? `${currentRole} · ${currentName}` : currentRole;
    badge.classList.remove('role-pm', 'role-designer', 'role-pm-proj', 'role-engineer', 'role-other');
    badge.classList.add(roleTagClass(currentRole));
  } else {
    badge.hidden = true;
  }
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

function openRoleGate() {
  const gate = $('roleGate');
  if (!gate) return;
  gate.classList.remove('hidden');
  // 预选当前角色
  document.querySelectorAll('.role-option').forEach((b) => {
    b.classList.toggle('selected', b.dataset.role === currentRole);
  });
  const nameInput = $('roleGateName');
  if (nameInput) nameInput.value = currentName || '';
  refreshEnterBtn();
  document.querySelector('main.layout').style.visibility = 'hidden';
}

function closeRoleGate() {
  const gate = $('roleGate');
  if (!gate) return;
  gate.classList.add('hidden');
  document.querySelector('main.layout').style.visibility = '';
}

function refreshEnterBtn() {
  const btn = $('roleGateEnter');
  if (!btn) return;
  btn.disabled = !currentRole;
}

// 绑定弹窗交互（元素在 review.html 上才存在）
(function setupRoleGate() {
  const opts = document.querySelectorAll('.role-option');
  opts.forEach((b) => {
    b.addEventListener('click', () => {
      opts.forEach((x) => x.classList.remove('selected'));
      b.classList.add('selected');
      currentRole = b.dataset.role;
      refreshEnterBtn();
    });
  });
  const nameInput = $('roleGateName');
  if (nameInput) {
    nameInput.addEventListener('input', () => {
      currentName = nameInput.value.trim();
    });
  }
  const enter = $('roleGateEnter');
  if (enter) {
    enter.addEventListener('click', () => {
      if (!currentRole) return;
      localStorage.setItem(ROLE_KEY, currentRole);
      localStorage.setItem(NAME_KEY, currentName || '');
      applyRoleBadge();
      closeRoleGate();
      onRoleReady();
    });
  }
  const badge = $('roleBadge');
  if (badge) badge.addEventListener('click', openRoleGate);
})();

function ensureRole() {
  if (currentRole) {
    applyRoleBadge();
    onRoleReady();
  } else {
    openRoleGate();
  }
}

// ── 读取 URL 参数 ──
const params = new URLSearchParams(location.search);
const ts = params.get('ts') || '';

function onRoleReady() {
  if (ts) {
    loadTsData(ts);
  } else {
    loadRunsList();
  }
}

// 启动：先确保角色已选，再渲染数据
ensureRole();

// ── 模式 1：无 ts → 列出所有历史评测 ──
async function loadRunsList() {
  $('pageSubtitle').textContent = '历史评测记录 · 选择时间戳查看详情';
  $('panelTop').style.display = 'none';
  $('panelBottom').style.display = 'none';
  $('panelShoes').style.display = 'none';
  $('panelRuns').style.display = '';

  try {
    const resp = await fetch('/eval/api/runs');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const runs = await resp.json();
    renderRunsSummary(runs);
    renderRunsList(runs);
  } catch (err) {
    $('summaryContent').textContent = `加载失败: ${err.message}`;
  }
}

function renderRunsSummary(runs) {
  const totalRuns = runs.length;
  const totalSkus = runs.reduce((s, r) => s + (r.total_skus || 0), 0);
  const totalSuccess = runs.reduce((s, r) => s + (r.success_count || 0), 0);
  const totalError = runs.reduce((s, r) => s + (r.error_count || 0), 0);
  $('summaryContent').innerHTML = `
    <div class="summary-stat">
      <span class="stat-value">${totalRuns}</span>
      <span class="stat-label">评测批次</span>
    </div>
    <div class="summary-stat">
      <span class="stat-value">${totalSkus}</span>
      <span class="stat-label">累计SKU</span>
    </div>
    <div class="summary-stat stat-ok">
      <span class="stat-value">${totalSuccess}</span>
      <span class="stat-label">累计成功</span>
    </div>
    <div class="summary-stat stat-err">
      <span class="stat-value">${totalError}</span>
      <span class="stat-label">累计失败</span>
    </div>
  `;
}

function renderRunsList(runs) {
  const container = $('runsList');
  container.innerHTML = '';
  if (!runs.length) {
    container.innerHTML = '<p class="empty-hint">暂无评测记录</p>';
    return;
  }
  for (const run of runs) {
    const href = `review.html?ts=${encodeURIComponent(run.ts)}`;
    const a = document.createElement('a');
    a.href = href;
    a.className = 'run-card';
    const timeLabel = run.eval_time
      ? new Date(run.eval_time).toLocaleString('zh-CN')
      : run.ts;
    a.innerHTML = `
      <span class="run-card-ts">${escapeHtml(run.ts)}</span>
      <span class="run-card-time">${escapeHtml(timeLabel)}</span>
      <span class="run-card-meta">
        ${run.total_skus} SKU · ${run.success_count} 成功${run.error_count ? ` · <span style="color:var(--error)">${run.error_count} 失败</span>` : ''}
      </span>
    `;
    container.appendChild(a);
  }
}

// ── 模式 2：有 ts → 展示该批次的中类分类 ──
async function loadTsData(ts) {
  $('pageSubtitle').innerHTML = `批次 ${escapeHtml(ts)} · <a href="review.html" class="back-link">&larr; 返回历史列表</a>`;
  $('panelTop').style.display = '';
  $('panelBottom').style.display = '';
  $('panelShoes').style.display = '';
  $('panelRuns').style.display = 'none';

  try {
    const resp = await fetch(`./results/${encodeURIComponent(ts)}/eval_results.json`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderSummary(data);
    renderLinks(data.categories || [], ts);
  } catch (err) {
    $('summaryContent').textContent = `加载失败: ${err.message}`;
  }
}

function renderSummary(d) {
  $('summaryContent').innerHTML = `
    <div class="summary-stat">
      <span class="stat-value">${d.total_skus}</span>
      <span class="stat-label">总SKU数</span>
    </div>
    <div class="summary-stat stat-ok">
      <span class="stat-value">${d.success_count}</span>
      <span class="stat-label">成功</span>
    </div>
    <div class="summary-stat stat-err">
      <span class="stat-value">${d.error_count}</span>
      <span class="stat-label">失败</span>
    </div>
    <div class="summary-stat">
      <span class="stat-value" style="font-size:0.9rem">${d.eval_time || '—'}</span>
      <span class="stat-label">评测时间</span>
    </div>
  `;
}

function renderLinks(categories, ts) {
  const topLinks = categories.filter((c) => c.up_down === '上装');
  const bottomLinks = categories.filter((c) => c.up_down === '下装');
  const shoeLinks = categories.filter((c) => c.up_down === '鞋');
  renderLinkGroup($('linksTop'), topLinks, ts);
  renderLinkGroup($('linksBottom'), bottomLinks, ts);
  renderLinkGroup($('linksShoes'), shoeLinks, ts);
}

function renderLinkGroup(container, items, ts) {
  container.innerHTML = '';
  if (!items.length) {
    container.innerHTML = '<p class="empty-hint">暂无数据</p>';
    return;
  }
  for (const g of items) {
    const href = `review_detail.html?ts=${encodeURIComponent(ts)}&file=${encodeURIComponent(g.file)}`;
    const a = document.createElement('a');
    a.href = href;
    a.className = 'cat-card';
    a.innerHTML = `
      <span class="cat-card-name">${escapeHtml(g.category_l2)}</span>
      <span class="cat-card-meta">${g.sku_count} 个SKU · ${g.outfit_count} 套搭配${g.error_count ? ` · <span style="color:var(--error)">${g.error_count} 失败</span>` : ''}</span>
    `;
    container.appendChild(a);
  }
}
