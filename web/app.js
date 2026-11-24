'use strict';

/* =========================================================
   刘一策略 · 前端终端
   ========================================================= */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  cfg: null,
  symbols: [],
  mode: 'paper',
  live: null,
  bt: null,
  btFilter: 'all',
  lastTradeKey: '',
  sseOpen: false,
  es: null,
  charts: {},
  pollJobs: {},
};

const PAGES = {
  trade: ['TRADING DESK', '交易台', '实时信号、持仓与成交执行'],
  backtest: ['BACKTEST LAB', '策略回测', '用历史数据验证刘一策略的收益、回撤与仓位节奏'],
  strategy: ['STRATEGY', '策略参数', '自定义信号、加仓节奏与成本模型'],
  data: ['DATA VAULT', '数据中心', '同步与管理永续合约历史 K 线'],
  settings: ['SETTINGS', '设置', 'API 密钥、网络代理与实盘风险确认'],
};

const DEFAULT_STRATEGY = {
  name: '刘一策略',
  symbol: 'ETH/USDT',
  timeframe: '15m',
  ema_fast: 25,
  ema_slow: 99,
  initial_capital: 10000,
  base_position: 10000,
  target_profit_pct: 0.07,
  add_after_failures: 4,
  add_position_pct: 0.5,
  max_position_multiplier: 5,
  skip_crosses: 20,
  max_leverage: 30,
  growth_per_1000: 500,
  taker_fee: 0.0005,
  funding_fee: 0.0001,
  leverage_mode: 'auto',
  fixed_leverage: 1,
};

/* ---------------- utilities ---------------- */
const nf = (d = 2) => new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: d,
  maximumFractionDigits: d,
});

