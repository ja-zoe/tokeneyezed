/* Tokeneyezed dashboard. Read-only: three views (Compare, Runs, one Run) over /api/*.
   Agent-written text (intents, diffs, flags) is always escaped before it touches innerHTML. */
(() => {
  'use strict';

  const view = document.getElementById('view');
  const state = { charts: [], timer: null, feedTimer: null, selected: new Set(), compare: { metric: 'held_out', mode: 'best', equal: true, ids: null } };

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const n = (k, one, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;
  const clock = (ts) => (ts ? new Date(ts).toLocaleTimeString([], { hour12: false }) : '');
  const tok = (v) => (v == null ? '—' : v >= 1e9 ? `${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}k` : String(v));
  const cumulative = (vals) => { let t = 0; return vals.map((v) => (v == null ? null : (t += v))); };
  const pct = (v, d = 1) => (v == null ? '—' : `${(v * 100).toFixed(d)}%`);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const nameColor = (n) => css(n === 'H' ? '--c-h' : n === 'H-mem' ? '--c-hmem' : '--c-b');
  const LABELS = { B: 'Baseline', H: 'Harness', 'H-mem': 'Harness, no memory' };
  const label = (name) => LABELS[name] || name;
  const when = (iso) => (iso ? new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '');
  /* "Harness, Sep 26, 1:35 PM". The raw session id stays in tooltips and the URL. */
  const runLabel = (r) => `${label(r.name)}${r.started_at ? `, ${when(r.started_at)}` : ''}`;
  const tag = (n) => `<span class="tag" data-name="${esc(n)}">${esc(label(n))}</span>`;
  const status = (s) => `<span class="status" data-s="${esc(s)}">${esc(s)}</span>`;
  const HELP = {
    held_out: 'Held-out: examples nobody saw during the run. The score we report.',
    val_pass: 'Validation: examples the harness scores and steers by. The agent never sees them.',
    visible_pass: 'Visible: examples the agent can run and code against. Highest, and easiest to game.',
    best: 'Best so far: the highest score reached up to each attempt. Flagged attempts never count.',
    each: 'Each attempt: every attempt as scored, so a bad attempt shows as a dip.',
  };
  const METRICS = { held_out: 'Held-out pass rate', val_pass: 'Validation pass rate', visible_pass: 'Visible pass rate' };

  function ago(iso) {
    if (!iso) return '—';
    const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }

  async function api(path) {
    const res = await fetch(path, { cache: 'no-store' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `${res.status} from ${path}`);
    return body;
  }

  /* ---------- lifecycle ---------- */

  function teardown() {
    state.charts.forEach((c) => c.destroy());
    state.charts = [];
    clearInterval(state.timer);
    clearInterval(state.feedTimer);
    state.timer = state.feedTimer = null;
    setLive(false);
  }

  function setLive(on) {
    document.getElementById('live-pill').hidden = !on;
  }

  function poll(ms, fn) {
    clearInterval(state.timer);
    state.timer = setInterval(() => { if (!document.hidden) fn().catch(() => {}); }, ms);
  }

  function fail(err) {
    view.innerHTML = `<div class="card"><div class="empty"><p class="mb-1"><strong>Couldn't load this.</strong></p>
      <p class="mb-0">${esc(err.message)}</p><p class="mt-2 mb-0">Check that <code>MONGODB_URI</code> is set and the cluster's access list includes this host.</p></div></div>`;
  }

  /* ---------- charts ---------- */

  function chartBase() {
    const text = css('--bs-secondary-color') || '#888';
    const grid = getComputedStyle(document.documentElement).getPropertyValue('--bs-border-color-translucent') || 'rgba(128,128,128,.2)';
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = text;
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      animation: { duration: 350, easing: 'easeOutCubic' },   // updates tween from the current values
      scales: {
        x: { title: { display: true, text: 'Attempt' }, grid: { display: false } },
        y: { min: 0, max: 1, ticks: { callback: (v) => `${Math.round(v * 100)}%` }, grid: { color: grid } },
      },
      plugins: {
        legend: { position: 'bottom', labels: { usePointStyle: true, boxWidth: 8 } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y == null ? '—' : pct(c.parsed.y)}` } },
      },
    };
  }

  /* Vertical dashed lines with a label: agent handoffs and replans. */
  const markPlugin = {
    id: 'marks',
    afterDatasetsDraw(chart, _a, opts) {
      const marks = (opts && opts.marks) || [];
      const { ctx, chartArea, scales } = chart;
      ctx.save();
      ctx.font = `11px ${Chart.defaults.font.family}`;
      marks.forEach((m, i) => {
        const x = scales.x.getPixelForValue(m.at - 1);
        if (!Number.isFinite(x) || x < chartArea.left || x > chartArea.right) return;
        ctx.strokeStyle = m.color; ctx.fillStyle = m.color; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, chartArea.top); ctx.lineTo(x, chartArea.bottom); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillText(m.label, x + 4, chartArea.top + 12 + (i % 3) * 13);
      });
      ctx.restore();
    },
  };

  function makeChart(id, config) {
    const chart = new Chart(document.getElementById(id), { ...config, plugins: [markPlugin] });
    state.charts.push(chart);
    return chart;
  }

  /* ---------- Runs ---------- */

  async function viewRuns() {
    view.innerHTML = `<div class="page-head"><div><h1>Runs</h1><p>Every run saved in Atlas, newest first.</p></div>
      <button class="btn btn-primary" id="compare-btn" disabled>Compare selected</button></div>
      <div class="card"><div class="table-responsive" id="runs-table"></div></div>`;
    document.getElementById('compare-btn').onclick = () => {
      state.compare.ids = [...state.selected];
      location.hash = '#/compare';
    };
    const paint = async () => {
      const runs = await api('/api/runs');
      const box = document.getElementById('runs-table');
      if (!box) return;
      if (!runs.length) {
        box.innerHTML = `<div class="empty"><p class="mb-1"><strong>No runs in Atlas yet.</strong></p>
          <p class="mb-0">Start one with <code>tokeneyezed run --config configs/h.toml</code> and it shows up here.</p></div>`;
        return;
      }
      box.innerHTML = `<table class="table table-borderless mb-0"><thead><tr>
        <th></th><th>Config</th><th>Started</th><th>Agent</th><th>Model</th><th>Status</th>
        <th class="num">Attempts</th><th class="num">Best validation</th><th class="num">Held-out</th>
        <th class="num">Flagged</th><th class="num">Replans</th><th>Updated</th></tr></thead><tbody>
        ${runs.map((r) => `<tr data-href="#/run/${encodeURIComponent(r.session_id)}">
          <td><input type="checkbox" class="form-check-input pick" data-id="${esc(r.session_id)}" aria-label="Select ${esc(r.session_id)}" ${state.selected.has(r.session_id) ? 'checked' : ''}></td>
          <td>${tag(r.name)}</td><td title="${esc(r.session_id)}">${esc(when(r.started_at))}</td>
          <td>${esc(r.agents.join(' → ') || '—')}</td><td class="muted">${esc(r.model || '—')}</td>
          <td>${status(r.status)}</td>
          <td class="num">${r.attempts}${r.budget ? `<span class="muted"> / ${r.budget}</span>` : ''}</td>
          <td class="num">${pct(r.best_val)}</td><td class="num"><strong>${pct(r.final_held_out)}</strong></td>
          <td class="num">${r.flagged || '—'}</td><td class="num">${r.replans || '—'}</td>
          <td class="muted">${ago(r.updated_at)}</td></tr>`).join('')}</tbody></table>`;
      box.querySelectorAll('tr[data-href]').forEach((tr) => {
        tr.addEventListener('click', (e) => { if (!e.target.classList.contains('pick')) location.hash = tr.dataset.href; });
      });
      box.querySelectorAll('.pick').forEach((cb) => cb.addEventListener('change', () => {
        cb.checked ? state.selected.add(cb.dataset.id) : state.selected.delete(cb.dataset.id);
        document.getElementById('compare-btn').disabled = state.selected.size < 2;
      }));
      document.getElementById('compare-btn').disabled = state.selected.size < 2;
      setLive(runs.some((r) => r.status === 'running'));
    };
    await paint();
    poll(5000, paint);
  }

  /* ---------- One run ---------- */

  const RUN_SHELL = `
    <div class="page-head"><div><h1 id="r-title"></h1><p id="r-sub"></p></div><a class="btn btn-outline-secondary" href="#/runs">All runs</a></div>
    <div class="row g-3 mb-3" id="r-stats"></div>
    <div class="card mb-3 feed-card"><div class="card-header d-flex justify-content-between align-items-start">
      <div><div class="card-title">Live feed</div><div class="card-sub" id="feed-sub">What the CLI is doing, read from Atlas.</div></div>
      <button class="btn btn-sm btn-outline-secondary" id="feed-latest" hidden>Jump to latest</button></div>
      <div class="card-body"><div class="feed" id="feed" role="log" aria-live="off" tabindex="0"></div></div></div>
    <div class="card mb-3"><div class="card-header"><div class="card-title">Score by attempt</div>
      <div class="card-sub" id="r-chart-note"></div></div>
      <div class="card-body"><div class="chart-box"><canvas id="r-score"></canvas></div></div></div>
    <div class="row g-3 mb-3">
      <div class="col-lg-7"><div class="card h-100"><div class="card-header"><div class="card-title">Goals</div>
        <div class="card-sub">One per spec section. A replan changes the strategy, not the goal.</div></div>
        <div class="table-responsive" id="r-goals"></div></div></div>
      <div class="col-lg-5"><div class="card h-100"><div class="card-header"><div class="card-title">Observer</div>
        <div class="card-sub">Tool calls it blocked, and attempts it kept out of memory.</div></div>
        <div class="card-body" id="r-flags"></div></div></div></div>
    <div class="card mb-3"><div class="card-header"><div class="card-title">Tokens per attempt</div>
      <div class="card-sub">Bars: everything the model read and wrote. Line: its largest single context window. Both as the agent reported them.</div></div>
      <div class="card-body" id="r-ctx"></div></div>
    <div class="card"><div class="card-header"><div class="card-title">Attempts</div></div>
      <div class="table-responsive" id="r-attempts"></div></div>`;

  async function viewRun(id) {
    view.innerHTML = RUN_SHELL;
    const first = await api(`/api/runs/${encodeURIComponent(id)}`);
    let sig = '';
    let score = null;
    let ctx = null;

    const paint = (d) => {
      const r = d.run;
      document.getElementById('r-title').innerHTML = `${tag(r.name)} <span class="muted ms-1" title="${esc(r.session_id)}">${esc(when(r.started_at))}</span>`;
      document.getElementById('r-sub').innerHTML = `${status(r.status)} · ${esc(r.agents.join(' → '))}${r.model ? ` · ${esc(r.model)}` : ''} · started ${ago(r.started_at)}`;
      const tile = (label, value, note = '') => `<div class="col-6 col-lg"><div class="card stat"><div class="stat-label">${label}</div><div class="stat-value">${value}</div><div class="stat-note">${note}</div></div></div>`;
      document.getElementById('r-stats').innerHTML =
        tile('Tokens processed', tok(r.tokens_processed), r.usage_attempts ? `across ${n(r.usage_attempts, 'attempt')}` : 'not recorded yet') +
        tile('Peak context window', tok(r.peak_context), 'largest single window') +
        tile('Held-out pass rate', pct(r.final_held_out), r.best_held_out != null ? `best ${pct(r.best_held_out)}` : 'not scored yet') +
        tile('Validation, latest', pct(r.final_val), `best ${pct(r.best_val)}`) +
        tile('Attempts', r.budget ? `${r.attempts}<span class="muted fs-5"> / ${r.budget}</span>` : r.attempts, `${r.goals_done}/${r.goals_total} goals done, ${n(r.replans, 'replan')}`) +
        tile('Flagged attempts', r.flagged, `${n(d.blocked.length, 'call')} blocked`);

      const pts = d.attempts;
      const labels = pts.map((p) => p.number);
      const flagged = pts.map((p) => p.outcome === 'flagged');
      const marks = [
        ...d.handoffs.map((h) => ({ at: h.number, label: `${h.from} → ${h.to}`, color: css('--c-hmem') })),
        ...d.goals.filter((g) => g.replan_at_attempt).map((g) => ({ at: g.replan_at_attempt, label: 'replan', color: css('--c-bad') })),
      ];
      const data = {
        labels,
        datasets: [
          { label: 'Validation', data: pts.map((p) => p.val_pass), borderColor: css('--c-h'), backgroundColor: css('--c-h'), tension: 0.25, spanGaps: true,
            pointRadius: flagged.map((f) => (f ? 6 : 3)), pointStyle: flagged.map((f) => (f ? 'crossRot' : 'circle')),
            pointBorderColor: flagged.map((f) => (f ? css('--c-bad') : css('--c-h'))), pointBorderWidth: flagged.map((f) => (f ? 2.5 : 1)) },
          { label: 'Held-out', data: pts.map((p) => p.held_out), borderColor: css('--c-good'), backgroundColor: css('--c-good'), tension: 0.25, spanGaps: true, pointRadius: 3 },
          { label: 'Visible', data: pts.map((p) => p.visible_pass), borderColor: css('--c-b'), borderDash: [3, 3], borderWidth: 1.5, tension: 0.25, spanGaps: true, pointRadius: 0 },
        ],
      };
      document.getElementById('r-chart-note').textContent = flagged.some(Boolean)
        ? 'Red crosses are attempts the gaming review flagged. They stay out of memory.' : '';
      if (score) { score.data = data; score.options.plugins.marks = { marks }; score.update(); }
      else { score = makeChart('r-score', { type: 'line', data, options: { ...chartBase(), plugins: { ...chartBase().plugins, marks: { marks } } } }); }

      document.getElementById('r-goals').innerHTML = d.goals.length ? `<table class="table table-borderless mb-0"><thead><tr><th>Section</th><th>Status</th>
        <th class="num">Attempts</th><th class="num">Best / target</th><th>Strategy</th></tr></thead><tbody>
        ${d.goals.map((g) => `<tr><td>${esc(g.section)}</td><td>${status(g.status === 'complete' ? 'complete' : 'open')}</td>
          <td class="num">${g.attempts}</td><td class="num">${pct(g.best_val, 0)} / ${pct(g.target, 0)}</td>
          <td>${g.replan_count ? `<span class="pill bad">replanned ${n(g.replan_count, 'time')}</span> <span class="muted">${esc(g.strategy_notes)}</span>` : '<span class="muted">first strategy</span>'}</td></tr>`).join('')}
        </tbody></table>` : '<div class="empty">No goals seeded for this run.</div>';

      const flagRows = [
        ...d.blocked.map((b) => `<div class="flag-row"><i class="bi bi-slash-circle"></i><div><div><span class="pill bad">${esc(b.verdict)}</span>
          <span class="muted">attempt ${esc(String(b.attempt_id || '').split('-').pop())} · ${esc(b.tool)}</span></div><div class="mono">${esc(b.input)}</div></div></div>`),
        ...pts.filter((p) => p.outcome === 'flagged').map((p) => `<div class="flag-row"><i class="bi bi-flag"></i><div><div><span class="pill bad">flagged</span>
          <span class="muted">attempt ${p.number}, kept out of memory</span></div><div>${esc(p.flags.join('; '))}</div></div></div>`),
      ];
      document.getElementById('r-flags').innerHTML = flagRows.length ? flagRows.join('') : '<div class="empty py-3">Nothing blocked or flagged.</div>';

      const processed = pts.map((p) => p.tokens_processed);
      const ctxBox = document.getElementById('r-ctx');
      if (processed.every((t) => t == null)) {
        if (ctx) { ctx.destroy(); state.charts = state.charts.filter((c) => c !== ctx); ctx = null; }
        ctxBox.innerHTML = `<div class="empty py-3">No token usage recorded for this run. It's read from the agent's transcript when each attempt finishes.</div>`;
      } else {
        if (!ctx) { ctxBox.innerHTML = '<div class="chart-box short"><canvas id="r-ctx-c"></canvas></div>'; }
        const cd = { labels, datasets: [
          { type: 'bar', label: 'Tokens processed', data: processed, backgroundColor: css('--c-h'), borderRadius: 4, yAxisID: 'y' },
          { type: 'line', label: 'Peak context window', data: pts.map((p) => p.context_tokens), borderColor: css('--c-hmem'), backgroundColor: css('--c-hmem'), tension: 0.25, pointRadius: 2.5, yAxisID: 'y1' },
        ] };
        if (ctx) { ctx.data = cd; ctx.update(); } else {
          const o = chartBase();
          o.scales.y = { beginAtZero: true, title: { display: true, text: 'Processed' }, ticks: { callback: (v) => tok(v) }, grid: o.scales.y.grid };
          o.scales.y1 = { position: 'right', beginAtZero: true, title: { display: true, text: 'Peak window' }, ticks: { callback: (v) => tok(v) }, grid: { drawOnChartArea: false } };
          o.plugins.tooltip = { callbacks: { label: (c) => `${c.dataset.label}: ${tok(c.parsed.y)}` } };
          ctx = makeChart('r-ctx-c', { type: 'bar', data: cd, options: o });
        }
      }

      document.getElementById('r-attempts').innerHTML = `<table class="table table-borderless mb-0"><thead><tr><th class="num">#</th><th>Agent</th><th>Section</th>
        <th>Intent</th><th class="num">Validation</th><th class="num">Held-out</th><th>Outcome</th></tr></thead><tbody>
        ${[...pts].reverse().map((p) => `<tr><td class="num">${p.number}</td><td>${esc(p.agent)}</td><td>${esc(p.section)}</td>
          <td class="intent" title="${esc(p.intent)}">${esc(p.intent)}</td><td class="num">${pct(p.val_pass)}</td><td class="num">${pct(p.held_out)}</td>
          <td><span class="pill ${p.outcome === 'improved' ? 'good' : p.outcome === 'flagged' || p.outcome === 'killed' ? 'bad' : ''}">${esc(p.status === 'running' ? 'running' : p.outcome)}</span></td></tr>`).join('')}</tbody></table>`;
      setLive(r.status === 'running');
    };

    const apply = (d) => {
      const next = `${d.attempts.length}|${d.run.status}|${d.attempts.at(-1)?.closed_at}|${d.blocked.length}`;
      if (next !== sig) { sig = next; paint(d); }
    };
    apply(first);
    mountFeed(id, first.run.status === 'running');
    if (first.run.status === 'running') {
      poll(3000, async () => {
        const d = await api(`/api/runs/${encodeURIComponent(id)}`);
        apply(d);
        if (d.run.status !== 'running') clearInterval(state.timer);
      });
    }
  }

  /* ---------- Live feed ---------- */

  /* A terminal-style tail of the run. Entries come from /feed (attempts, hook events, replans).
     It sticks to the bottom while you're at the bottom, and leaves you alone once you scroll up. */
  function mountFeed(id, live) {
    const box = document.getElementById('feed');
    const jump = document.getElementById('feed-latest');
    const sub = document.getElementById('feed-sub');
    const seen = new Set();
    let cursor = null;
    let pinned = true;
    const atBottom = () => box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    box.addEventListener('scroll', () => { pinned = atBottom(); jump.hidden = pinned; });
    jump.onclick = () => { box.scrollTop = box.scrollHeight; };

    const line = (e) => `<div class="fl ${esc(e.kind)}"><time>${esc(clock(e.ts))}</time><span>${esc(e.text)}${e.detail ? `<em>${esc(e.detail)}</em>` : ''}</span></div>`;
    const pull = async () => {
      const q = cursor ? `?after=${encodeURIComponent(cursor)}` : '';
      const f = await api(`/api/runs/${encodeURIComponent(id)}/feed${q}`);
      const fresh = f.entries.filter((e) => { const k = `${e.ts}|${e.kind}|${e.text}|${e.detail}`; if (seen.has(k)) return false; seen.add(k); return true; });
      if (fresh.length) {
        box.insertAdjacentHTML('beforeend', fresh.map(line).join(''));
        if (pinned) box.scrollTop = box.scrollHeight;
      }
      cursor = f.cursor;
      if (!box.children.length) box.innerHTML = '<div class="fl info"><time></time><span>Waiting for the first event…</span></div>';
      sub.textContent = f.running ? 'Streaming from Atlas every 1.5 s.' : 'This run has finished. Showing what it recorded.';
      setLive(f.running);
      if (!f.running) clearInterval(state.feedTimer);
    };
    pull().then(() => { box.scrollTop = box.scrollHeight; }).catch(fail);
    if (live) state.feedTimer = setInterval(() => { if (!document.hidden) pull().catch(() => {}); }, 1500);
  }

  /* ---------- Compare ---------- */

  const runningMax = (vals, clean) => { let m = null; return vals.map((v, i) => { if (clean[i] && v != null) m = m == null ? v : Math.max(m, v); return m; }); };

  async function viewCompare() {
    const c = state.compare;
    view.innerHTML = `<div class="page-head"><div><h1>Compare</h1><p id="cmp-sub"></p></div></div>
      <div class="row g-3 mb-3" id="cmp-cards"></div>
      <div class="card mb-3"><div class="card-header d-flex justify-content-between align-items-start flex-wrap gap-2">
        <div><div class="card-title">Score by attempt</div><div class="card-sub" id="cmp-help"></div></div>
        <div class="d-flex gap-2 flex-wrap"><div class="seg" id="seg-metric"></div><div class="seg" id="seg-mode"></div></div></div>
        <div class="card-body"><div class="chart-box"><canvas id="cmp-chart"></canvas></div><p class="card-sub mt-2 mb-0" id="cmp-note"></p></div></div>
      <div class="card mb-3"><div class="card-header"><div class="card-title">Tokens processed, running total</div>
        <div class="card-sub" id="cmp-tok-help">Everything the model read and wrote, as the agent reported it. Long runs are where this grows.</div></div>
        <div class="card-body" id="cmp-tok-box"><div class="chart-box short"><canvas id="cmp-tok"></canvas></div></div></div>
      <div class="chips mb-3" id="cmp-chips"></div>`;
    let chart = null;
    let tokChart = null;
    let all = [];

    const segment = (id, options, current, set) => {
      const el = document.getElementById(id);
      el.innerHTML = options.map(([k, label]) => `<button type="button" data-k="${k}" aria-pressed="${k === current}">${label}</button>`).join('');
      el.onclick = (e) => { const b = e.target.closest('button'); if (b) { set(b.dataset.k); load(); } };
    };

    const load = async () => {
      all = await api('/api/runs');
      const q = c.ids && c.ids.length ? `?ids=${c.ids.map(encodeURIComponent).join(',')}` : '';
      const data = await api(`/api/compare${q}`);
      if (!c.ids || !c.ids.length) c.ids = data.runs.map((r) => r.session_id);
      paint(data);
      const running = data.runs.some((r) => r.summary.status === 'running');
      setLive(running);
      if (running) poll(5000, async () => { paint(await api(`/api/compare?ids=${c.ids.map(encodeURIComponent).join(',')}`)); });
      else { clearInterval(state.timer); }
    };

    const paint = (data) => {
      segment('seg-metric', Object.entries(METRICS).map(([k, l]) => [k, l.replace(' pass rate', '')]), c.metric, (k) => { c.metric = k; });
      segment('seg-mode', [['best', 'Best so far'], ['each', 'Each attempt']], c.mode, (k) => { c.mode = k; });
      if (!data.runs.length) {
        document.getElementById('cmp-sub').textContent = '';
        view.querySelector('.card-body').innerHTML = `<div class="empty"><p class="mb-1"><strong>Nothing to compare yet.</strong></p><p class="mb-0">Pick at least one run below, or start one with <code>tokeneyezed run --config configs/b.toml</code>.</p></div>`;
      }
      document.getElementById('cmp-help').textContent = `${HELP[c.metric]} ${HELP[c.mode]}`;
      const cap = c.equal ? data.common : Math.max(0, ...data.runs.map((r) => r.points.length));
      document.getElementById('cmp-sub').textContent = `${METRICS[c.metric]}, ${c.mode === 'best' ? 'best clean attempt so far' : 'each attempt'}, by attempt number.`;
      const labels = Array.from({ length: cap }, (_, i) => i + 1);
      const datasets = data.runs.map((r) => {
        const pts = r.points.slice(0, cap);
        const raw = pts.map((p) => p[c.metric]);
        const vals = c.mode === 'best' ? runningMax(raw, pts.map((p) => p.clean)) : raw;
        return { label: runLabel(r.summary), data: vals, borderColor: nameColor(r.name), backgroundColor: nameColor(r.name), tension: 0.25, spanGaps: true, pointRadius: 2.5, borderWidth: 2.5 };
      });
      const missing = data.runs.filter((r) => c.metric === 'held_out' && r.points.every((p) => p.held_out == null)).map((r) => r.name);
      document.getElementById('cmp-note').textContent =
        (c.equal && data.runs.length > 1 ? `Cut to the shortest run: ${data.common} attempts each, so the lines compare at equal attempt counts. ` : '') +
        (missing.length ? `No held-out scores yet for ${missing.map(label).join(', ')}.` : '');
      const cd = { labels, datasets };
      if (chart) { chart.data = cd; chart.update(); }
      else if (document.getElementById('cmp-chart')) chart = makeChart('cmp-chart', { type: 'line', data: cd, options: chartBase() });

      const tokSets = data.runs.map((r) => ({
        label: runLabel(r.summary),
        data: cumulative(r.points.slice(0, cap).map((p) => p.tokens_processed)),
        borderColor: nameColor(r.name), backgroundColor: nameColor(r.name), tension: 0.25, spanGaps: true, pointRadius: 0, borderWidth: 2.5,
      }));
      const anyTokens = tokSets.some((d) => d.data.some((v) => v != null));
      const tokBox = document.getElementById('cmp-tok-box');
      if (!anyTokens) {
        if (tokChart) { tokChart.destroy(); state.charts = state.charts.filter((x) => x !== tokChart); tokChart = null; }
        tokBox.innerHTML = '<div class="empty py-3">No token usage recorded for these runs yet. It is read from each attempt\'s transcript when the attempt finishes.</div>';
      } else {
        if (!tokChart) { tokBox.innerHTML = '<div class="chart-box short"><canvas id="cmp-tok"></canvas></div>'; }
        const td = { labels, datasets: tokSets };
        if (tokChart) { tokChart.data = td; tokChart.update(); } else {
          const o = chartBase();
          o.scales.y = { beginAtZero: true, ticks: { callback: (v) => tok(v) }, grid: o.scales.y.grid };
          o.plugins.tooltip = { callbacks: { label: (x) => `${x.dataset.label}: ${tok(x.parsed.y)}` } };
          tokChart = makeChart('cmp-tok', { type: 'line', data: td, options: o });
        }
      }

      document.getElementById('cmp-chips').innerHTML = all.slice(0, 12).map((r) => `<button type="button" class="chip" data-id="${esc(r.session_id)}" aria-pressed="${c.ids.includes(r.session_id)}">${tag(r.name)}<span title="${esc(r.session_id)}">${esc(when(r.started_at))}</span></button>`).join('')
        + `<button type="button" class="chip" id="eq" aria-pressed="${c.equal}"><i class="bi bi-rulers"></i>Equal attempts</button>`;
      document.getElementById('cmp-chips').onclick = (e) => {
        const b = e.target.closest('.chip'); if (!b) return;
        if (b.id === 'eq') c.equal = !c.equal;
        else c.ids = c.ids.includes(b.dataset.id) ? c.ids.filter((i) => i !== b.dataset.id) : [...c.ids, b.dataset.id];
        load();
      };
      document.getElementById('cmp-cards').innerHTML = data.runs.map((r) => {
        const s = r.summary;
        return `<div class="col-md-6 col-xl-4"><a class="card stat text-reset text-decoration-none d-block" href="#/run/${encodeURIComponent(r.session_id)}">
          <div class="d-flex justify-content-between"><span>${tag(r.name)} <span class="muted small">${esc(when(s.started_at))}</span></span>${status(s.status)}</div>
          <div class="row mt-2 g-2">
            <div class="col-6"><div class="stat-value">${tok(s.tokens_processed)}</div><div class="stat-label">tokens processed</div></div>
            <div class="col-6"><div class="stat-value">${tok(s.peak_context)}</div><div class="stat-label">peak context window</div></div></div>
          <div class="stat-note mt-2">Held-out ${pct(s.final_held_out)} · ${s.attempts} attempts · ${s.flagged} flagged · ${n(s.replans, 'replan')}</div></a></div>`;
      }).join('');
    };
    await load();
  }

  /* ---------- router ---------- */

  async function route() {
    teardown();
    const [, kind, arg] = (location.hash || '#/compare').split('/');
    document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('active', a.dataset.nav === (kind === 'run' ? 'runs' : kind)));
    document.getElementById('crumb').textContent = kind === 'run' ? 'Runs / run detail' : kind === 'runs' ? 'Runs' : 'Compare';
    try {
      if (kind === 'live') {
        const { session_id } = await api('/api/live');
        if (session_id) { location.replace(`#/run/${encodeURIComponent(session_id)}`); return; }
        view.innerHTML = `<div class="card"><div class="empty"><p class="mb-1"><strong>Nothing is running.</strong></p><p class="mb-0">Start a run with <code>tokeneyezed run --config configs/h.toml</code> and this page picks it up.</p></div></div>`;
        return;
      }
      if (kind === 'runs') await viewRuns();
      else if (kind === 'run') await viewRun(decodeURIComponent(arg));
      else await viewCompare();
    } catch (err) { fail(err); }
  }

  document.getElementById('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-bs-theme', next);
    try { localStorage.setItem('lte-theme', next); } catch {}
    route();
  });
  window.addEventListener('hashchange', route);
  route();
})();
