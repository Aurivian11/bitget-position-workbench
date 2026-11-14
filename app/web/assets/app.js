/* Bitget 持仓工作台 · Light Glass UI (read-only dashboard + notification settings) */
(() => {
"use strict";

/* ================= helpers ================= */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const ICONS = {
  overview:'<rect x="3.5" y="3.5" width="7" height="8.5" rx="2"/><rect x="13.5" y="3.5" width="7" height="5" rx="2"/><rect x="13.5" y="11.5" width="7" height="9" rx="2"/><rect x="3.5" y="15" width="7" height="5.5" rx="2"/>',
  layers:'<path d="M12 3 2.8 7.6 12 12.2l9.2-4.6L12 3Z"/><path d="m2.8 12 9.2 4.6 9.2-4.6"/><path d="m2.8 16.4 9.2 4.6 9.2-4.6"/>',
  receipt:'<path d="M5.5 3h13v18l-2.6-1.7-2.2 1.7-2.2-1.7-2.2 1.7-2.2-1.7L5.5 21V3Z"/><path d="M9 8h6M9 12h6M9 16h3.5"/>',
  bell:'<path d="M6 9a6 6 0 1 1 12 0c0 6.5 2.5 8 2.5 8h-17S6 15.5 6 9"/><path d="M10.2 20.5a2 2 0 0 0 3.6 0"/>',
  bellRing:'<path d="M6 9a6 6 0 1 1 12 0c0 6.5 2.5 8 2.5 8h-17S6 15.5 6 9"/><path d="M10.2 20.5a2 2 0 0 0 3.6 0"/><path d="M2.5 8.5A9.5 9.5 0 0 1 5 3.5M21.5 8.5A9.5 9.5 0 0 0 19 3.5"/>',
  sliders:'<path d="M4 6.5h9M17 6.5h3M4 12h3M11 12h9M4 17.5h11M19 17.5h1"/><circle cx="15" cy="6.5" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17.5" r="2"/>',
  logout:'<path d="M9.5 20.5H6a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2h3.5"/><path d="m15.5 16.5 4.5-4.5-4.5-4.5"/><path d="M20 12H9.5"/>',
  refresh:'<path d="M20.5 12a8.5 8.5 0 0 1-14.8 5.7L3.5 15.5"/><path d="M3.5 20.5v-5h5"/><path d="M3.5 12a8.5 8.5 0 0 1 14.8-5.7l2.2 2.2"/><path d="M20.5 3.5v5h-5"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
  send:'<path d="M21.5 2.5 10.8 13.2"/><path d="M21.5 2.5 14.8 21.5l-4-8.3-8.3-4 19-6.7Z"/>',
  message:'<path d="M20.5 14.5a2 2 0 0 1-2 2H8l-4.5 4v-15a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2v9Z"/><path d="M8 9h8M8 12.5h5"/>',
  arrowUpRight:'<path d="M7 17 17 7M8.5 7H17v8.5"/>',
  arrowRight:'<path d="M5 12h14M13 6l6 6-6 6"/>',
  check:'<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 16.5v-5M12 8h.01"/>',
  alert:'<path d="M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9.5v4M12 17h.01"/>',
  chevLeft:'<path d="m14.5 18-6-6 6-6"/>',
  chevRight:'<path d="m9.5 18 6-6-6-6"/>',
  wifiOff:'<path d="m3 3 18 18"/><path d="M8.5 16.5a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 4.6-2.6M19 12.9a10 10 0 0 0-1.9-1.5M2 8.8a15 15 0 0 1 4-2.5M22 8.8a15 15 0 0 0-11.3-3.7M12 20h.01"/>',
  inbox:'<path d="M21.5 12.5h-6l-2 3h-3l-2-3h-6"/><path d="M5.6 5h12.8l3.1 7.5V18a2 2 0 0 1-2 2h-15a2 2 0 0 1-2-2v-5.5L5.6 5Z"/>',
  trend:'<path d="m3 16.5 6-6 4 4 8-8"/><path d="M14.5 6.5H21V13"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  minus:'<path d="M5 12h14"/>',
  close:'<path d="M18 6 6 18M6 6l12 12"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  clock:'<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  shield:'<path d="M12 3 4.5 6v6c0 4.6 3.2 7.8 7.5 9 4.3-1.2 7.5-4.4 7.5-9V6L12 3Z"/><path d="m9 12 2 2 4-4"/>',
  book:'<path d="M4 19.5V5a2 2 0 0 1 2-2h13.5v15H6a2 2 0 0 0-2 2Zm0 0A2 2 0 0 0 6 21.5h13.5"/>',
};
const icon = (name, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICONS[name] || ""}</svg>`;
const hydrate = (root = document) => $$("i[data-icon]", root).forEach(i => { i.insertAdjacentHTML("afterend", icon(i.dataset.icon, i.className)); i.remove(); });

const num = v => {
  if (v === null || v === undefined || typeof v === "boolean") return null;
  const s = String(v).replace(/[,%\s]/g, "").replace("−", "-");
  if (s === "" || s === "-" || s === "—") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};
const fmt = (v, dp = 2, sign = false) => {
  const n = num(v); if (n === null) return "—";
  const s = Math.abs(n).toLocaleString("en-US", {minimumFractionDigits: dp, maximumFractionDigits: dp});
  if (Number(s.replace(/,/g, "")) === 0) return s;
  return (n < 0 ? "−" : sign ? "+" : "") + s;
};
const tone = v => { const n = num(v); return n === null || Math.abs(n) < 1e-9 ? "flat" : n > 0 ? "up" : "down"; };
const pct = v => { const n = num(v); return n === null ? "—" : fmt(n, 2, true) + "%"; };
const px = v => {
  const n = num(v); if (n === null || n === 0 && String(v).trim() === "0") return n === 0 ? "0" : "—";
  const decimals = Math.min(8, ((String(v).split(".")[1] || "").replace(/\D/g, "")).length);
  return n.toLocaleString("en-US", {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
};
const qty = v => { const n = num(v); return n === null ? "—" : n.toLocaleString("en-US", {maximumFractionDigits: 8}); };
const txt = v => (v === null || v === undefined || v === "" || v === "-") ? "—" : String(v);
const TZ = "Asia/Shanghai";
const F_DT = new Intl.DateTimeFormat("zh-CN", {timeZone: TZ, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
const F_CLOCK = new Intl.DateTimeFormat("zh-CN", {timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"});
const F_DAY = new Intl.DateTimeFormat("zh-CN", {timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit"});
const F_LONG = new Intl.DateTimeFormat("zh-CN", {timeZone: TZ, month: "long", day: "numeric", weekday: "long"});
const F_HOUR = new Intl.DateTimeFormat("en-US", {timeZone: TZ, hour: "numeric", hourCycle: "h23"});
const toDate = (v, seconds = false) => {
  if (v === null || v === undefined || v === "") return null;
  const d = new Date(seconds ? Number(v) * 1000 : v);
  return Number.isNaN(d.getTime()) ? null : d;
};
const when = (v, s) => { const d = toDate(v, s); return d ? F_DT.format(d).replace(/\//g, "-") : "—"; };
const clock = (v, s) => { const d = toDate(v, s); return d ? F_CLOCK.format(d) : "—"; };
const dayOf = v => { const d = toDate(v); return d ? F_DAY.format(d).replace(/\//g, "-") : "—"; };
const sideOf = raw => raw === "long" ? {cls: "long", label: "做多"} : raw === "short" ? {cls: "short", label: "做空"} : {cls: "soft", label: "—"};
const modeOf = m => ({crossed: "全仓", cross: "全仓", isolated: "逐仓", fixed: "逐仓"})[String(m || "").toLowerCase()] || null;
const PRODUCTS = {"USDT-FUTURES": "USDT 合约", "USDC-FUTURES": "USDC 合约", "COIN-FUTURES": "币本位合约", "CFD": "CFD"};
const productOf = p => PRODUCTS[p] || p || "";
const empty = (ico, title, body) => `<div class="empty"><span class="empty-ico">${icon(ico)}</span><strong>${esc(title)}</strong>${body ? `<span>${esc(body)}</span>` : ""}</div>`;
const banner = (kind, ico, message) => `<div class="banner glass ${kind}">${icon(ico)}<span>${esc(message)}</span></div>`;

/* ================= state & api ================= */
const S = {
  tab: "overview", data: null, live: null, liveAt: "", lastOk: 0, failures: 0, leaving: false,
  dashError: null, posKey: "", chart: null, coin: null,
  hist: {q: "", side: "all", result: "all", page: 1},
  events: [], status: null, nError: null, nBusy: false, nTimer: 0, ev: {q: "", type: "all", page: 1},
  settings: null, csrf: null, dirty: false, setBusy: false, saving: false, applying: false, authEpoch: 0,
};

class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
async function api(path, body) {
  const options = {cache: "no-store", credentials: "same-origin", headers: {"Accept": "application/json"}};
  if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) options.signal = AbortSignal.timeout(20000);
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    if (S.csrf) options.headers["X-CSRF-Token"] = S.csrf;
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch("api/" + path, options); }
  catch (_) { throw new ApiError("网络连接异常，请稍后重试。", 0); }
  if (response.status === 401) { expire(); throw new ApiError("登录已过期，请重新登录。", 401); }
  let data;
  try { data = await response.json(); } catch (_) { throw new ApiError("服务暂时没有响应，请稍后重试。", response.status); }
  if (!response.ok) throw new ApiError(data.error || data.message || "请求未完成，请稍后重试。", response.status);
  return data;
}
function expire(reason = "expired") {
  if (S.leaving) return;
  S.leaving = true; S.dirty = false;
  $$('input[type="password"]').forEach(i => { i.value = ""; });
  location.replace("./?reason=" + reason);
}
let toastTimer = 0;
function toast(message) {
  const t = $("#toast");
  t.textContent = message; t.hidden = false;
  requestAnimationFrame(() => t.classList.add("show"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.classList.remove("show"); setTimeout(() => { t.hidden = true; }, 300); }, 3000);
}

/* ================= routing ================= */
const TABS = ["overview", "positions", "history", "notifications", "settings"];
const ALIASES = {account: "overview", dashboard: "overview", ledger: "history", performance: "history", guide: "settings"};
const TITLES = {overview: "总览", positions: "持仓", history: "交易记录", notifications: "通知中心", settings: "通知设置"};
function route() {
  const raw = location.hash.replace(/^#/, "").split("-")[0];
  const tab = TABS.includes(raw) ? raw : ALIASES[raw] || "overview";
  const changed = S.tab !== tab;
  S.tab = tab;
  $$(".view").forEach(v => v.classList.toggle("active", v.dataset.view === tab));
  $$("[data-tab]").forEach(a => {
    const on = a.dataset.tab === tab;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  moveIndicator();
  document.title = TITLES[tab] + " · Bitget 持仓工作台";
  if (changed) window.scrollTo({top: 0, behavior: "instant"});
  if (tab === "notifications") loadNotifications();
  if (tab === "settings") loadSettings();
  if (tab === "overview") requestAnimationFrame(renderCurve);
  scheduleLive(changed ? 0 : undefined);
}
function moveIndicator() {
  const nav = $("#tabs"), active = nav && $("a.active", nav), bar = nav && $(".tab-indicator", nav);
  if (!bar) return;
  if (!active || !active.offsetWidth) { bar.style.width = "0px"; return; }
  bar.style.width = active.offsetWidth + "px";
  bar.style.transform = `translateX(${active.offsetLeft}px)`;
}

/* ================= data loading ================= */
let dashTimer = 0, liveTimer = 0, dashBusy = false, liveBusy = false, dashStale = false;
async function loadDashboard(manual = false) {
  if (dashBusy || S.leaving) return;
  dashBusy = true;
  const btn = $("#refresh-btn");
  if (manual) btn.classList.add("spinning");
  try {
    const data = await api("dashboard");
    S.data = data; S.dashError = null; S.failures = 0;
    applyLive(data);
    S.lastOk = Date.now();
    if (manual) toast("数据已刷新");
  } catch (err) {
    if (err.status === 401) return;
    S.dashError = err.message;
    if (manual) toast(err.message);
  } finally {
    dashBusy = false;
    btn.classList.remove("spinning");
    renderAll();
    clearTimeout(dashTimer);
    const every = Math.max(15, num(S.data?.refreshSeconds) || 30) * 1000;
    dashTimer = setTimeout(() => { if (document.hidden) dashStale = true; else loadDashboard(); }, every);
  }
}
function applyLive(payload) {
  if (!payload || !Array.isArray(payload.positions)) return false;
  const at = payload.positionsUpdatedAt || payload.generatedAt || "";
  if (S.liveAt && at && at < S.liveAt) return false;
  S.live = {
    positions: payload.positions, currentTotals: payload.currentTotals || [],
    monitorHealthy: payload.monitorHealthy, monitorAgeSeconds: payload.monitorAgeSeconds,
    quotesHealthy: payload.quotesHealthy, accountMode: payload.accountMode, generatedAt: payload.generatedAt,
  };
  S.liveAt = at;
  return true;
}
function liveDelay() {
  if (document.hidden || S.leaving) return null;
  if (S.failures) return Math.min(10000, 1000 * 2 ** Math.min(S.failures - 1, 4));
  const fast = S.tab === "overview" || S.tab === "positions";
  const cfd = (S.live?.positions || []).some(p => p.productType === "CFD");
  return fast ? (cfd ? 2000 : 1000) : 5000;
}
function scheduleLive(delay) {
  clearTimeout(liveTimer);
  const d = delay ?? liveDelay();
  if (d === null) return;
  liveTimer = setTimeout(loadLive, d);
}
async function loadLive() {
  if (liveBusy || S.leaving) return;
  if (!S.data) { scheduleLive(1000); return; }
  liveBusy = true;
  try {
    const payload = await api("positions/live");
    S.failures = 0; S.lastOk = Date.now();
    if (applyLive(payload)) renderLive();
  } catch (err) {
    if (err.status === 401) return;
    S.failures++;
  } finally {
    liveBusy = false;
    updateChip();
    scheduleLive();
  }
}
function updateChip() {
  const chip = $("#live-chip"), label = $("#live-text");
  let cls = "", text = "连接中";
  if (S.data || S.live) {
    const age = Date.now() - S.lastOk;
    if (S.failures >= 3 || age > 45000) { cls = "err"; text = "连接中断"; }
    else if (S.live && S.live.monitorHealthy === false) { cls = "warn"; text = "监控延迟"; }
    else { cls = "ok"; text = "实时"; }
    if (S.liveAt && cls !== "err") text += `<span class="lbl-time"> · ${clock(S.liveAt)}</span>`;
  } else if (S.dashError) { cls = "err"; text = "读取失败"; }
  chip.className = "live-chip " + cls;
  if (label.innerHTML !== text) label.innerHTML = text;
}

/* ================= overview ================= */
function greeting() {
  const h = Number(F_HOUR.format(new Date()));
  return h < 5 ? "夜深了" : h < 11 ? "早上好" : h < 13 ? "中午好" : h < 18 ? "下午好" : "晚上好";
}
function coins() {
  const d = S.data; if (!d) return [];
  const list = [], add = c => { if (c && !list.includes(c)) list.push(c); };
  add(d.stats?.coin);
  (d.accounts || []).forEach(a => add(a.coin));
  (d.history || []).forEach(h => add(h.coin));
  return list;
}
function isCfdCoin(coin = S.coin) {
  const accounts = (S.data?.accounts || []).filter(a => a.coin === coin);
  if (accounts.length) return accounts.every(a => a.productType === "CFD");
  const rows = (S.data?.history || []).filter(h => h.coin === coin);
  return rows.length > 0 && rows.every(h => h.productType === "CFD");
}
const acctLabel = c => c + (isCfdCoin(c) ? " · CFD" : " · 合约");
function pickCoin() {
  const list = coins();
  if (S.coin && list.includes(S.coin)) return;
  let want = null;
  const param = new URLSearchParams(location.search).get("account");
  if (param && param.toUpperCase() === "CFD") want = list.find(c => isCfdCoin(c)) || null;
  S.coin = want && list.includes(want) ? want : (S.data?.stats?.coin || list[0] || null);
}
function setCoin(coin) {
  if (!coin || coin === S.coin || !coins().includes(coin)) return;
  S.coin = coin; S.hist.page = 1;
  renderAll();
}
function renderSwitch() {
  const list = coins();
  const html = list.length > 1 ? `<div class="seg acct-seg" role="group" aria-label="切换账户">${list.map(c =>
    `<button type="button" data-coin="${esc(c)}" class="${c === S.coin ? "on" : ""}" aria-pressed="${c === S.coin}">${esc(acctLabel(c))}</button>`).join("")}</div>` : "";
  ["#ov-acct", "#his-acct"].forEach(sel => { const el = $(sel); if (el && el.innerHTML !== html) el.innerHTML = html; });
}
function statsFor(coin) {
  const d = S.data || {};
  if (d.stats && d.stats.coin === coin) return d.stats;
  const rows = (d.history || []).filter(h => h.coin === coin);
  let net = 0, profit = 0, loss = 0, wins = 0;
  rows.forEach(h => { const n = num(h.netPnl); if (n === null) return; net += n; if (n > 0) { profit += n; wins++; } else if (n < 0) loss -= n; });
  const trades = rows.length;
  return {coin, netProfit: net, grossProfit: profit, grossLoss: loss, wins, trades,
    winRate: trades ? wins * 100 / trades : 0, averageNet: trades ? net / trades : 0,
    profitFactor: loss > 0 ? (profit / loss).toFixed(2) : profit > 0 ? "∞" : "—"};
}
function accountSummary() {
  const d = S.data; if (!d) return null;
  const coin = S.coin || d.stats?.coin || "USDT";
  const accounts = (d.accounts || []);
  const own = accounts.filter(a => a.coin === coin);
  const sum = key => own.length ? own.reduce((s, a) => { const n = num(a[key]); return s === null || n === null ? null : s + n; }, 0) : null;
  return {coin, equity: sum("accountEquity"), available: sum("available"), locked: sum("locked"), upnl: sum("unrealizedPnl"), others: accounts.filter(a => a.coin !== coin)};
}
function totalsFor(coin) { return (S.live?.currentTotals || []).find(t => t.coin === coin) || null; }
function renderOverview() {
  $("#h-overview").textContent = greeting();
  $("#ov-sub").textContent = F_LONG.format(new Date()) + " · 账户数据每 30 秒同步，持仓价格每秒刷新";
  const d = S.data;
  if (!d) {
    if (S.dashError) $("#ov-hero").innerHTML = empty("wifiOff", "暂时无法读取账户", S.dashError);
    return;
  }
  const a = accountSummary(), totals = totalsFor(a.coin), positions = S.live?.positions || [];
  const equity = fmt(a.equity, 2), [intPart, decPart] = equity.split(".");
  $("#ov-hero").innerHTML = `
    <div class="card-head"><span class="eyebrow">账户权益 · ${esc(a.coin)}</span><span class="pill soft">${isCfdCoin(a.coin) ? "CFD 账户" : S.live?.accountMode === "unified" ? "统一账户" : "经典账户"}</span></div>
    <div class="big"><span>${esc(intPart)}</span>${decPart !== undefined ? `<small>.${esc(decPart)}</small>` : ""}<em>${esc(a.coin)}</em></div>
    <div class="hero-sub" id="hero-sub">${heroSub(totals, positions.filter(p => p.coin === a.coin).length)}</div>
    <div class="metrics">
      <div class="metric"><label>可用余额</label><b>${fmt(a.available)}</b></div>
      <div class="metric"><label>冻结金额</label><b>${fmt(a.locked)}</b></div>
      <div class="metric"><label>未实现盈亏</label><b class="${tone(a.upnl)}">${fmt(a.upnl, 2, true)}</b></div>
      <div class="metric"><label>持仓保证金</label><b id="hero-margin">${fmt(totals?.margin)}</b></div>
    </div>
    ${a.others.length ? `<div class="acct-list">${a.others.map(o => `<span class="pill soft">${esc(productOf(o.productType))} · 权益 ${fmt(o.accountEquity)} ${esc(o.coin)}</span>`).join("")}</div>` : ""}
    ${d.accountError ? `<div class="inline-alert">${icon("alert")}${esc(d.accountError)}，显示的是上一次读取的余额</div>` : ""}`;

  const st = statsFor(a.coin), rate = Math.max(0, Math.min(100, num(st.winRate) || 0)), C = 2 * Math.PI * 52;
  $("#ov-perf").innerHTML = `
    <div class="card-head"><h2 class="card-title">${icon("trend")}交易表现</h2><span class="sub">自 ${esc(d.historyStart || "—")}</span></div>
    <div class="perf-body">
      <div class="ring-box">
        <svg viewBox="0 0 128 128" aria-hidden="true"><defs><linearGradient id="ring-g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7470fa"/><stop offset="1" stop-color="#3fb2f2"/></linearGradient></defs>
          <circle class="track" cx="64" cy="64" r="52"/><circle class="bar" cx="64" cy="64" r="52" stroke="url(#ring-g)" stroke-dasharray="${(C * rate / 100).toFixed(2)} ${C.toFixed(2)}"/></svg>
        <div class="ring-label"><b>${fmt(rate, 1)}%</b><span>胜率</span></div>
      </div>
      <dl class="perf-list">
        <div><dt>净收益</dt><dd class="${tone(st.netProfit)}">${fmt(st.netProfit, 2, true)}</dd></div>
        <div><dt>盈亏比</dt><dd>${esc(txt(st.profitFactor))}</dd></div>
        <div><dt>交易笔数</dt><dd>${esc(st.wins ?? 0)} / ${esc(st.trades ?? 0)}</dd></div>
        <div><dt>平均每笔</dt><dd class="${tone(st.averageNet)}">${fmt(st.averageNet, 2, true)}</dd></div>
      </dl>
    </div>`;
  renderCurve();
  renderOverviewLive();
  renderOverviewHistory();
}
function heroSub(totals, count) {
  if (!count) return `<span>当前没有持仓</span>`;
  return `<span class="pill ${tone(totals?.netPnl)}">持仓净盈亏 ${fmt(totals?.netPnl, 2, true)}</span><span>共 ${count} 个持仓</span>`;
}
function renderOverviewLive() {
  if (!S.data) return;
  const positions = [...(S.live?.positions || [])];
  const a = accountSummary(), totals = totalsFor(a.coin);
  const sub = $("#hero-sub"); if (sub) sub.innerHTML = heroSub(totals, positions.filter(p => p.coin === a.coin).length);
  const margin = $("#hero-margin"); if (margin) margin.textContent = fmt(totals?.margin);
  positions.sort((x, y) => Math.abs(num(y.netPnl) || 0) - Math.abs(num(x.netPnl) || 0));
  $("#ov-pos").innerHTML = `
    <div class="card-head"><h2 class="card-title">${icon("layers")}当前持仓</h2><a class="link" href="#positions">全部持仓${icon("arrowUpRight")}</a></div>
    ${positions.length ? `<div class="mini-list">${positions.slice(0, 5).map(p => {
      const s = sideOf(p.sideRaw), lev = num(p.leverage);
      return `<a class="mini-row" href="#positions"><div class="mini-sym"><strong>${esc(p.symbol)}</strong><span class="pill ${s.cls}">${s.label}${lev ? " " + lev + "x" : ""}</span></div><div class="mini-mid">${p.productType === "CFD" ? "参考" : "标记"} ${px(p.markPrice)}</div><div class="mini-val ${tone(p.netPnl)}">${fmt(p.netPnl, 2, true)}<small>${pct(p.netRoi)}</small></div></a>`;
    }).join("")}</div>` : empty("layers", "当前没有持仓", "开仓后将在这里实时显示盈亏。")}`;
}
function sortedHistory() {
  return [...(S.data?.history || [])].sort((a, b) => (toDate(b.closedAt)?.getTime() || 0) - (toDate(a.closedAt)?.getTime() || 0));
}
function renderOverviewHistory() {
  const rows = sortedHistory().filter(h => h.coin === S.coin).slice(0, 5);
  $("#ov-his").innerHTML = `
    <div class="card-head"><h2 class="card-title">${icon("receipt")}最近平仓</h2><a class="link" href="#history">全部记录${icon("arrowUpRight")}</a></div>
    ${rows.length ? `<div class="mini-list">${rows.map(h => {
      const s = sideOf(h.sideRaw);
      return `<a class="mini-row" href="#history"><div class="mini-sym"><strong>${esc(h.symbol)}</strong><span class="pill ${s.cls}">${s.label}</span></div><div class="mini-mid">${when(h.closedAt)}</div><div class="mini-val ${tone(h.netPnl)}">${fmt(h.netPnl, 2, true)}<small>${esc(h.coin)}</small></div></a>`;
    }).join("")}</div>` : empty("receipt", "暂无平仓记录", "统计起点之后的平仓交易会显示在这里。")}`;
}

/* ---- cumulative net curve ---- */
function renderCurve() {
  const box = $("#ov-curve"); if (!box || !S.data) return;
  const coin = S.coin || S.data.stats?.coin;
  const rows = sortedHistory().reverse().filter(h => h.coin === coin && toDate(h.closedAt) && num(h.netPnl) !== null);
  let acc = 0;
  const pts = [{v: 0, h: null}, ...rows.map(h => ({v: (acc += num(h.netPnl)), h}))];
  const final = pts[pts.length - 1].v;
  const head = `<div class="card-head"><h2 class="card-title">${icon("trend")}累计净收益</h2><span class="curve-value ${tone(final)}">${fmt(final, 2, true)} <span class="sub">${esc(coin || "")}</span></span></div>`;
  if (rows.length < 1) { box.innerHTML = head + empty("trend", "还没有可绘制的收益曲线", "新的交易平仓后，这里会按时间累计净收益。"); S.chart = null; return; }
  const W = 1000, H = 210, P = 12;
  let min = Math.min(...pts.map(p => p.v)), max = Math.max(...pts.map(p => p.v));
  if (max - min < 1e-9) { max += 1; min -= 1; }
  const pad = (max - min) * 0.12; min -= pad; max += pad;
  const x = i => P + (W - 2 * P) * (i / (pts.length - 1));
  const y = v => P + (H - 2 * P) * (1 - (v - min) / (max - min));
  const line = pts.map((p, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p.v).toFixed(1)).join(" ");
  const area = `${line} L${x(pts.length - 1).toFixed(1)} ${H} L${x(0).toFixed(1)} ${H} Z`;
  const color = final >= 0 ? "#5b5bd6" : "#e5484d";
  box.innerHTML = head + `
    <div class="chart" id="chart">
      <svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-label="累计净收益曲线" role="img">
        <defs>
          <linearGradient id="cv-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".24"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient>
          <linearGradient id="cv-line" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="${final >= 0 ? "#8a84ff" : "#f08a8d"}"/><stop offset="1" stop-color="${final >= 0 ? "#3f9cf4" : "#e5484d"}"/></linearGradient>
        </defs>
        <line class="zero" x1="${P}" x2="${W - P}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>
        <path d="${area}" fill="url(#cv-fill)"/>
        <path class="line" d="${line}" stroke="url(#cv-line)"/>
      </svg>
      <span class="chart-dot" id="chart-dot"></span><div class="chart-tip" id="chart-tip"></div>
    </div>
    <div class="chart-foot"><span>${dayOf(rows[0].closedAt)}</span><span>${rows.length} 笔平仓</span><span>${dayOf(rows[rows.length - 1].closedAt)}</span></div>`;
  S.chart = {pts, W, H, x, y, color};
  const chart = $("#chart");
  chart.addEventListener("pointermove", chartHover);
  chart.addEventListener("pointerdown", chartHover);
  chart.addEventListener("pointerleave", () => chart.classList.remove("hover"));
}
function chartHover(e) {
  const c = S.chart, chart = e.currentTarget; if (!c) return;
  const r = chart.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  const i = Math.round(ratio * (c.pts.length - 1)), p = c.pts[i];
  const left = c.x(i) / c.W * r.width, top = c.y(p.v) / c.H * r.height;
  const dot = $("#chart-dot"), tip = $("#chart-tip");
  dot.style.left = left + "px"; dot.style.top = top + "px"; dot.style.borderColor = c.color;
  tip.innerHTML = p.h
    ? `${esc(when(p.h.closedAt))} · <strong>${esc(p.h.symbol)}</strong> <span class="${tone(p.h.netPnl)}">${fmt(p.h.netPnl, 2, true)}</span><br>累计 <b class="${tone(p.v)}">${fmt(p.v, 2, true)}</b>`
    : `统计起点<br>累计 <b>0.00</b>`;
  const tw = tip.offsetWidth || 160;
  tip.style.left = Math.max(tw / 2, Math.min(r.width - tw / 2, left)) + "px";
  tip.style.top = Math.max(0, top - tip.offsetHeight - 14) + "px";
  chart.classList.add("hover");
}

/* ================= positions ================= */
function posFields(p) {
  const unit = p.quantityUnit ? " " + p.quantityUnit : "";
  return {
    net: [fmt(p.netPnl, 2, true), tone(p.netPnl)], roi: [pct(p.netRoi), tone(p.netRoi)],
    open: [px(p.openPrice)], mark: [px(p.markPrice)],
    size: [qty(p.size) + unit], margin: [fmt(p.margin)],
    upnl: [fmt(p.unrealizedPnl, 2, true), tone(p.unrealizedPnl)],
    fee: [fmt(p.transactionFee, 2, true)], funding: [fmt(p.fundingFee, 2, true), tone(p.fundingFee)],
    rebate: [p.productType === "CFD" ? "—" : fmt(p.rebate, 2, true)], tp: [px(p.takeProfit)], sl: [px(p.stopLoss)],
    hold: [txt(p.holdingTime)], quote: [p.quoteMode === "snapshot" ? "账户快照" : p.quoteHealthy ? "实时标记价" : "行情延迟"],
  };
}
const posId = p => [p.positionId, p.symbol, p.sideRaw, p.productType].join(":");
function posCard(p) {
  const f = posFields(p), s = sideOf(p.sideRaw), lev = num(p.leverage), mode = modeOf(p.marginMode), cfd = p.productType === "CFD";
  const v = k => `<span data-f="${k}" class="${f[k][1] || ""}">${esc(f[k][0])}</span>`;
  return `<article class="glass card pos-card ${s.cls}" data-id="${esc(posId(p))}">
    <header class="pos-head"><div class="pos-sym"><strong>${esc(p.symbol)}</strong><span class="pill ${s.cls}">${s.label}${lev ? " · " + lev + "x" : ""}</span>${mode ? `<span class="pill soft">${mode}</span>` : ""}</div><span class="pos-hold">${icon("clock")}${v("hold")}</span></header>
    <div class="pos-pnl"><div><label>净盈亏 · ${esc(p.coin)}</label><b>${v("net")}</b></div><div class="pos-roi">${v("roi")}<small>净收益率</small></div></div>
    <div class="pos-prices"><div><label>开仓均价</label>${v("open")}</div><span class="pos-arrow">${icon("arrowRight")}</span><div><label>${cfd ? "平仓参考价" : "标记价格"}</label>${v("mark")}</div></div>
    <dl class="kv">
      <div><dt>持仓数量</dt><dd>${v("size")}</dd></div><div><dt>保证金</dt><dd>${v("margin")}</dd></div><div><dt>未实现盈亏</dt><dd>${v("upnl")}</dd></div>
      <div><dt>手续费</dt><dd>${v("fee")}</dd></div><div><dt>${cfd ? "隔夜利息" : "资金费"}</dt><dd>${v("funding")}</dd></div><div><dt>预估返佣</dt><dd>${v("rebate")}</dd></div>
      <div><dt>止盈</dt><dd>${v("tp")}</dd></div><div><dt>止损</dt><dd>${v("sl")}</dd></div><div><dt>价格来源</dt><dd>${v("quote")}</dd></div>
    </dl>
    <footer class="pos-foot">${cfd ? "记录于" : "开仓于"} ${esc(when(p.openedAt))}${p.productType ? " · " + esc(productOf(p.productType)) : ""}</footer>
  </article>`;
}
function patchCard(card, p) {
  const f = posFields(p);
  $$("[data-f]", card).forEach(el => {
    const [value, cls] = f[el.dataset.f] || ["—"];
    if (el.textContent !== value) {
      if (el.dataset.f === "mark") {
        const before = num(el.textContent), after = num(value);
        el.classList.remove("tick-up", "tick-down"); void el.offsetWidth;
        if (before !== null && after !== null && before !== after) el.classList.add(after > before ? "tick-up" : "tick-down");
      }
      el.textContent = value;
    }
    if (cls !== undefined) ["up", "down", "flat"].forEach(c => el.classList.toggle(c, c === cls));
  });
}
function renderPositions() {
  const grid = $("#pos-grid");
  if (!S.live) {
    if (S.dashError) { grid.innerHTML = `<div class="glass card">${empty("wifiOff", "暂时无法读取持仓", S.dashError)}</div>`; S.posKey = "error"; }
    return;
  }
  const list = S.live.positions;
  $("#pos-totals").innerHTML = (S.live.currentTotals || []).map(t =>
    `<div class="total-chip glass"><label>持仓净盈亏 · ${esc(t.coin)}</label><b class="${tone(t.netPnl)}">${fmt(t.netPnl, 2, true)}</b></div><div class="total-chip glass"><label>占用保证金 · ${esc(t.coin)}</label><b>${fmt(t.margin)}</b></div>`).join("");
  const key = list.map(p => posId(p) + ":" + p.leverage + ":" + p.marginMode).join("|") || "none";
  if (key !== S.posKey) {
    grid.innerHTML = list.length ? list.map(posCard).join("") : `<div class="glass card empty-wrap">${empty("layers", "当前没有持仓", "开仓后，这里会实时显示价格、盈亏与费用。")}</div>`;
    S.posKey = key;
    return;
  }
  const cards = new Map($$(".pos-card", grid).map(c => [c.dataset.id, c]));
  list.forEach(p => { const card = cards.get(posId(p)); if (card) patchCard(card, p); });
}
function renderLive() { renderPositions(); renderOverviewLive(); }

/* ================= history ================= */
const HIST_PAGE = 12;
function filteredHistory() {
  const q = S.hist.q.trim().toUpperCase();
  return sortedHistory().filter(h => h.coin === S.coin &&
    (!q || String(h.symbol).toUpperCase().includes(q)) &&
    (S.hist.side === "all" || h.sideRaw === S.hist.side) &&
    (S.hist.result === "all" || (S.hist.result === "win" ? num(h.netPnl) > 0 : num(h.netPnl) < 0)));
}
function renderHistory() {
  const d = S.data;
  if (!d) {
    if (S.dashError) $("#his-table").innerHTML = empty("wifiOff", "暂时无法读取交易记录", S.dashError);
    return;
  }
  const coin = S.coin || d.stats?.coin || "", st = statsFor(coin), cfd = isCfdCoin(coin);
  $("#his-sub").textContent = `统计自 ${d.historyStart || "—"} 起 · 最多显示最近 100 笔平仓 · 当前账户 ${acctLabel(coin)}`;
  $("#his-alert").innerHTML = d.historyError ? banner("warn", "alert", d.historyError + "，下方为上一次读取的记录。") : "";
  $("#his-stats").innerHTML = `
    <div class="stat glass"><label>净收益</label><b class="${tone(st.netProfit)}">${fmt(st.netProfit, 2, true)}</b><small>${esc(coin)}</small></div>
    <div class="stat glass"><label>胜率</label><b>${fmt(st.winRate, 2)}%</b><small>${esc(st.wins ?? 0)} 笔盈利 / ${esc(st.trades ?? 0)} 笔</small></div>
    <div class="stat glass"><label>盈亏比</label><b>${esc(txt(st.profitFactor))}</b><small>盈 ${fmt(st.grossProfit)} · 亏 ${fmt(st.grossLoss)}</small></div>
    <div class="stat glass"><label>交易笔数</label><b>${esc(st.trades ?? 0)}</b><small>已平仓</small></div>
    <div class="stat glass"><label>平均每笔</label><b class="${tone(st.averageNet)}">${fmt(st.averageNet, 2, true)}</b><small>${esc(coin)}</small></div>`;
  const rows = filteredHistory(), total = (d.history || []).filter(h => h.coin === coin).length;
  const pages = Math.max(1, Math.ceil(rows.length / HIST_PAGE));
  S.hist.page = Math.min(Math.max(1, S.hist.page), pages);
  const slice = rows.slice((S.hist.page - 1) * HIST_PAGE, S.hist.page * HIST_PAGE);
  if (!rows.length) {
    $("#his-table").innerHTML = total
      ? empty("search", "没有符合条件的记录", "调整合约关键词或筛选条件后再试。")
      : empty("receipt", "暂无平仓记录", `从 ${d.historyStart || "统计起点"} 起平仓的交易会显示在这里。`);
  } else {
    $("#his-table").innerHTML = `<table class="ledger"><thead><tr>
      <th>合约</th><th>数量</th><th>开仓 → 平仓</th><th class="hide-md">持仓时长</th><th class="r">费用</th><th class="r hide-md">返佣</th><th class="r">净收益</th><th class="r">${cfd ? "记录时间" : "平仓时间"}</th>
    </tr></thead><tbody>${slice.map(h => {
      const s = sideOf(h.sideRaw), unit = h.quantityUnit ? " " + h.quantityUnit : "";
      return `<tr>
        <td class="cell-sym" data-label="合约"><div class="sym-cell"><strong>${esc(h.symbol)}</strong><span class="pill ${s.cls}">${s.label}</span></div><span class="sub">${esc(productOf(h.productType))}</span></td>
        <td data-label="数量">${qty(h.size)}${esc(unit)}</td>
        <td data-label="开仓 → 平仓"><span class="price-pair">${px(h.openPrice)}${icon("arrowRight")}${px(h.closePrice)}</span><span class="sub">价差盈亏 <span class="${tone(h.pnl)}">${fmt(h.pnl, 2, true)}</span></span></td>
        <td class="hide-md" data-label="持仓时长">${esc(txt(h.holdingTime))}</td>
        <td class="r" data-label="费用">${fmt(h.fees, 2, true)}<span class="sub">手续费 ${fmt(h.transactionFee, 2, true)} · ${h.productType === "CFD" ? "隔夜利息" : "资金费"} ${fmt(h.fundingFee, 2, true)}</span></td>
        <td class="r hide-md" data-label="返佣">${h.productType === "CFD" ? "—" : fmt(h.rebate, 2, true)}</td>
        <td class="r" data-label="净收益"><b class="${tone(h.netPnl)}">${fmt(h.netPnl, 2, true)}</b><span class="sub">${esc(h.coin)}</span></td>
        <td class="r" data-label="平仓时间">${esc(when(h.closedAt))}</td>
      </tr>`; }).join("")}</tbody></table>`;
  }
  $("#his-pager").innerHTML = `<span>${rows.length === total ? `共 ${total} 笔` : `筛选出 ${rows.length} / ${total} 笔`}</span>
    <div class="btns"><button class="icon-btn" type="button" data-page="his:-1" aria-label="上一页" ${S.hist.page <= 1 ? "disabled" : ""}>${icon("chevLeft")}</button><span>${S.hist.page} / ${pages}</span><button class="icon-btn" type="button" data-page="his:1" aria-label="下一页" ${S.hist.page >= pages ? "disabled" : ""}>${icon("chevRight")}</button></div>`;
}

/* ================= notifications ================= */
const CHANNELS = {telegram: {name: "Telegram", icon: "send"}, discord: {name: "Discord", icon: "message"}, bark: {name: "Bark", icon: "bellRing"}};
const EVENT_NAMES = {opened: "开仓", increased: "加仓", reduced: "减仓", closed: "平仓", updated: "参数变更"};
const EVENT_ICONS = {opened: "plus", increased: "plus", reduced: "minus", closed: "check", updated: "gear"};
const DELIVERY = {processed: "已处理", pending: "待处理", skipped: "已跳过", unknown: "状态未知"};
const EV_PAGE = 10;
async function loadNotifications(manual = false) {
  if (S.nBusy || S.leaving) return;
  S.nBusy = true;
  const btn = $("#ev-refresh"); btn.disabled = true; btn.classList.add("spinning");
  try {
    const [status, events] = await Promise.all([api("notification-status"), api("events")]);
    S.status = status; S.events = Array.isArray(events.events) ? events.events : []; S.nError = null;
    if (manual) toast("通知状态已刷新");
  } catch (err) {
    if (err.status === 401) return;
    S.nError = err.message;
  } finally {
    S.nBusy = false; btn.disabled = false; btn.classList.remove("spinning");
    renderNotifications();
    clearTimeout(S.nTimer);
    S.nTimer = setTimeout(() => { if (S.tab === "notifications" && !document.hidden) loadNotifications(); }, 30000);
  }
}
function renderNotifications() {
  const st = S.status;
  let note = "";
  if (S.nError) note = banner("warn", "wifiOff", "刷新失败：" + S.nError + (st ? " 当前显示上一次的结果。" : ""));
  else if (st) note = st.monitorHealthy
    ? banner("ok", "check", "持仓监控同步正常 · 最近确认 " + clock(st.monitorHeartbeatAt, true))
    : banner("warn", "alert", "尚未确认最新的持仓同步，请稍后刷新查看。");
  $("#n-banner").innerHTML = note;
  if (st) {
    $("#ch-grid").innerHTML = Object.entries(CHANNELS).map(([key, meta]) => {
      const c = st.channels?.[key] || {};
      const [cls, label] = !c.enabled ? ["off", "已停用"] : !c.configured ? ["wait", "待补充凭据"] : ["on", "已启用"];
      const foot = c.uncertain ? `${c.uncertain} 条结果待核对` : c.lastAcceptedAt ? "最近推送确认 " + when(c.lastAcceptedAt, true) : key === "bark" ? "暂无成功推送记录" : "启用不等于消息已送达设备";
      return `<article class="glass card"><div class="ch-top"><span class="ch-ico ${key}">${icon(meta.icon)}</span><div><strong>${meta.name}</strong><span class="sub">${key === "bark" ? "开仓成交提醒" : "持仓卡片与交易事件"}</span></div><span class="badge ${cls}">${label}</span></div>
        <div class="counts"><div><b>${esc(c.pending ?? "—")}</b><span>待处理</span></div><div><b>${esc(c.failed ?? "—")}</b><span>异常</span></div><div><b>${esc(c.skipped ?? "—")}</b><span>已跳过</span></div></div>
        <p class="ch-foot">${esc(foot)}</p></article>`;
    }).join("");
  } else if (S.nError) {
    $("#ch-grid").innerHTML = `<div class="glass card">${empty("wifiOff", "通知状态暂时不可用", "稍后点击刷新重试。")}</div>`;
  }
  renderEvents();
}
function renderEvents() {
  const q = S.ev.q.trim().toUpperCase(), type = S.ev.type;
  const rows = S.events.filter(e => (!q || String(e.symbol).toUpperCase().includes(q)) && (type === "all" || e.type === type));
  const pages = Math.max(1, Math.ceil(rows.length / EV_PAGE));
  S.ev.page = Math.min(Math.max(1, S.ev.page), pages);
  $("#ev-count").textContent = rows.length === S.events.length ? `共 ${S.events.length} 条` : `筛选出 ${rows.length} / ${S.events.length} 条`;
  if (!S.status && !S.nError) return;
  $("#ev-list").innerHTML = rows.length ? rows.slice((S.ev.page - 1) * EV_PAGE, S.ev.page * EV_PAGE).map(e => {
    const side = e.side === "long" ? "做多" : e.side === "short" ? "做空" : "—";
    const state = DELIVERY[e.telegramState] ? e.telegramState : "unknown";
    return `<div class="ev"><span class="ev-ico ${EVENT_NAMES[e.type] ? esc(e.type) : "other"}">${icon(EVENT_ICONS[e.type] || "info")}</span>
      <div class="ev-main"><strong>${esc(e.symbol)}</strong><span class="sub">${side} · 数量 ${esc(txt(e.size))}</span></div>
      <span class="ev-type">${esc(EVENT_NAMES[e.type] || e.type)}</span>
      <div class="ev-meta"><time>${esc(when(e.createdAt))}</time><span class="ev-state ${state}">Telegram ${DELIVERY[state]}</span></div></div>`;
  }).join("") : (S.events.length ? empty("search", "没有符合条件的事件", "调整合约或事件类型后再试。") : empty("inbox", "暂无持仓事件", "监控捕捉到开仓、加减仓或平仓后，会在这里记录。"));
  $("#ev-pager").innerHTML = `<span>按监控记录时间倒序</span><div class="btns"><button class="icon-btn" type="button" data-page="ev:-1" aria-label="上一页" ${S.ev.page <= 1 ? "disabled" : ""}>${icon("chevLeft")}</button><span>${S.ev.page} / ${pages}</span><button class="icon-btn" type="button" data-page="ev:1" aria-label="下一页" ${S.ev.page >= pages ? "disabled" : ""}>${icon("chevRight")}</button></div>`;
}

/* ================= settings ================= */
const GUIDES = {
  telegram: ["在 Telegram 搜索 <b>@BotFather</b>，发送 <code>/newbot</code> 创建机器人，复制它给出的 Token。", "把机器人拉进目标群组，或先私聊它发送任意一条消息。", "浏览器打开 <code>https://api.telegram.org/bot&lt;Token&gt;/getUpdates</code>，返回内容里 <code>chat.id</code> 就是 Chat ID（群组通常以 -100 开头）。"],
  discord: ["打开目标频道的「编辑频道 → 整合 → Webhook」。", "新建 Webhook，选择频道后点击「复制 Webhook 网址」。", "粘贴到上方地址栏，保存后即可接收持仓卡片与交易事件。"],
  bark: ["在 iPhone 安装 Bark App 并允许通知。", "打开 App，复制示例链接中 <code>https://api.day.app/</code> 后面的那段字符，即设备 Key。", "如果使用自建 Bark 服务器，把推送服务器改为你的服务器根地址。"],
};
function field(ch, key, label, value, hint, o = {}) {
  const id = `f-${ch}-${key}`, secret = !!o.secret;
  const attrs = o.type === "number" ? `type="number" inputmode="decimal" min="${o.min}" max="3600" step="${o.step || 1}" required` : `type="${secret ? "password" : "text"}"`;
  const placeholder = secret ? (o.configured ? "已保存，留空则不修改" : "填写" + label) : (o.placeholder || "");
  return `<div class="field"><label for="${id}">${esc(label)}${secret ? `<span class="tag ${o.configured ? "ok" : ""}">${o.configured ? "● 已配置" : "未配置"}</span>` : ""}</label>
    <input class="input" id="${id}" data-ch="${ch}" data-k="${key}" ${attrs} value="${esc(secret ? "" : value ?? "")}" maxlength="${secret ? 2048 : 256}" autocomplete="${secret ? "new-password" : "off"}" spellcheck="false" placeholder="${esc(placeholder)}">
    ${hint ? `<p class="hint">${hint}</p>` : ""}</div>`;
}
const toggle = (ch, key, on, label) => `<label class="switch"><input type="checkbox" data-ch="${ch}" data-k="${key}" ${on ? "checked" : ""} aria-label="${esc(label)}"><span></span></label>`;
function settingsCard(key) {
  const c = S.settings.channels[key], meta = CHANNELS[key];
  let body = "";
  if (key === "telegram") {
    body = field(key, "token", "Bot Token", "", "只有更换机器人时才需要重新填写。", {secret: true, configured: c.token_configured})
      + field(key, "chat_id", "Chat ID", c.chat_id, "数字 ID 或 @频道名；群组 ID 通常为负数。", {placeholder: "-1001234567890"});
  } else if (key === "discord") {
    body = field(key, "webhook", "Webhook 地址", "", "目标频道的完整 Webhook URL，已保存的地址不会回显。", {secret: true, configured: c.webhook_configured});
  } else {
    body = field(key, "base", "推送服务器", c.base, "服务器根地址，不含设备 Key。", {placeholder: "https://api.day.app"})
      + field(key, "key", "设备 Key", "", "与所选服务器对应的设备 Key。", {secret: true, configured: c.key_configured})
      + field(key, "sound", "提示音", c.sound, "Bark 支持的铃声名称。", {placeholder: "minuet"})
      + field(key, "group", "通知分组", c.group, "在 Bark 历史里归类这些消息。")
      + field(key, "reconcile_seconds", "成交核对间隔（秒）", c.reconcile_seconds, "定期补查遗漏的成交，范围 5–3600。", {type: "number", min: 5})
      + `<div class="set-row"><div><strong>持续响铃</strong><p>使用 Bark 的 call 选项，实际效果取决于手机通知权限。</p></div>${toggle(key, "call", c.call, "Bark 持续响铃")}</div>`;
  }
  if (key !== "bark") {
    body += `<div class="set-row"><div><strong>实时持仓卡片</strong><p>开仓后建立卡片，并持续更新价格与盈亏。</p></div>${toggle(key, "board_enabled", c.board_enabled, meta.name + " 实时持仓卡片")}</div>
      <div class="field"><label>交易提醒</label><div class="checks">${["increased", "reduced", "closed"].map(t =>
        `<label class="check"><input type="checkbox" data-ch="${key}" data-ev="${t}" ${(c.events || []).includes(t) ? "checked" : ""}><span>${EVENT_NAMES[t]}</span></label>`).join("")}</div>
      <p class="hint">开仓与参数变更由持仓卡片展示，这里控制额外的加仓、减仓、平仓消息。</p></div>`
      + field(key, "update_seconds", "卡片刷新间隔（秒）", c.update_seconds, "范围 1–3600；网络耗时和平台限流会让实际间隔更长。", {type: "number", min: 1});
  }
  return `<article class="glass card set-card">
    <div class="ch-top"><span class="ch-ico ${key}">${icon(meta.icon)}</span><div><strong>${meta.name}</strong><span class="sub">${key === "bark" ? "开仓成交 · 手机提醒" : "持仓卡片 · 交易事件"}</span></div>${toggle(key, "enabled", c.enabled, "启用 " + meta.name)}</div>
    ${body}
    <details class="guide"><summary>${icon("chevRight")}如何获取配置</summary><ol>${GUIDES[key].map(g => `<li>${g}</li>`).join("")}</ol></details>
  </article>`;
}
function setNotice(kind, message) {
  $("#set-notice").innerHTML = message ? banner(kind, kind === "ok" ? "check" : kind === "err" ? "alert" : "info", message) : "";
}
function setMeta(data) {
  const applied = data.appliedRevision === data.revision && !data.applying;
  const fresh = data.monitorHeartbeatAt && Date.now() / 1000 - data.monitorHeartbeatAt < 90;
  $("#set-meta").textContent = `配置版本 v${data.revision} · ` + (applied ? (fresh ? "监控已加载" : "已加载，等待同步确认") : "等待监控加载");
}
function setFormBusy(busy) {
  $$("#set-form input, #set-form button").forEach(el => { el.disabled = busy; });
  if (!busy) $("#set-save").disabled = !S.dirty;
}
function updateSavebar() {
  const show = S.dirty || S.applying || S.saving;
  $("#savebar").classList.toggle("show", show);
  $("#save-title").textContent = S.applying ? "正在等待监控加载新配置…" : S.saving ? "正在保存…" : "有未保存的更改";
}
function renderSettings() {
  if (!S.settings) return;
  $("#set-grid").innerHTML = ["telegram", "discord", "bark"].map(settingsCard).join("");
  $("#set-note").textContent = S.settings.semantics || "";
  S.dirty = false;
  setMeta(S.settings);
  setFormBusy(S.setBusy || S.applying);
  updateSavebar();
}
async function loadSettings(force = false) {
  if (S.setBusy || S.applying || S.leaving) return;
  if (S.settings && S.dirty && !force) return;
  S.setBusy = true;
  const epoch = S.authEpoch;
  try {
    const session = await api("admin/session");
    if (!session.authenticated) { expire(); return; }
    S.csrf = session.csrfToken || null;
    const data = await api("admin/settings");
    if (epoch !== S.authEpoch) return;
    S.settings = data;
    S.setBusy = false;
    renderSettings();
    if (force) toast("已读取服务器上的最新配置");
  } catch (err) {
    if (err.status === 401) return;
    setNotice("err", err.message);
    if (!S.settings) $("#set-grid").innerHTML = `<div class="glass card">${empty("shield", "暂时无法读取通知设置", err.message)}</div>`;
  } finally { S.setBusy = false; updateSavebar(); }
}
function collect() {
  const channels = {telegram: {events: []}, discord: {events: []}, bark: {}};
  $$("#set-grid [data-k]").forEach(input => {
    channels[input.dataset.ch][input.dataset.k] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value.trim();
  });
  $$("#set-grid [data-ev]:checked").forEach(input => channels[input.dataset.ch].events.push(input.dataset.ev));
  return {revision: S.settings.revision, channels};
}
async function waitApplied(revision) {
  S.applying = true; updateSavebar();
  const started = Date.now();
  try {
    while (Date.now() - started < 90000) {
      await new Promise(r => setTimeout(r, 2500));
      if (S.leaving) return;
      try {
        const data = await api("admin/settings");
        setMeta(data);
        if (data.revision !== revision) { setNotice("warn", "配置已被另一个页面更新，请点击「撤销」重新读取后核对。"); return; }
        if (data.appliedRevision === revision && !data.applying) { S.settings = data; setNotice("ok", "配置已生效，监控已完成重新加载。"); toast("配置已生效"); return; }
      } catch (err) { if (err.status === 401) return; }
    }
    setNotice("warn", "配置已保存，但监控仍未确认加载。请稍后刷新本页查看状态。");
  } finally { S.applying = false; setFormBusy(false); updateSavebar(); }
}

/* ================= events ================= */
function bind() {
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", () => { moveIndicator(); if (S.tab === "overview") renderCurve(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (dashStale) { dashStale = false; loadDashboard(); }
    scheduleLive(0);
    if (S.tab === "notifications") loadNotifications();
  });
  window.addEventListener("pageshow", e => { if (e.persisted) api("admin/session").then(s => { if (!s.authenticated) expire(); }).catch(() => {}); });
  $("#refresh-btn").addEventListener("click", () => { loadDashboard(true); if (S.tab === "notifications") loadNotifications(); });
  $("#logout-btn").addEventListener("click", async () => {
    if (S.dirty && !confirm("还有未保存的设置，确定要退出吗？")) return;
    try {
      if (!S.csrf) { const session = await api("admin/session"); S.csrf = session.csrfToken || null; }
      await api("admin/logout", {});
      S.dirty = false; expire("signed-out");
    } catch (err) { if (err.status !== 401) toast(err.message); }
  });
  $("#his-q").addEventListener("input", e => { S.hist.q = e.target.value; S.hist.page = 1; renderHistory(); });
  $$(".seg").forEach(seg => seg.addEventListener("click", e => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    $$("button", seg).forEach(x => x.classList.toggle("on", x === b));
    S.hist[seg.dataset.seg] = b.dataset.v; S.hist.page = 1; renderHistory();
  }));
  $("#ev-q").addEventListener("input", e => { S.ev.q = e.target.value; S.ev.page = 1; renderEvents(); });
  $("#ev-type").addEventListener("change", e => { S.ev.type = e.target.value; S.ev.page = 1; renderEvents(); });
  $("#ev-refresh").addEventListener("click", () => loadNotifications(true));
  document.addEventListener("click", e => {
    const acct = e.target.closest("[data-coin]");
    if (acct) { setCoin(acct.dataset.coin); return; }
    const b = e.target.closest("[data-page]"); if (!b || b.disabled) return;
    const [which, step] = b.dataset.page.split(":");
    if (which === "his") { S.hist.page += Number(step); renderHistory(); $("#his-table").scrollIntoView({block: "nearest", behavior: "smooth"}); }
    else { S.ev.page += Number(step); renderEvents(); }
  });
  const form = $("#set-form");
  form.addEventListener("input", () => { if (S.setBusy || S.applying || !S.settings) return; S.dirty = true; $("#set-save").disabled = false; updateSavebar(); });
  $("#set-reload").addEventListener("click", () => { if (S.setBusy || S.applying) return; S.dirty = false; setNotice(); loadSettings(true); });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    if (S.setBusy || S.applying || !S.settings || !S.dirty) return;
    if (!form.reportValidity()) return;
    S.setBusy = true; S.saving = true; setFormBusy(true); updateSavebar();
    const previous = S.settings.revision;
    try {
      const data = await api("admin/settings", collect());
      S.settings = data; S.setBusy = false; S.saving = false;
      renderSettings(); setFormBusy(true);
      if (data.revision === previous && !data.applying) { setNotice("ok", "当前设置已经是最新，无需重新加载监控。"); setFormBusy(false); }
      else { setNotice("info", "配置已保存，正在等待监控重新连接并加载…"); await waitApplied(data.revision); }
    } catch (err) {
      if (err.status === 401) return;
      setNotice("err", err.status === 409 ? "配置已在其他页面修改。请点击「撤销」重新读取后再编辑，以免覆盖。" : err.message);
      S.setBusy = false; setFormBusy(false);
    } finally { S.setBusy = false; S.saving = false; updateSavebar(); }
  });
  window.addEventListener("beforeunload", e => { if (S.dirty && !S.leaving) { e.preventDefault(); e.returnValue = ""; } });
}
function renderAll() { pickCoin(); renderSwitch(); renderOverview(); renderPositions(); renderHistory(); updateChip(); }

/* ================= boot ================= */
hydrate();
bind();
route();
loadDashboard();
setInterval(updateChip, 1000);
if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveIndicator);
})();