function num(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}
function fmt(n, d = 2) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return '—';
  return nf(d).format(Number(n));
}
function money(n, d = 2) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return '—';
  return fmt(n, d);
}
function signed(n, d = 2) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return '—';
  return `${n > 0 ? '+' : ''}${fmt(n, d)}`;
}
function signedPct(n, d = 2) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return '—';
  return `${n > 0 ? '+' : ''}${fmt(n, d)}%`;
}
function clsOf(n) {
  if (!Number.isFinite(Number(n)) || Number(n) === 0) return '';
  return Number(n) > 0 ? 'up' : 'down';
}
function priceFmt(n) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return '—';
  const a = Math.abs(Number(n));
  if (a >= 1000) return fmt(n, 2);
  if (a >= 1) return fmt(n, 4);
  return fmt(n, 6);
}
function timeFmt(ms, withDate = true) {
  if (!ms) return '—';
  const d = new Date(Number(ms));
  const p = (x) => String(x).padStart(2, '0');
  const t = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  if (!withDate) return t;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${t}`;
}
function dateShort(ms) {
  if (!ms) return '—';
  const d = new Date(Number(ms));
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function isoDate(d) {
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, options = {}) {
  const opts = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    const msg = (data && (data.error || data.detail)) || `请求失败 (${res.status})`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

function toast(message, type = 'info', ttl = 3800) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .25s';
    setTimeout(() => el.remove(), 260);
  }, ttl);
}

function setBusy(btn, busy, text) {
  if (!btn) return;
  if (busy) {
    btn.dataset.original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = text || '处理中…';
  } else {
    btn.disabled = false;
    if (btn.dataset.original) btn.innerHTML = btn.dataset.original;
  }
}

/* ---------------- charts ---------------- */
const TOOLTIP = {
  backgroundColor: 'rgba(13,15,19,.96)',
  borderColor: 'rgba(255,255,255,.12)',
  borderWidth: 1,
  padding: [8, 11],
  textStyle: { color: '#dfe3e9', fontSize: 11 },
  extraCssText: 'box-shadow:0 12px 30px rgba(0,0,0,.5);border-radius:9px;',
};

function ensureChart(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  let inst = echarts.getInstanceByDom(el);
  if (!inst) {
    inst = echarts.init(el, null, { renderer: 'canvas' });
    state.charts[id] = inst;
  }
  return inst;
}
function resizeCharts() {
  Object.values(state.charts).forEach((c) => { try { c.resize(); } catch (e) { /* ignore */ } });
}
window.addEventListener('resize', () => {
  clearTimeout(window.__rz);
  window.__rz = setTimeout(resizeCharts, 120);
});

function axisTimeLabel(ts) {
  const d = new Date(Number(ts));
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderLiveEquity(history, current) {
  const chart = ensureChart('equityChart');
  if (!chart) return;
  const pts = (history || []).map((h) => [h.ts, h.equity]);
  const lastTs = pts.length ? pts[pts.length - 1][0] : 0;
  if (Number.isFinite(Number(current)) && current > 0) {
    const ts = Date.now();
    if (!lastTs || ts - lastTs > 30000) pts.push([ts, Number(current)]);
    else pts[pts.length - 1] = [lastTs, Number(current)];
  }
  if (!pts.length) {
    chart.clear();
    chart.setOption({
      backgroundColor: 'transparent',
      graphic: { type: 'text', left: 'center', top: 'middle', style: { text: '暂无权益采样数据', fill: '#545b67', fontSize: 12 } },
    });
    return;
  }
  const values = pts.map((p) => p[1]);
  const min = Math.min(...values);
  const max = Math.max(...values);
  chart.setOption({
    animationDuration: 300,
    grid: { left: 8, right: 18, top: 22, bottom: 22, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis',
      formatter: (p) => `${timeFmt(p[0].axisValue)}<br/><b style="color:#f0b83f">${money(p[0].data[1])}</b> USDT`,
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: 'rgba(255,255,255,.08)' } },
      axisTick: { show: false },
      axisLabel: { color: '#5f6773', fontSize: 10, hideOverlap: true, formatter: (v) => axisTimeLabel(v) },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      min: (v) => (v.min === v.max ? v.min * 0.999 : v.min - (v.max - v.min) * 0.12),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#5f6773', fontSize: 10, formatter: (v) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)) },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,.045)' } },
    },
    series: [{
      type: 'line',
      data: pts,
      showSymbol: false,
      smooth: 0.25,
      lineStyle: { color: '#f0b83f', width: 2 },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(240,184,63,.28)' },
          { offset: 1, color: 'rgba(240,184,63,0)' },
        ]),
      },
    }],
  }, true);
}

/* ---------------- navigation ---------------- */
function switchView(view) {
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
  const [eyebrow, title, sub] = PAGES[view];
  $('#pageEyebrow').textContent = eyebrow;
  $('#pageTitle').textContent = title;
  let dynamicSub = sub;
  if (view === 'trade' && state.cfg) {
    dynamicSub = `${state.cfg.strategy.name} · ${state.cfg.strategy.symbol} 永续 · ${state.cfg.strategy.timeframe}`;
  }
  $('#pageSub').textContent = dynamicSub;
  if (view === 'data') loadInventory();
  setTimeout(resizeCharts, 60);
}

/* ---------------- config / forms ---------------- */
function fillSymbols() {
  const list = state.symbols.length ? state.symbols : [{ value: 'ETH/USDT' }];
  const current = (state.cfg && state.cfg.strategy.symbol) || 'ETH/USDT';
  const values = list.map((s) => s.value);
  if (!values.includes(current)) values.unshift(current);
  ['#cfgSymbol', '#btSymbol', '#spSymbol', '#dsSymbol'].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.innerHTML = values.map((v) => `<option value="${esc(v)}">${esc(v.replace('/', ' / '))} 永续</option>`).join('');
    el.value = current;
  });
}

function applyConfig(cfg) {
  state.cfg = cfg;
  const s = cfg.strategy;

  $('#cfgExchange').value = cfg.exchange || 'binance';
  $('#cfgTf').value = s.timeframe;
  $('#cfgSymbol').value = s.symbol;
  $('#cfgBase').value = s.base_position;
  $('#cfgTarget').value = (s.target_profit_pct * 100).toFixed(2).replace(/\.?0+$/, '');
  $('#cfgCapital').value = s.initial_capital;

  fillStrategyForm(s);
  fillSettings(cfg);
  updateModeHint();
  updateKeyState();
  updateStrategyTag();
  updateTopEquity(state.live);
  updateTradeMetaFromCfg();

  if (document.getElementById('view-strategy').classList.contains('active')) renderStrategyPreview();

  const sub = `${s.name} · ${s.symbol} 永续 · ${s.timeframe}`;
  $('#pageSub').textContent = sub;
  $('#btDataTag').textContent = `${s.symbol} · ${s.timeframe}`;
}

function fillStrategyForm(s) {
  $('#spName').value = s.name;
  $('#spSymbol').value = s.symbol;
  $('#spTf').value = s.timeframe;
  $('#spCapital').value = s.initial_capital;
  $('#spBase').value = s.base_position;
  $('#spGrowth').value = s.growth_per_1000;
  $('#spFast').value = s.ema_fast;
  $('#spSlow').value = s.ema_slow;
  $('#spTarget').value = +(s.target_profit_pct * 100).toFixed(4);
  $('#spAddAfter').value = s.add_after_failures;
  $('#spAddPct').value = +(s.add_position_pct * 100).toFixed(4);
  $('#spMaxMult').value = s.max_position_multiplier;
  $('#spSkip').value = s.skip_crosses;
  $('#spMaxLev').value = s.max_leverage;
  $('#spLevMode').value = s.leverage_mode;
  $('#spFixedLev').value = s.fixed_leverage;
  $('#spTaker').value = +(s.taker_fee * 100).toFixed(4);
  $('#spFunding').value = +(s.funding_fee * 100).toFixed(4);
  $('#fixedLevWrap').style.display = s.leverage_mode === 'fixed' ? '' : 'none';
}

function fillSettings(cfg) {
  $('#setProxy').value = cfg.proxy || '';
  $('#setExchange').value = cfg.exchange || 'binance';
  $('#setDataSource').value = cfg.data_source || 'binance';
  $('#setMode').value = cfg.default_mode || 'paper';
  $('#riskAck').checked = !!cfg.risk_ack;
  $('#riskStatus').textContent = cfg.risk_ack ? '已确认实盘风险' : '未确认';
  $('#riskStatus').classList.toggle('ok', !!cfg.risk_ack);

  const keys = cfg.keys || {};
  const b = keys.binance || {};
  const bt = keys.binance_testnet || {};
  const okx = keys.okx || {};
  $('#binanceKey').value = '';
  $('#binanceSecret').value = '';
  $('#binanceTestKey').value = '';
  $('#binanceTestSecret').value = '';
  $('#okxKey').value = '';
  $('#okxSecret').value = '';
  $('#okxPass').value = '';
  $('#binanceKey').placeholder = b.configured ? b.api_key : '主网 API Key';
  $('#binanceSecret').placeholder = b.configured ? '已保存（留空则不修改）' : '主网 Secret';
  $('#binanceTestKey').placeholder = bt.configured ? bt.api_key : 'Futures Testnet Key';
  $('#binanceTestSecret').placeholder = bt.configured ? '已保存（留空则不修改）' : 'Futures Testnet Secret';
  $('#okxKey').placeholder = okx.configured ? okx.api_key : 'API Key';
  $('#okxSecret').placeholder = okx.configured ? '已保存（留空则不修改）' : 'Secret';
  $('#okxPass').placeholder = okx.configured && okx.passphrase ? okx.passphrase : 'Passphrase';

  $('#keyBadgeBinance').textContent = `主网${b.configured ? ' ✓' : ' —'} · 测试网${bt.configured ? ' ✓' : ' —'}`;
  $('#keyBadgeBinance').classList.toggle('ok', !!(b.configured && bt.configured));
  $('#keyBadgeOkx').textContent = okx.configured ? '已配置' : '未配置';
  $('#keyBadgeOkx').classList.toggle('ok', !!okx.configured);
}

function updateStrategyTag() {
  if (!state.cfg) return;
  $('#strategyTag').textContent = state.cfg.strategy.name;
  $('#tradeStatusPill').classList.toggle('running', !!(state.live && state.live.running));
}

function updateTradeMetaFromCfg() {
  const s = state.cfg.strategy;
  if (!state.live || !state.live.running) {
    $('#tradeMeta').textContent = `${s.name} · ${s.symbol} 永续 · ${s.timeframe} · EMA${s.ema_fast}/${s.ema_slow}`;
  }
}

function currentTradeConfig() {
  const s = state.cfg.strategy;
  return {
    ...s,
    symbol: $('#cfgSymbol').value,
    timeframe: $('#cfgTf').value,
    base_position: num($('#cfgBase').value, s.base_position),
    target_profit_pct: num($('#cfgTarget').value, s.target_profit_pct * 100) / 100,
    initial_capital: num($('#cfgCapital').value, s.initial_capital),
  };
}

function collectStrategy() {
  const s = {
    name: $('#spName').value.trim() || '刘一策略',
    symbol: $('#spSymbol').value,
    timeframe: $('#spTf').value,
    initial_capital: num($('#spCapital').value, 10000),
    base_position: num($('#spBase').value, 10000),
    growth_per_1000: num($('#spGrowth').value, 500),
    ema_fast: Math.round(num($('#spFast').value, 25)),
    ema_slow: Math.round(num($('#spSlow').value, 99)),
    target_profit_pct: num($('#spTarget').value, 7) / 100,
    add_after_failures: Math.round(num($('#spAddAfter').value, 4)),
    add_position_pct: num($('#spAddPct').value, 50) / 100,
    max_position_multiplier: num($('#spMaxMult').value, 5),
    skip_crosses: Math.round(num($('#spSkip').value, 20)),
    max_leverage: num($('#spMaxLev').value, 30),
    growth_per_1000: num($('#spGrowth').value, 500),
    taker_fee: num($('#spTaker').value, 0.05) / 100,
    funding_fee: num($('#spFunding').value, 0.01) / 100,
    leverage_mode: $('#spLevMode').value,
    fixed_leverage: num($('#spFixedLev').value, 1),
  };
  return s;
}

/* ---------------- mode ---------------- */
function setMode(mode) {
  const exchange = $('#cfgExchange').value;
  if (mode === 'testnet' && exchange !== 'binance') {
    toast('测试网仅支持币安', 'warn');
    return;
  }
  state.mode = mode;
  $$('#modeSeg .seg').forEach((b) => b.classList.toggle('active', b.dataset.mode === mode));
  const box = $('#liveConfirmBox');
  box.hidden = mode !== 'live';
  $('#liveConfirmInput').value = '';
  $('#liveRiskCheck').checked = false;
  updateModeHint();
  updateKeyState();
}

function updateModeHint() {
  const mode = state.mode;
  const keyName = mode === 'testnet' ? 'binance_testnet' : $('#cfgExchange').value;
  const keys = (state.cfg && state.cfg.keys) || {};
  const configured = keys[keyName] && keys[keyName].configured;
  const hints = {
    paper: '纸面模式不连接私有接口，使用实时行情模拟撮合，适合验证策略。',
    testnet: configured
      ? '已检测到测试网 Key。将使用币安 Futures Testnet 实时行情与模拟资金撮合。'
      : '测试网模式需要单独配置币安 Futures Testnet API Key（设置页）。',
    live: '实盘模式将使用真实资金市价下单。本策略无独立止损，请务必小仓位并随时一键全平。',
  };
  $('#modeHint').textContent = hints[mode];
  $('#modeHint').style.color = mode === 'live' ? '#ff8b96' : '';
}

function updateKeyState() {
  const el = $('#keyState');
  if (!el || !state.cfg) return;
  const mode = state.mode;
  const exchange = $('#cfgExchange').value;
  const keys = state.cfg.keys || {};
  if (mode === 'paper') {
    el.className = 'key-state';
    el.textContent = '纸面模拟 · 无需 API Key';
    return;
  }
  const slot = mode === 'testnet' ? 'binance_testnet' : exchange;
  const ok = keys[slot] && keys[slot].configured;
  el.className = `key-state ${ok ? 'ok' : 'warn'}`;
  const label = mode === 'testnet' ? '币安测试网' : (exchange === 'okx' ? '欧易主网' : '币安主网');
  el.textContent = ok ? `${label} Key 已配置 · 可启动` : `${label} Key 未配置 · 请前往设置页填写`;
}

function syncModeAvailability() {
  const exchange = $('#cfgExchange').value;
  const testnetSeg = $('#modeSeg .seg[data-mode="testnet"]');
  testnetSeg.disabled = exchange !== 'binance';
  testnetSeg.style.opacity = exchange === 'binance' ? '' : '.4';
  if (exchange !== 'binance' && state.mode === 'testnet') setMode('paper');
  updateKeyState();
  updateModeHint();
}

/* ---------------- live status ---------------- */
function setConnection(ok) {
  state.sseOpen = ok;
  $('#connDot').className = `dot ${ok ? 'live' : 'err'}`;
  $('#connText').textContent = ok ? '服务在线' : '连接中断';
  $('#connPill').title = ok ? 'SSE 实时推送已连接' : '实时推送断开，正在重试';
}

function updateTopEquity(snap) {
  const equity = snap && Number(snap.equity) > 0 ? Number(snap.equity) : (state.cfg ? state.cfg.strategy.initial_capital : 0);
  $('#topEquity').textContent = money(equity);
  $('#topEquitySub').textContent = 'USDT';
  if (state.cfg && state.cfg.strategy.initial_capital > 0) {
    const pnl = equity - state.cfg.strategy.initial_capital;
    $('#topEquity').className = clsOf(pnl);
  }
}

function renderLive(snap) {
  if (!snap) return;
  state.live = snap;
  const running = !!snap.running;
  const pos = snap.position;
  const hasError = !!snap.error;

  $('#sideDot').className = `dot ${running ? 'live' : hasError ? 'err' : ''}`;
  $('#sideStatus').textContent = running ? '引擎运行中' : hasError ? '引擎异常' : '引擎待机';
  $('#sideMode').textContent = `${snap.mode} · ${snap.exchange}`;

  const pill = $('#tradeStatusPill');
  pill.className = 'status-pill' + (hasError ? ' error' : running ? (snap.ready ? ' running' : ' warn') : '');
  $('#tradeStatusText').textContent = running ? (snap.ready ? '运行中' : '初始化中') : hasError ? '异常' : '待机';

  const metaBits = [];
  if (snap.mode) metaBits.push(snap.mode === 'paper' ? '纸面模拟' : snap.mode === 'testnet' ? '币安测试网' : '实盘');
  if (snap.exchange) metaBits.push(snap.exchange === 'okx' ? '欧易' : '币安');
  if (snap.symbol) metaBits.push(snap.symbol);
  if (snap.timeframe) metaBits.push(snap.timeframe);
  if (snap.last_update) metaBits.push(`更新 ${timeFmt(snap.last_update, false)}`);
  $('#tradeMeta').textContent = metaBits.join(' · ') || '尚未启动实时引擎';
  if (snap.error) $('#tradeMeta').textContent += ` · ${snap.error}`;

  const equity = Number(snap.equity) || 0;
  $('#statEquity').textContent = money(equity);
  $('#statEquity').className = clsOf(equity - (state.cfg ? state.cfg.strategy.initial_capital : equity));
  $('#statEquitySub').textContent = snap.mode === 'paper' ? '模拟权益 · USDT' : '账户权益 · USDT';

  const upnl = pos ? Number(pos.net_pnl || 0) : 0;
  $('#statUpnl').textContent = pos ? signed(upnl) : '—';
  $('#statUpnl').className = pos ? clsOf(upnl) : '';
  $('#statUpnlSub').textContent = pos ? `${signedPct(pos.net_pct || 0)} / 仓位` : '按标记价';

  $('#statPosition').textContent = pos ? (pos.side > 0 ? '多头' : '空头') : '空仓';
  $('#statPosition').className = pos ? (pos.side > 0 ? 'up' : 'down') : '';
  $('#statPositionSub').textContent = pos ? `${money(pos.notional, 0)} USDT · ${fmt(pos.leverage, 2)}x` : '等待交叉信号';
  $('#statSkip').textContent = fmt(snap.skip_remaining || 0, 0);

  renderPosition(pos, snap);
  renderLogs(snap.logs || []);
  updateTopEquity(snap);
  renderLiveEquity(snap.equity_history || [], equity);

  $('#btnStartLive').disabled = running;
  $('#btnStartLive2').disabled = running;
  $('#btnStopLive').disabled = !running;
  $('#sideStatus').textContent = running ? '引擎运行中' : '引擎待机';

  ['cfgExchange', 'cfgSymbol', 'cfgTf', 'cfgBase', 'cfgTarget', 'cfgCapital'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = running;
  });
  $$('#modeSeg .seg').forEach((b) => { b.disabled = running || (b.dataset.mode === 'testnet' && $('#cfgExchange').value !== 'binance'); });

  if (running) {
    $('#btnStartLive').textContent = '运行中';
    $('#btnStartLive').disabled = true;
  } else {
    $('#btnStartLive').innerHTML = '启动策略';
    $('#btnStartLive').disabled = false;
  }

  const count = Number(snap.trades_count || 0);
  const key = `${snap.mode_key || ''}:${count}`;
  if (key !== state.lastTradeKey) {
    state.lastTradeKey = key;
    loadLiveTrades(snap.mode_key || '');
  }
}

function renderPosition(pos, snap) {
  const tag = $('#posSideTag');
  const body = $('#posBody');
  if (!pos) {
    tag.textContent = 'FLAT';
    tag.className = 'tag neutral';
    tag.style.color = '';
    body.innerHTML = '<div class="empty-hint">暂无持仓 · 等待 EMA 交叉信号</div>';
    $('#posMeta').textContent = snap && snap.skip_remaining > 0
      ? `空仓期 · 还需跳过 ${snap.skip_remaining} 次交叉`
      : '空仓 · 等待下一次交叉';
    return;
  }
  const side = Number(pos.side);
  tag.textContent = side > 0 ? 'LONG' : 'SHORT';
  tag.className = `tag ${side > 0 ? 'long' : 'short'}`;
  $('#posMeta').textContent = `开仓 ${timeFmt(pos.entry_ts)} · ${fmt(pos.leverage, 2)}x`;
  const target = Number(pos.target_net) || 0;
  const net = Number(pos.net_pnl) || 0;
  const progress = target > 0 ? Math.max(0, Math.min(100, net / target * 100)) : 0;
  body.innerHTML = `
    <div class="pos-row"><span>方向 / 数量</span><b>${side > 0 ? '多' : '空'} · ${fmt(pos.qty, 4)}</b></div>
    <div class="pos-row"><span>开仓均价</span><b>${priceFmt(pos.entry)}</b></div>
    <div class="pos-row"><span>名义仓位</span><b>${money(pos.notional, 2)} USDT</b></div>
    <div class="pos-row"><span>目标止盈价</span><b class="gold">${priceFmt(pos.target_px)}</b></div>
    <div class="pos-row"><span>预估强平价</span><b class="down">${priceFmt(pos.liquidation_px)}</b></div>
    <div class="pos-row"><span>浮动净盈亏</span><b class="${clsOf(net)}">${signed(net)} (${signedPct(pos.net_pct || 0)})</b></div>
    <div class="pos-progress">
      <div class="label"><span>目标进度</span><span>${fmt(progress, 1)}%</span></div>
      <div class="bar green"><i style="width:${progress}%"></i></div>
    </div>`;
}

function renderLogs(logs) {
  const box = $('#logList');
  if (!logs || !logs.length) {
    box.innerHTML = '<div class="empty-hint">启动引擎后，这里会显示实时信号与成交。</div>';
    return;
  }
  const html = logs.slice(-60).map((l) => `
    <div class="log-line ${esc(l.level || 'info')}">
      <time>${timeFmt(l.ts, false)}</time>
      <p>${esc(l.message)}</p>
    </div>`).join('');
  box.innerHTML = html;
  box.scrollTop = box.scrollHeight;
}

function renderLiveTrades(rows) {
  const body = $('#liveTradesBody');
  if (!rows || !rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-cell">暂无成交</td></tr>';
    return;
  }
  body.innerHTML = rows.slice(0, 8).map((t) => {
    const side = Number(t.side);
    const pnl = Number(t.pnl) || 0;
    return `<tr>
      <td><span class="side-badge ${side > 0 ? 'long' : 'short'}">${side > 0 ? '多' : '空'}</span></td>
      <td>${priceFmt(t.entry)}</td>
      <td>${priceFmt(t.exit)}</td>
      <td class="${clsOf(pnl)}">${signed(pnl)}</td>
      <td><span class="res-badge ${t.success ? 'win' : 'loss'}">${t.success ? '达标' : '未达标'}</span></td>
    </tr>`;
  }).join('');
}

async function loadLiveTrades(modeKey) {
  try {
    const q = modeKey ? `?mode=${encodeURIComponent(modeKey)}&limit=20` : '?limit=20';
    const rows = await api('/api/trades' + q);
    renderLiveTrades(rows);
    $('#liveTradeMeta').textContent = rows.length ? `最近 ${rows.length} 笔` : '本次会话';
  } catch (e) { /* ignore */ }
}

async function loadLogs() {
  try {
    const rows = await api('/api/logs?limit=60');
    if (rows.length) renderLogs(rows);
  } catch (e) { /* ignore */ }
}

/* ---------------- SSE ---------------- */
function connectSSE() {
  if (state.es) state.es.close();
  const es = new EventSource('/api/live/events');
  state.es = es;
  es.onopen = () => setConnection(true);
  es.onmessage = (ev) => {
    setConnection(true);
    try { renderLive(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
  };
  es.onerror = () => setConnection(false);
}

/* ---------------- backtest ---------------- */
function buildMetrics(m) {
  const items = [
    { label: '总收益率', value: `${m.total_return_pct > 0 ? '+' : ''}${fmt(m.total_return_pct, 2)}%`, cls: clsOf(m.total_return_pct), sub: `期末 ${money(m.final_equity)} USDT` },
    { label: '净利润', value: `${signed(m.net_profit)} USDT`, cls: clsOf(m.net_profit), sub: `初始 ${money(m.initial_capital)}` },
    { label: '最大回撤', value: `-${fmt(m.max_drawdown_pct, 2)}%`, cls: 'down', sub: `峰值 ${money(m.peak_equity)}` },
    { label: '胜率（达标）', value: `${fmt(m.win_rate_pct, 1)}%`, cls: '', sub: `${m.wins} 达标 / ${m.losses} 未达标` },
    { label: '交易笔数', value: fmt(m.trades, 0), cls: '', sub: `交叉 ${m.crosses} · 跳过 ${m.skipped_crosses}` },
    { label: '盈亏比', value: m.profit_factor === null ? '∞' : fmt(m.profit_factor, 2), cls: '', sub: `平均盈利 ${money(m.avg_win)}` },
    { label: '最大实际杠杆', value: `${fmt(m.max_leverage_used, 2)}x`, cls: m.max_leverage_used > 10 ? 'down' : '', sub: `上限 ${state.cfg ? state.cfg.strategy.max_leverage : 30}x` },
    { label: '成本合计', value: money(m.total_fees + m.total_funding), cls: '', sub: `手续费 ${money(m.total_fees)} · 资金费 ${money(m.total_funding)}` },
  ];
  return items.map((it) => `
    <div class="metric">
      <span>${it.label}</span>
      <strong class="${it.cls}">${it.value}</strong>
      <em>${it.sub}</em>
    </div>`).join('');
}

function renderBacktest(res) {
  state.bt = res;
  const m = res.metrics;
  $('#btEmpty').hidden = true;
  $('#btResult').hidden = false;
  $('#btMetrics').innerHTML = buildMetrics(m);
  $('#btChartMeta').textContent = `${dateShort(m.start_ts)} → ${dateShort(m.end_ts)} · ${fmt(m.bars, 0)} 根 K 线 · 暴露 ${fmt(m.exposure_pct, 1)}% · 夏普 ${m.sharpe === null ? '—' : fmt(m.sharpe, 2)}`;
  $('#btTradesBody').innerHTML = '';
  state.btFilter = 'all';
  $$('#btFilter .seg').forEach((b) => b.classList.toggle('active', b.dataset.filter === 'all'));

  renderBacktestEquity(res);
  renderBacktestPrice(res);
  renderBacktestTrades();
  $('#btDataTag').textContent = `${res.config.symbol} · ${res.config.timeframe}`;
  $('#btTradeMeta').textContent = `共 ${m.trades} 笔 · 平均持仓 ${fmt(m.avg_hold_hours, 1)} 小时 · 覆盖 ${fmt(res.coverage_pct || 100, 1)}%`;
  setTimeout(resizeCharts, 60);
}

function renderBacktestEquity(res) {
  const chart = ensureChart('btEquityChart');
  if (!chart) return;
  const curve = res.equity_curve || [];
  const eq = curve.map((p) => [p[0], p[1]]);
  const dd = curve.map((p) => [p[0], -Math.abs(p[2])]);
  chart.setOption({
    animation: false,
    grid: { left: 10, right: 12, top: 24, bottom: 24, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis',
      formatter: (params) => {
        const row = params[0];
        const d = curve[params[0].dataIndex] || [];
        return `${timeFmt(row.axisValue)}<br/>权益 <b style="color:#f0b83f">${money(row.data[1])}</b> USDT<br/>回撤 <span style="color:#ff5d6c">-${fmt(Math.abs(d[2] || 0), 2)}%</span>`;
      },
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: 'rgba(255,255,255,.08)' } },
      axisTick: { show: false },
      axisLabel: { color: '#5f6773', fontSize: 10, hideOverlap: true, formatter: (v) => axisTimeLabel(v) },
    },
    yAxis: [
      {
        type: 'value', scale: true,
        axisLabel: { color: '#5f6773', fontSize: 10, formatter: (v) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)) },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,.045)' } },
      },
      {
        type: 'value', max: 0, min: -(res.metrics.max_drawdown_pct || 10) * 1.2,
        axisLabel: { color: '#7a4a52', fontSize: 9, formatter: (v) => `${Math.abs(v).toFixed(0)}%` },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '权益', type: 'line', data: eq, showSymbol: false, smooth: 0.2,
        lineStyle: { color: '#f0b83f', width: 1.8 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(240,184,63,.24)' },
            { offset: 1, color: 'rgba(240,184,63,0)' },
          ]),
        },
      },
      {
        name: '回撤', type: 'line', yAxisIndex: 1, data: dd, showSymbol: false,
        lineStyle: { color: 'rgba(255,93,108,.55)', width: 1 },
        areaStyle: { color: 'rgba(255,93,108,.10)' },
      },
    ],
  }, true);
}

function renderBacktestPrice(res) {
  const chart = ensureChart('btPriceChart');
  if (!chart) return;
  const candles = res.candles || [];
  const labels = candles.map((c) => axisTimeLabel(c[0]));
  const ohlc = candles.map((c) => [c[1], c[4], c[3], c[2]]);
  const fast = res.ema_fast || [];
  const slow = res.ema_slow || [];
  const tsList = candles.map((c) => c[0]);

  const nearest = (ts) => {
    let lo = 0, hi = tsList.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (tsList[mid] < ts) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0 && Math.abs(tsList[lo - 1] - ts) < Math.abs(tsList[lo] - ts)) return lo - 1;
    return lo;
  };

  const longPts = [];
  const shortPts = [];
  (res.trades || []).forEach((t) => {
    if (!tsList.length) return;
    const idx = nearest(t.entry_ts);
    const item = {
      value: [idx, t.entry],
      name: `${t.side > 0 ? '开多' : '开空'} · ${priceFmt(t.entry)}`,
      ts: t.entry_ts,
      pnl: t.pnl,
    };
    (t.side > 0 ? longPts : shortPts).push(item);
  });

  chart.setOption({
    animation: false,
    grid: { left: 10, right: 14, top: 22, bottom: 52, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      trigger: 'axis',
      axisPointer: { type: 'cross', lineStyle: { color: 'rgba(255,255,255,.18)' }, crossStyle: { color: 'rgba(255,255,255,.18)' } },
      formatter: (params) => {
        const p = params.find((x) => x.seriesType === 'candlestick');
        let out = '';
        if (p) {
          const v = p.data;
          out += `${timeFmt(tsList[p.dataIndex])}<br/>开 ${priceFmt(v[1])} · 高 ${priceFmt(v[3])}<br/>低 ${priceFmt(v[2])} · 收 ${priceFmt(v[4])}`;
        }
        const sc = params.find((x) => x.seriesType === 'scatter');
        if (sc) out += `<br/><span style="color:#f0b83f">${sc.data.name}</span>`;
        return out;
      },
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: 'rgba(255,255,255,.08)' } },
      axisTick: { show: false },
      axisLabel: { color: '#5f6773', fontSize: 9, hideOverlap: true },
    },
    yAxis: {
      type: 'value', scale: true,
      axisLabel: { color: '#5f6773', fontSize: 10, formatter: (v) => priceFmt(v) },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,.045)' } },
    },
    dataZoom: [
      { type: 'inside', start: 70, end: 100 },
      { type: 'slider', height: 16, bottom: 8, start: 70, end: 100, borderColor: 'transparent', backgroundColor: 'rgba(255,255,255,.03)', fillerColor: 'rgba(240,184,63,.10)', handleStyle: { color: '#3a3f48' }, textStyle: { color: '#5f6773', fontSize: 9 } },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: ohlc,
        itemStyle: { color: '#2fd07f', color0: '#ff5d6c', borderColor: '#2fd07f', borderColor0: '#ff5d6c' },
        barMaxWidth: 8,
      },
      { name: 'EMA25', type: 'line', data: fast, showSymbol: false, smooth: true, lineStyle: { color: '#5b8cff', width: 1 }, connectNulls: false },
      { name: 'EMA99', type: 'line', data: slow, showSymbol: false, smooth: true, lineStyle: { color: '#a78bfa', width: 1 }, connectNulls: false },
      {
        name: '开多', type: 'scatter', data: longPts, symbol: 'triangle', symbolSize: 8,
        itemStyle: { color: '#2fd07f' },
      },
      {
        name: '开空', type: 'scatter', data: shortPts, symbol: 'triangle', symbolRotate: 180, symbolSize: 8,
        itemStyle: { color: '#ff5d6c' },
      },
    ],
  }, true);
}

function renderBacktestTrades() {
  if (!state.bt) return;
  const filter = state.btFilter;
  let rows = state.bt.trades || [];
  if (filter === 'win') rows = rows.filter((t) => t.success);
  if (filter === 'loss') rows = rows.filter((t) => !t.success);
  const limited = rows.slice(-300);
  const body = $('#btTradesBody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="12" class="empty-cell">没有符合条件的成交</td></tr>';
    return;
  }
  const reasonMap = { target: '目标止盈', reverse: '反向平仓', liquidation: '爆仓', end: '期末平仓' };
  body.innerHTML = limited.slice().reverse().map((t, i) => {
    const pnl = Number(t.pnl) || 0;
    return `<tr>
      <td>${rows.length - i}</td>
      <td><span class="side-badge ${t.side > 0 ? 'long' : 'short'}">${t.side > 0 ? '多' : '空'}</span></td>
      <td>${timeFmt(t.entry_ts)}</td>
      <td>${timeFmt(t.exit_ts)}</td>
      <td>${priceFmt(t.entry)}</td>
      <td>${priceFmt(t.exit)}</td>
      <td>${money(t.notional, 0)}</td>
      <td>${fmt(t.leverage, 2)}x</td>
      <td class="${clsOf(pnl)}">${signed(pnl)}</td>
      <td class="${clsOf(t.pnl_pct)}">${signedPct(t.pnl_pct)}</td>
      <td><span class="res-badge ${t.success ? 'win' : 'loss'}">${t.success ? '达标' : '未达标'}</span></td>
      <td class="reason">${reasonMap[t.reason] || esc(t.reason)}</td>
    </tr>`;
  }).join('');
  if (rows.length > 300) {
    $('#btTradeMeta').textContent = `共 ${rows.length} 笔，显示最近 300 笔`;
  }
}

async function pollJob(jobId, onProgress) {
  for (let i = 0; i < 1200; i += 1) {
    const job = await api(`/api/jobs/${jobId}`);
    onProgress(job);
    if (job.status !== 'running') return job;
    await sleep(900);
  }
  throw new Error('任务超时');
}

async function runBacktest() {
  const startVal = $('#btStart').value;
  const endVal = $('#btEnd').value;
  if (!startVal || !endVal) { toast('请选择回测区间', 'warn'); return; }
  const start = new Date(`${startVal}T00:00:00`).getTime();
  const end = new Date(`${endVal}T23:59:59`).getTime();
  if (!(end > start)) { toast('结束日期必须晚于开始日期', 'warn'); return; }
  const strategy = {
    ...state.cfg.strategy,
    symbol: $('#btSymbol').value,
    timeframe: $('#btTf').value,
    initial_capital: num($('#btCapital').value, 10000),
  };
  const btn = $('#btRun');
  setBusy(btn, true, '回测中…');
  const wrap = $('#btProgressWrap');
  wrap.hidden = false;
  $('#btProgressBar').style.width = '2%';
  $('#btProgressText').textContent = '创建任务…';
  try {
    const { job_id: jobId } = await api('/api/backtest', {
      method: 'POST',
      body: {
        source: $('#btSource').value,
        start,
        end,
        strategy,
        auto_fetch: true,
      },
    });
    const job = await pollJob(jobId, (j) => {
      $('#btProgressBar').style.width = `${Math.max(2, j.progress || 0)}%`;
      $('#btProgressText').textContent = j.message || '运行中…';
    });
    if (job.status === 'error') throw new Error(job.error || '回测失败');
    $('#btProgressBar').style.width = '100%';
    $('#btProgressText').textContent = '完成';
    renderBacktest(job.result);
    toast(`回测完成：${fmt(job.result.metrics.total_return_pct, 2)}% · ${job.result.metrics.trades} 笔`, 'success');
    setTimeout(() => { wrap.hidden = true; }, 1000);
  } catch (e) {
    $('#btProgressText').textContent = e.message;
    toast(e.message, 'error', 6000);
  } finally {
    setBusy(btn, false);
    if (!wrap.hidden) setTimeout(() => { wrap.hidden = true; }, 2500);
  }
}

/* ---------------- strategy preview ---------------- */
function renderStrategyPreview() {
  const s = collectStrategy();
  const base = s.base_position;
  const add = s.add_after_failures;
  const multStep = s.add_position_pct;
  const rows = [];
  [0, add, add * 2, add * 3, add * 4].forEach((f) => {
    const mult = Math.min(1 + multStep * Math.floor(f / add), s.max_position_multiplier);
    const notional = base * mult;
    const need = s.target_profit_pct * 100 + s.taker_fee * 100 * 2;
    rows.push(`<tr>
      <td>${f}</td>
      <td>${fmt(mult, 2)}x</td>
      <td>${money(notional, 0)}</td>
      <td>约 ${fmt(need, 2)}%</td>
    </tr>`);
  });
  $('#spPreviewBody').innerHTML = rows.join('');
  $('#spPreviewNote').textContent = `按基础仓位 ${money(base, 0)} USDT 计算；每次加仓为基础仓位的 ${fmt(multStep * 100, 0)}%，倍数上限 ${fmt(s.max_position_multiplier, 2)}x。峰值权益每 +1000，基础仓位 +${money(s.growth_per_1000, 0)}。`;

  const lev = s.leverage_mode === 'fixed' ? `${fmt(s.fixed_leverage, 2)}x 固定` : `自动 · 上限 ${fmt(s.max_leverage, 0)}x`;
  $('#spSummary').innerHTML = `
    <div><span>策略</span><b>${esc(s.name)}</b></div>
    <div><span>交易对 / 周期</span><b>${esc(s.symbol)} · ${esc(s.timeframe)}</b></div>
    <div><span>EMA</span><b>${s.ema_fast} / ${s.ema_slow}</b></div>
    <div><span>基础仓位</span><b>${money(s.base_position, 0)} USDT</b></div>
    <div><span>目标净收益</span><b>${fmt(s.target_profit_pct * 100, 2)}%</b></div>
    <div><span>杠杆</span><b>${lev}</b></div>
    <div><span>成本</span><b>${fmt(s.taker_fee * 100, 3)}% / ${fmt(s.funding_fee * 100, 3)}%</b></div>
    <div><span>达标后跳过</span><b>${s.skip_crosses} 次交叉</b></div>`;
}

async function saveStrategy(silent = false) {
  const s = collectStrategy();
  if (Number(s.ema_slow) <= Number(s.ema_fast)) {
    toast('慢线周期必须大于快线周期', 'warn');
    return;
  }
  try {
    const res = await api('/api/strategy', { method: 'PUT', body: s });
    state.cfg.strategy = res.strategy;
    $('#cfgBase').value = res.strategy.base_position;
    $('#cfgTarget').value = +(res.strategy.target_profit_pct * 100).toFixed(4);
    $('#cfgCapital').value = res.strategy.initial_capital;
    $('#cfgTf').value = res.strategy.timeframe;
    $('#cfgSymbol').value = res.strategy.symbol;
    $('#btCapital').value = res.strategy.initial_capital;
    updateStrategyTag();
    updateTradeMetaFromCfg();
    renderStrategyPreview();
    if (!silent) toast('策略参数已保存', 'success');
  } catch (e) {
    toast(e.message, 'error', 5000);
  }
}

async function loadPresets() {
  try {
    const presets = await api('/api/presets');
    $('#spPresetSelect').innerHTML = presets.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('');
  } catch (e) { /* ignore */ }
}

/* ---------------- data ---------------- */
async function loadInventory() {
  try {
    const rows = await api('/api/data/inventory');
    const body = $('#inventoryBody');
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty-cell">暂无缓存，请先同步历史数据</td></tr>';
      $('#dataCacheTag').textContent = '0 条';
      return;
    }
    const total = rows.reduce((a, r) => a + Number(r.count || 0), 0);
    $('#dataCacheTag').textContent = `${fmt(total, 0)} 根`;
    body.innerHTML = rows.map((r) => `
      <tr>
        <td>${r.source === 'okx' ? '欧易' : '币安'}</td>
        <td>${esc(r.symbol)}</td>
        <td>${esc(r.timeframe)}</td>
        <td>${fmt(r.count, 0)}</td>
        <td>${dateShort(r.start)}</td>
        <td>${dateShort(r.end)}</td>
        <td><button class="btn btn-mini btn-danger" data-del-source="${esc(r.source)}" data-del-symbol="${esc(r.symbol)}" data-del-tf="${esc(r.timeframe)}">删除</button></td>
      </tr>`).join('');
    $$('#inventoryBody [data-del-source]').forEach((b) => {
      b.addEventListener('click', async () => {
        if (!confirm(`删除 ${b.dataset.delSymbol} ${b.dataset.delTf} 的本地缓存？`)) return;
        try {
          await api(`/api/data?source=${encodeURIComponent(b.dataset.delSource)}&symbol=${encodeURIComponent(b.dataset.delSymbol)}&timeframe=${encodeURIComponent(b.dataset.delTf)}`, { method: 'DELETE' });
          toast('缓存已删除', 'success');
          loadInventory();
        } catch (e) { toast(e.message, 'error'); }
      });
    });
  } catch (e) { /* ignore */ }
}

async function syncData() {
  const startVal = $('#dsStart').value;
  const endVal = $('#dsEnd').value;
  if (!startVal || !endVal) { toast('请选择同步区间', 'warn'); return; }
  const start = new Date(`${startVal}T00:00:00`).getTime();
  const end = new Date(`${endVal}T23:59:59`).getTime();
  const btn = $('#dsSync');
  setBusy(btn, true, '同步中…');
  const wrap = $('#dsProgressWrap');
  wrap.hidden = false;
  $('#dsProgressBar').style.width = '2%';
  $('#dsProgressText').textContent = '创建任务…';
  try {
    const { job_id: jobId } = await api('/api/data/sync', {
      method: 'POST',
      body: {
        source: $('#dsSource').value,
        symbol: $('#dsSymbol').value,
        timeframe: $('#dsTf').value,
        start,
        end,
      },
    });
    const job = await pollJob(jobId, (j) => {
      $('#dsProgressBar').style.width = `${Math.max(2, j.progress || 0)}%`;
      $('#dsProgressText').textContent = j.message || '同步中…';
    });
    if (job.status === 'error') throw new Error(job.error || '同步失败');
    $('#dsProgressBar').style.width = '100%';
    $('#dsProgressText').textContent = `完成 · 新增 ${fmt(job.result.inserted, 0)} 条`;
    toast(`同步完成：${fmt(job.result.inserted, 0)} 条`, 'success');
    loadInventory();
    setTimeout(() => { wrap.hidden = true; }, 1200);
  } catch (e) {
    $('#dsProgressText').textContent = e.message;
    toast(e.message, 'error', 6000);
  } finally {
    setBusy(btn, false);
    if (!wrap.hidden) setTimeout(() => { wrap.hidden = true; }, 3000);
  }
}

/* ---------------- settings ---------------- */
async function saveKeys(exchange) {
  const map = {
    binance: { api_key: '#binanceKey', secret: '#binanceSecret' },
    binance_testnet: { api_key: '#binanceTestKey', secret: '#binanceTestSecret' },
    okx: { api_key: '#okxKey', secret: '#okxSecret', passphrase: '#okxPass' },
  }[exchange];
  if (!map) return;
  const payload = { exchange };
  Object.entries(map).forEach(([field, sel]) => {
    const value = $(sel).value.trim();
    if (value) payload[field] = value;
  });
  if (Object.keys(payload).length === 1) { toast('请填写至少一项内容', 'warn'); return; }
  try {
    await api('/api/keys', { method: 'POST', body: payload });
    toast('API Key 已保存', 'success');
    await refreshConfig();
    applyConfig(state.cfg);
  } catch (e) { toast(e.message, 'error', 5000); }
}

async function testKey(exchange, mode, btn) {
  setBusy(btn, true, '测试中…');
  try {
    const res = await api('/api/keys/test', { method: 'POST', body: { exchange, mode } });
    toast(`连接成功 · 权益 ${money(res.equity)} USDT`, 'success', 6000);
  } catch (e) {
    toast(e.message, 'error', 7000);
  } finally {
    setBusy(btn, false);
  }
}

/* ---------------- start/stop ---------------- */
async function startLive() {
  const mode = state.mode;
  const exchange = $('#cfgExchange').value;
  if (mode === 'live') {
    if (!$('#liveRiskCheck').checked) { toast('请先勾选实盘风险确认', 'warn'); return; }
    if ($('#liveConfirmInput').value.trim() !== 'START LIVE') { toast('请输入 START LIVE 完成二次确认', 'warn'); return; }
  }
  if (mode === 'testnet') {
    const keys = (state.cfg && state.cfg.keys) || {};
    if (!keys.binance_testnet || !keys.binance_testnet.configured) {
      toast('请先在设置中配置币安测试网 API Key', 'warn', 5000);
      return;
    }
  }
  if (mode === 'live') {
    const keys = (state.cfg && state.cfg.keys) || {};
    if (!keys[exchange] || !keys[exchange].configured) {
      toast('请先在设置中配置该交易所 API Key', 'warn', 5000);
      return;
    }
  }
  const btn = $('#btnStartLive');
  setBusy(btn, true, '启动中…');
  try {
    const snap = await api('/api/live/start', {
      method: 'POST',
      body: {
        mode,
        exchange,
        strategy: currentTradeConfig(),
        confirm: mode === 'live' ? 'START LIVE' : undefined,
      },
    });
    renderLive(snap);
    toast('引擎已启动', 'success');
  } catch (e) {
    toast(e.message, 'error', 7000);
  } finally {
    setBusy(btn, false);
  }
}

async function stopLive() {
  if (state.live && state.live.position) {
    if (!confirm('当前仍有持仓。停止引擎不会自动平仓，是否继续？')) return;
  }
  try {
    const snap = await api('/api/live/stop', { method: 'POST', body: { close_position: false } });
    renderLive(snap);
    toast('引擎已停止', 'warn');
  } catch (e) { toast(e.message, 'error'); }
}

async function panic() {
  if (!confirm('将停止引擎并以市价平掉交易所全部仓位，确定执行？')) return;
  try {
    const snap = await api('/api/live/panic', { method: 'POST', body: {} });
    renderLive(snap);
    toast('已执行一键全平', 'warn');
  } catch (e) { toast(e.message, 'error', 6000); }
}

async function refreshConfig() {
  const cfg = await api('/api/config');
  state.cfg = cfg;
  return cfg;
}

/* ---------------- bindings ---------------- */
function bind() {
  $$('.nav-item').forEach((b) => b.addEventListener('click', () => switchView(b.dataset.view)));

  $$('#modeSeg .seg').forEach((b) => b.addEventListener('click', () => {
    if (b.disabled) return;
    setMode(b.dataset.mode);
  }));

  $('#cfgExchange').addEventListener('change', syncModeAvailability);
  $('#btnStartLive').addEventListener('click', startLive);
  $('#btnStartLive2').addEventListener('click', startLive);
  $('#btnStopLive').addEventListener('click', stopLive);
  $('#btnPanic').addEventListener('click', panic);
  $('#btnRefreshTrades').addEventListener('click', () => loadLiveTrades(state.live ? state.live.mode_key : ''));
  $('#btnRefreshLogs').addEventListener('click', loadLogs);

  $('#btRun').addEventListener('click', runBacktest);
  $$('#btFilter .seg').forEach((b) => b.addEventListener('click', () => {
    state.btFilter = b.dataset.filter;
    $$('#btFilter .seg').forEach((x) => x.classList.toggle('active', x === b));
    renderBacktestTrades();
  }));

  $('#spSave').addEventListener('click', () => saveStrategy(false));
  $('#spLevMode').addEventListener('change', () => {
    $('#fixedLevWrap').style.display = $('#spLevMode').value === 'fixed' ? '' : 'none';
    renderStrategyPreview();
  });
  $$('#view-strategy input, #view-strategy select').forEach((el) => {
    if (el.id !== 'spPresetSelect') el.addEventListener('input', renderStrategyPreview);
  });
  $('#spReset').addEventListener('click', () => {
    fillStrategyForm(DEFAULT_STRATEGY);
    renderStrategyPreview();
    toast('已填入刘一策略默认参数，点击保存生效', 'info');
  });
  $('#spApplyPreset').addEventListener('click', async () => {
    const name = $('#spPresetSelect').value;
    if (!name) return;
    try {
      const presets = await api('/api/presets');
      const p = presets.find((x) => x.name === name);
      if (!p) return;
      fillStrategyForm(p);
      renderStrategyPreview();
      toast(`已载入预设「${name}」，点击保存生效`, 'info');
    } catch (e) { toast(e.message, 'error'); }
  });
  $('#spSavePreset').addEventListener('click', async () => {
    const s = collectStrategy();
    try {
      const res = await api('/api/presets', { method: 'POST', body: s });
      await loadPresets();
      $('#spPresetSelect').value = s.name;
      toast(`已保存预设「${s.name}」`, 'success');
    } catch (e) { toast(e.message, 'error', 5000); }
  });

  $('#dsSync').addEventListener('click', syncData);
  $('#dsRefresh').addEventListener('click', loadInventory);

  $$('[data-save-key]').forEach((b) => b.addEventListener('click', () => saveKeys(b.dataset.saveKey)));
  $$('[data-test-key]').forEach((b) => b.addEventListener('click', () => testKey(b.dataset.testKey, b.dataset.mode, b)));

  $('#saveSettings').addEventListener('click', async () => {
    try {
      await api('/api/settings', {
        method: 'PUT',
        body: {
          proxy: $('#setProxy').value.trim(),
          exchange: $('#setExchange').value,
          data_source: $('#setDataSource').value,
          default_mode: $('#setMode').value,
        },
      });
      await refreshConfig();
      toast('设置已保存', 'success');
    } catch (e) { toast(e.message, 'error', 5000); }
  });

  $('#saveRisk').addEventListener('click', async () => {
    try {
      await api('/api/settings', { method: 'PUT', body: { risk_ack: $('#riskAck').checked } });
      await refreshConfig();
      fillSettings(state.cfg);
      toast(state.cfg.risk_ack ? '已确认实盘风险' : '已取消风险确认', state.cfg.risk_ack ? 'success' : 'warn');
    } catch (e) { toast(e.message, 'error'); }
  });

  $('#testPublic').addEventListener('click', async () => {
    const box = $('#probeBox');
    box.hidden = false;
    box.textContent = '正在检测公共接口…';
    try {
      const exchange = $('#setExchange').value;
      const res = await api(`/api/exchanges/probe?exchange=${encodeURIComponent(exchange)}`);
      box.textContent = res.ok
        ? `✓ 公共接口可用 · 延迟 ${res.latency_ms}ms · ETH 最新价 ${priceFmt(res.price)}`
        : `✗ 无法连接：${res.error || '未知错误'}`;
      box.style.color = res.ok ? '#2fd07f' : '#ff8b6c';
    } catch (e) {
      box.textContent = `✗ ${e.message}`;
      box.style.color = '#ff8b6c';
    }
  });

  $('#setExchange').addEventListener('change', () => {
    const testnet = $('#modeSeg .seg[data-mode="testnet"]');
    if (state.cfg) state.cfg.exchange = $('#setExchange').value;
  });
}

/* ---------------- init ---------------- */
function setDefaultDates() {
  const today = new Date();
  const end = isoDate(today);
  const start = '2024-01-01';
  $('#btStart').value = start;
  $('#btEnd').value = end;
  $('#dsStart').value = start;
  $('#dsEnd').value = end;
}

async function init() {
  bind();
  setDefaultDates();
  setMode('paper');

  try {
    const [symbols, cfg] = await Promise.all([api('/api/symbols'), api('/api/config')]);
    state.symbols = symbols.map((s) => ({ value: s.value, label: s.label }));
    state.cfg = cfg;
    fillSymbols();
    applyConfig(cfg);
    $('#btCapital').value = cfg.strategy.initial_capital;
    $('#cfgExchange').value = cfg.exchange || 'binance';
    syncModeAvailability();
    setMode(cfg.default_mode === 'testnet' && cfg.exchange !== 'binance' ? 'paper' : (cfg.default_mode || 'paper'));
    $('#setMode').value = cfg.default_mode || 'paper';
  } catch (e) {
    toast(`初始化失败：${e.message}`, 'error', 8000);
  }

  await loadPresets();
  loadInventory();
  loadLogs();

  try {
    const last = await api('/api/backtest/last');
    if (last.exists) renderBacktest(last.result);
  } catch (e) { /* ignore */ }

  try {
    const snap = await api('/api/live/status');
    renderLive(snap);
  } catch (e) { /* ignore */ }

  connectSSE();

  // SSE 断开时兜底轮询
  setInterval(async () => {
    if (state.sseOpen) return;
    try {
      const snap = await api('/api/live/status');
      renderLive(snap);
    } catch (e) { /* ignore */ }
  }, 5000);

  renderStrategyPreview();
}

document.addEventListener('DOMContentLoaded', init);
