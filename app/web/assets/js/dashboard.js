"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  data: null,
  coin: "USDT",
  initialAccountApplied: false,
  range: "all",
  query: "",
  side: "all",
  result: "all",
  sort: "closedAt",
  direction: -1,
  page: 1,
  pageSize: 10,
  loading: false,
  nextRefresh: Infinity,
  clockBase: Date.now(),
  clockStarted: performance.now(),
  fetchFailed: false,
  chartPoints: [],
  chartScope: "",
  chartWindow: null,
  hoverIndex: -1,
  filtered: [],
  focusReturn: null,
  bodyOverflow: "",
  positionSignature: null,
  drawerPositionKey: null,
};

const liveState = { loading: false, next: 0, failures: 0, failed: false, payload: null, controller: null, timer: null };

const hasNumber = (value) => (typeof value === "number" || typeof value === "string" && value.trim() !== "") && Number.isFinite(Number(value));
const numberValue = (value) => hasNumber(value) ? Number(value) : 0;
function positionTotal(positions, totals, field) {
  if (totals && Object.hasOwn(totals, field)) return hasNumber(totals[field]) ? Number(totals[field]) : null;
  return positions.every(item => hasNumber(item[field])) ? positions.reduce((sum, item) => sum + Number(item[field]), 0) : null;
}
function rebateText(value) { return hasNumber(value) ? "+" + formatNumber(Math.abs(Number(value)), 8) : "—"; }
const escapeHtml = (value) => String(value ?? "—").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

function formatNumber(value, digits = 4, fixed = false) {
  if (!hasNumber(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: fixed ? digits : 0,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function signed(value, digits = 4, fixed = false) {
  if (!hasNumber(value)) return "—";
  const amount = Number(value);
  return (amount > 0 ? "+" : amount < 0 ? "−" : "") + formatNumber(Math.abs(amount), digits, fixed);
}

function tone(value) {
  const amount = numberValue(value);
  return amount > 0 ? "positive" : amount < 0 ? "negative" : "neutral";
}

function serverNow() {
  return state.clockBase + performance.now() - state.clockStarted;
}

function formatDate(value, mode = "full") {
  if (value === null || value === undefined || value === "") return "—";
  const target = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(target.getTime())) return "—";
  const options = { timeZone: "Asia/Shanghai", hourCycle: "h23" };
  if (mode !== "time") Object.assign(options, { year: (mode === "full" || mode === "date") ? "numeric" : undefined, month: "2-digit", day: "2-digit" });
  if (mode !== "date") Object.assign(options, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return new Intl.DateTimeFormat("zh-CN", options).format(target);
}

function axisDate(value) {
  const target = new Date(value);
  if (!Number.isFinite(target.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit" }).format(target);
}

function positionDuration(openedAt, fallback = "—") {
  const opened = new Date(openedAt).getTime();
  if (!openedAt || !Number.isFinite(opened)) return fallback;
  const seconds = Math.max(0, Math.floor((serverNow() - opened) / 1000));
  const days = Math.floor(seconds / 86400);
  const parts = [Math.floor(seconds / 3600) % 24, Math.floor(seconds / 60) % 60, seconds % 60]
    .map((part) => String(part).padStart(2, "0")).join(":");
  return days ? days + "天 " + parts : parts;
}

function initializeIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function isCfd(item) { return item?.productType === "CFD"; }
function isCfdAccount(coin = state.coin, data = state.data) {
  return [data?.accounts, data?.positions, data?.history].some(rows => (rows || []).some(item => item.coin === coin && isCfd(item)));
}
function quantityText(item) { return formatNumber(item.size, 8) + (isCfd(item) ? " 手" : ""); }
function roiText(item) {
  return isCfd(item) && !hasNumber(item.margin) || item.netRoi === null || item.netRoi === undefined || item.netRoi === "" ? "—" : String(item.netRoi);
}
function rebateRate(item) { return isCfd(item) ? 0 : hasNumber(item?.feeRebateRate) ? Number(item.feeRebateRate) : numberValue(state.data?.rebateRate); }
function priceLabel(item) { return isCfd(item) ? "平仓参考价" : "标记价格"; }
function productLabel(item) { return (item.coin || state.coin) + (isCfd(item) ? " · CFD" : " 永续"); }
function accountLabel(coin, data = state.data) { return coin + (isCfdAccount(coin, data) ? " · CFD" : " · 合约"); }
function selectAccount(coin) {
  state.coin = coin;
  state.initialAccountApplied = true;
  state.page = 1;
  if ($("trade-drawer").open) closeDrawer();
  $("currency").value = coin;
  updateView();
  liveState.next = 0;
  loadLivePositions();
}
function syncAccountControls() {
  $("currency").value = state.coin;
  document.querySelectorAll("[data-account-coin]").forEach(button => {
    const selected = button.dataset.accountCoin === state.coin;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  const cfd = isCfdAccount();
  const rate = cfd ? 0 : numberValue(state.data?.rebateRate);
  $("rebate-rate").textContent = formatNumber(rate * 100, 0) + "%";
  $("rebate-note").hidden = cfd;
  $("cfd-net-note").hidden = !cfd;
  $("ledger-time-label").textContent = cfd ? "记录时间" : "平仓时间";
  $("ledger-order").querySelector('[value="latest"]').textContent = cfd ? "最近记录" : "最近平仓";
  $("ledger-order").querySelector('[value="earliest"]').textContent = cfd ? "最早记录" : "最早平仓";
}

function baseSymbol(symbol) {
  return String(symbol || "—").replace(/(USDT|USDC|USD)$/u, "");
}

function contractMarkup(item) {
  const base = isCfd(item) ? String(item.symbol || "—") : baseSymbol(item.symbol);
  const side = item.sideRaw === "long" ? "long" : "short";
  const letters = base.match(/[A-Za-z]/g) || Array.from(base);
  const avatar = base === "BTC" ? "₿" : base === "ETH" ? "Ξ" : letters.slice(0, 2).join("").toUpperCase();
  return '<div class="contract-identity">' +
    '<span class="coin-avatar" aria-hidden="true">' + escapeHtml(avatar) + '</span>' +
    '<div class="contract-info"><strong class="contract-name">' + escapeHtml(base) + '</strong>' +
    '<div class="contract-meta"><span>' + escapeHtml(productLabel(item)) + '</span>' +
    '<span class="side ' + side + '"><i data-lucide="' + (side === "long" ? "arrow-up-right" : "arrow-down-right") + '"></i>' +
    (side === "long" ? "做多" : "做空") + '</span></div></div></div>';
}

const DAY_MS = 86400000;

function historyTimestamp(item) {
  if (!item.closedAt || item.netPnl === null || item.netPnl === undefined || item.netPnl === "" || !Number.isFinite(Number(item.netPnl))) return NaN;
  return new Date(item.closedAt).getTime();
}

function chartWindow(data = state.data, coin = state.coin, range = state.range, now = serverNow()) {
  const currentTime = typeof now === "number" ? now : new Date(now).getTime();
  const endDay = shanghaiDay(currentTime);
  const endTime = new Date(endDay + "T00:00:00+08:00").getTime();
  let startTime = endTime;
  if (range === "all") {
    const configured = String(data?.historyStart || "");
    const configuredTime = new Date(/^\d{4}-\d{2}-\d{2}$/.test(configured)
      ? configured + "T00:00:00+08:00" : configured).getTime();
    if (Number.isFinite(configuredTime)) {
      startTime = Math.min(startTime, new Date(shanghaiDay(configuredTime) + "T00:00:00+08:00").getTime());
    }
    (data?.history || []).forEach((item) => {
      const time = historyTimestamp(item);
      if (item.coin === coin && Number.isFinite(time) && time <= currentTime) {
        startTime = Math.min(startTime, new Date(shanghaiDay(time) + "T00:00:00+08:00").getTime());
      }
    });
  } else {
    const days = range === "7" || range === 7 ? 7 : range === "30" || range === 30 ? 30 : 1;
    startTime = endTime - (days - 1) * DAY_MS;
  }
  return {
    coin, range, startDay: shanghaiDay(startTime), endDay, startTime, endTime,
    now: currentTime, dayCount: Math.round((endTime - startTime) / DAY_MS) + 1,
  };
}

function scopedHistory(window = chartWindow()) {
  return (state.data?.history || []).filter((item) => {
    const time = historyTimestamp(item);
    return item.coin === window.coin && Number.isFinite(time) &&
      time >= window.startTime && time < window.endTime + DAY_MS && time <= window.now;
  });
}

function buildDailyPoints(history, window = chartWindow()) {
  const grouped = new Map();
  history.forEach((item) => {
    const time = historyTimestamp(item);
    if (item.coin !== window.coin || !Number.isFinite(time) || time < window.startTime ||
        time >= window.endTime + DAY_MS || time > window.now) return;
    const day = shanghaiDay(time);
    const entry = grouped.get(day) || { daily: 0, count: 0 };
    entry.daily += numberValue(item.netPnl);
    entry.count += 1;
    grouped.set(day, entry);
  });
  const points = [];
  let value = 0;
  for (let time = window.startTime; time <= window.endTime; time += DAY_MS) {
    const day = shanghaiDay(time);
    const { daily, count } = grouped.get(day) || { daily: 0, count: 0 };
    value += daily;
    points.push({ day, time, daily, count, value });
  }
  return points;
}

function calculateStats(history) {
  let profit = 0;
  let loss = 0;
  let wins = 0;
  let losses = 0;
  let net = 0;
  history.forEach((item) => {
    const value = numberValue(item.netPnl);
    net += value;
    if (value > 0) { profit += value; wins += 1; }
    else if (value < 0) { loss += Math.abs(value); losses += 1; }
  });
  const factorValue = loss > 0 ? profit / loss : profit > 0 ? Infinity : null;
  return {
    profit, loss, wins, losses, flat: history.length - wins - losses, net,
    average: history.length ? net / history.length : 0,
    rate: history.length ? wins / history.length * 100 : 0,
    factorValue,
    factorLabel: factorValue === Infinity ? "无亏损" : factorValue === null ? "—" : formatNumber(factorValue, 2, true),
  };
}

function setAmount(id, value, digits = 2) {
  const element = $(id);
  element.textContent = signed(value, digits, true);
  element.classList.remove("positive", "negative", "neutral");
  element.classList.add(tone(value));
  element.title = signed(value, 8) + " " + state.coin;
  element.dataset.full = element.textContent;
  element.dataset.raw = String(value);
}

function fitNumbers() {
  document.querySelectorAll(".metric-value, .equity-value, .hero-stat > strong").forEach((element) => {
    element.style.fontSize = "";
    if (element.dataset.full) element.textContent = element.dataset.full;
    let size = parseFloat(getComputedStyle(element).fontSize);
    while (element.scrollWidth > element.clientWidth && size > 20) {
      size -= 1;
      element.style.fontSize = size + "px";
    }
    if (element.scrollWidth > element.clientWidth && element.dataset.raw && Math.abs(numberValue(element.dataset.raw)) >= 10000) {
      element.textContent = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 2 }).format(numberValue(element.dataset.raw));
    }
  });
}

function renderAccount(history, stats) {
  const accounts = state.data.accounts || [];
  const account = accounts.find((item) => item.coin === state.coin);
  const positions = (state.data.positions || []).filter((item) => item.coin === state.coin);
  const openTotal = (state.data.currentTotals || []).find((item) => item.coin === state.coin);

  $("equity").textContent = account ? formatNumber(account.accountEquity, 2, true) : "—";
  $("equity").dataset.full = $("equity").textContent;
  $("equity").dataset.raw = account ? String(account.accountEquity) : "";
  $("equity").title = account ? formatNumber(account.accountEquity, 8) + " " + state.coin : "";
  $("available").textContent = account ? formatNumber(account.available, 4) + " " + state.coin : "—";
  setAmount("net-profit", stats.net);
  setAmount("open-net", positionTotal(positions, openTotal, "netPnl"));
  if ($("position-net-summary")) {
    const positionNet = positionTotal(positions, openTotal, "netPnl");
    setAmount("position-net-summary", positionNet, 4);
  }
  if ($("position-margin-summary")) {
    const positionMargin = positionTotal(positions, openTotal, "margin");
    $("position-margin-summary").textContent = formatNumber(positionMargin, 4) + " " + state.coin;
    $("position-margin-summary").title = formatNumber(positionMargin, 8) + " " + state.coin;
  }
  $("profit-factor").textContent = stats.factorLabel;
  $("profit-factor").dataset.full = stats.factorLabel;
  $("factor-sample").textContent = history.length + " 笔交易样本";
  $("win-rate").textContent = formatNumber(stats.rate, 1, true) + "%";
  $("win-rate").dataset.full = $("win-rate").textContent;
  $("win-record").textContent = stats.wins + " 胜 / " + stats.losses + " 负" + (stats.flat ? " / " + stats.flat + " 平" : "");
  $("rebate-rate").textContent = formatNumber((isCfdAccount() ? 0 : numberValue(state.data.rebateRate)) * 100, 0) + "%";
  $("open-count").textContent = positions.length;
  $("nav-position-count").textContent = positions.length;
  $("nav-trade-count").textContent = history.length;
  $("position-heading-count").textContent = positions.length;
  if ($("account-product")) $("account-product").textContent = isCfdAccount() ? "CFD 账户 · " + state.coin : state.data.accountMode === "unified" ? "统一账户 · " + state.coin + " 资产" : state.coin + " 本位永续";
  if ($("equity-label")) $("equity-label").textContent = isCfdAccount() ? "CFD 账户权益" : state.data.accountMode === "unified" ? "币种资产权益" : "账户总权益";
  if ($("position-summary")) $("position-summary").textContent = positions.length ? "当前币种 " + positions.length + " 个持仓" : "当前未持仓";
  if ($("exposure-label")) $("exposure-label").textContent = positions.length ? "有持仓" : "空仓中";
  if ($("net-period-label")) $("net-period-label").textContent = state.range === "all" ? "累计净盈亏" : "区间净盈亏";
  if ($("metric-period")) $("metric-period").textContent = state.range === "all" ? "全部交易" : "近 " + state.range + " 天";
  const donut = $("win-donut");
  if (donut) {
    donut.style.setProperty("--win-angle", (stats.rate * 3.6) + "deg");
    const lossAngle = history.length ? stats.losses / history.length * 360 : 0;
    donut.style.setProperty("--loss-angle", (stats.rate * 3.6 + lossAngle) + "deg");
    donut.classList.toggle("empty", history.length === 0);
    donut.setAttribute("role", "img");
    donut.setAttribute("aria-label", history.length
      ? "胜率 " + formatNumber(stats.rate, 1) + "%，" + stats.wins + " 胜、" + stats.losses + " 负、" + stats.flat + " 平，共 " + history.length + " 笔"
      : "暂无已平仓交易样本");
  }
  $("trade-heading-count").textContent = history.length;
  document.querySelectorAll(".currency-label").forEach((element) => { element.textContent = state.coin; });
}

function renderScore(history, stats) {
  $("factor-large").textContent = stats.factorLabel;
  $("wins").textContent = stats.wins;
  $("losses").textContent = stats.losses;
  $("gross-profit").textContent = "+" + formatNumber(stats.profit, 2, true) + " " + state.coin;
  $("gross-profit").className = "positive";
  $("gross-loss").textContent = (stats.loss ? "−" : "") + formatNumber(stats.loss, 2, true) + " " + state.coin;
  $("gross-loss").className = stats.loss ? "negative" : "";
  setAmount("average", stats.average);

  const score = stats.factorValue === Infinity ? 2 : Math.max(0, Math.min(2, stats.factorValue || 0));
  const percent = score / 2 * 100;
  $("quality-fill").style.width = percent + "%";
  $("quality-marker").style.left = "calc(" + percent + "% - 1px)";
  let label = "暂无样本";
  if (history.length) {
    if (stats.factorValue === Infinity) label = "暂无亏损";
    else if (stats.factorValue === null) label = "暂无盈亏";
    else if (stats.factorValue >= 1) label = "盈利覆盖亏损";
    else label = "亏损高于盈利";
  }
  $("score-label").textContent = label;
}

function renderPositions() {
  const positions = (state.data.positions || []).filter((item) => item.coin === state.coin);
  $("position-empty").hidden = positions.length > 0;
  $("position-list").hidden = positions.length === 0;
  renderLiveStatus();

  const signature = JSON.stringify(positions.map((item) => [positionKey(item), item.symbol, item.sideRaw, item.coin, item.leverage, item.marginMode, item.openPrice, item.size, item.openedAt]));
  if (state.positionSignature === signature) {
    patchPositionValues(positions);
    return;
  }
  state.positionSignature = signature;
  $("position-list").innerHTML = positions.map((item, index) => {
    const marginMode = item.marginMode === "crossed" ? "全仓" : item.marginMode === "isolated" ? "逐仓" : item.marginMode;
    return '<article class="holding-card" data-position-key="' + escapeHtml(positionKey(item)) + '">' +
      '<div class="holding-head">' + contractMarkup(item) +
        '<div class="holding-badges"><span class="leverage-badge">' + escapeHtml(hasNumber(item.leverage) ? formatNumber(item.leverage, 2) + '×' : '—') + '</span><span class="margin-mode-badge">' + escapeHtml(marginMode) + '</span></div>' +
      '</div>' +
      '<div class="holding-pnl"><div><span class="holding-pnl-label">' + (isCfd(item) ? '持仓盈亏 · 含隔夜利息 · ' : '持仓净盈亏 · ') + escapeHtml(item.coin) + '</span>' +
        '<strong data-live-value="netPnl" class="holding-pnl-amount ' + tone(item.netPnl) + '" title="' + escapeHtml(signed(item.netPnl, 8)) + '">' + escapeHtml(signed(item.netPnl, 4)) + '</strong></div>' +
        '<div class="holding-roi"><span>净收益率</span><strong data-live-value="netRoi" class="' + tone(item.netPnl) + '">' + escapeHtml(roiText(item)) + '</strong></div>' +
      '</div>' +
      '<dl class="holding-facts">' +
        '<div class="holding-fact"><dt>开仓均价</dt><dd>' + escapeHtml(formatNumber(item.openPrice, 8)) + '</dd></div>' +
        '<div class="holding-fact"><dt>' + priceLabel(item) + '</dt><dd data-live-value="markPrice">' + escapeHtml(formatNumber(item.markPrice, 8)) + '</dd></div>' +
        '<div class="holding-fact"><dt>持仓数量</dt><dd>' + escapeHtml(quantityText(item)) + '</dd></div>' +
        '<div class="holding-fact"><dt>保证金 · ' + escapeHtml(item.coin) + '</dt><dd data-live-value="margin">' + escapeHtml(formatNumber(item.margin, 8)) + '</dd></div>' +
      '</dl>' +
      '<div class="holding-footer"><span class="holding-duration"><i data-lucide="timer"></i><span>' + (isCfd(item) ? (item.openedAt ? '记录时长 ' : '开仓记录时间未知 ') : '持仓 ') + '<b data-opened="' + escapeHtml(item.openedAt || "") + '" data-fallback="' + escapeHtml(item.holdingTime) + '">' + escapeHtml(positionDuration(item.openedAt, item.holdingTime)) + '</b></span></span>' +
        '<button type="button" class="holding-detail" data-position-detail="' + index + '" aria-label="查看 ' + escapeHtml(item.symbol) + ' 费用与风险详情">费用与风险<i data-lucide="arrow-up-right"></i></button></div>' +
    '</article>';
  }).join("");

  $("position-list").querySelectorAll("[data-position-detail]").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.closest(".holding-card").dataset.positionKey;
      const current = (state.data?.positions || []).find((item) => positionKey(item) === key);
      if (current) openDrawer(current, true, button);
    });
  });
  patchLiveDrawer(positions);
  initializeIcons();
}

function positionKey(item) {
  return [item.productType || "", item.positionId || "", item.symbol, item.coin, item.sideRaw, item.openedAt || ""].join(":");
}

function positionSnapshotTime(data) {
  return new Date(data?.positionsUpdatedAt || data?.generatedAt || 0).getTime() || 0;
}

function positionPayloadFields(data) {
  return Object.fromEntries(["positions", "currentTotals", "positionsUpdatedAt", "monitorUpdatedAt", "monitorAgeSeconds", "monitorHealthy", "quotesHealthy", "accountMode"].filter((key) => key in data).map((key) => [key, data[key]]));
}

function renderLiveStatus() {
  const positions = (state.data?.positions || []).filter((item) => item.coin === state.coin);
  const label = $("position-updated");
  label.title = "标记价格每秒同步；市场报价未变化时价格保持不变";
  if (!positions.length) {
    const age = serverNow() - new Date(state.data?.monitorUpdatedAt).getTime();
    const healthy = Boolean(state.data?.monitorHealthy) && Number.isFinite(age) && age <= 90000 && !liveState.failed && !state.fetchFailed;
    label.textContent = healthy ? "账户空仓" : "上次记录为空仓 · 等待最新同步";
    label.dataset.status = healthy ? "empty" : "stale";
    $("position-empty").querySelector("h3").textContent = healthy ? "当前暂无持仓" : "上次同步时暂无持仓";
    $("position-empty").querySelector("p").textContent = healthy ? "新持仓会自动显示在这里，盈亏与费用实时更新。" : "数据尚未更新，当前持仓状态需要等待监控恢复后确认。";
    return;
  }
  if (positions.every(item => item.quoteMode === "snapshot" || isCfd(item))) {
    const time = new Date(state.data.monitorUpdatedAt).getTime();
    const age = serverNow() - time;
    const healthy = Boolean(state.data.monitorHealthy) && Number.isFinite(age) && age <= 90000 && !liveState.failed;
    label.title = "CFD 持仓按监控快照同步，页面每 2 秒读取；盈亏采用交易所返回值";
    label.dataset.status = healthy ? "live" : "stale";
    label.textContent = healthy ? "持仓同步 · " + formatDate(time, "time") + " 更新"
      : liveState.failed ? "持仓连接中断 · 正在重试" : "持仓同步延迟 · 等待更新";
    return;
  }
  const times = positions.map((item) => new Date(item.quoteUpdatedAt).getTime());
  const oldest = Math.min(...times);
  const fresh = positions.every((item) => item.quoteHealthy === true) && Number.isFinite(oldest) && serverNow() - oldest < 10000;
  const monitorAge = serverNow() - new Date(state.data.monitorUpdatedAt).getTime();
  const healthy = fresh && !liveState.failed && monitorAge <= 90000;
  label.dataset.status = healthy ? "live" : "stale";
  label.textContent = healthy ? "实时行情 · " + formatDate(oldest, "time") + " 更新"
    : liveState.failed ? "行情连接中断 · 正在重试"
    : monitorAge > 90000 ? "持仓同步延迟 · 等待更新"
    : liveState.payload ? "行情延迟 · 正在重试" : "行情连接中 · 等待最新报价";
}

function patchLiveDrawer(positions) {
  if (!state.drawerPositionKey || !$("trade-drawer").open) return;
  const item = positions.find((row) => positionKey(row) === state.drawerPositionKey);
  if (!item) { closeDrawer(); return; }
  const signedFields = new Set(["unrealizedPnl", "realizedPnl", "transactionFee", "fundingFee"]);
  $("drawer-content").querySelectorAll("[data-live-detail]").forEach((element) => {
    const field = element.dataset.liveDetail;
    if (field === "netPnl") {
      element.textContent = signed(item.netPnl, 8);
      element.parentElement.className = tone(item.netPnl);
    } else if (signedFields.has(field)) {
      element.textContent = signed(item[field], 8) + " " + item.coin;
      element.className = tone(item[field]);
    } else if (field === "size") {
      element.textContent = quantityText(item);
    } else if (field === "netRoi") {
      element.textContent = roiText(item);
    } else {
      element.textContent = formatNumber(item[field], 8) + (field === "margin" ? " " + item.coin : "");
    }
  });
  const rebate = $("drawer-content").querySelector(".rebate-row dd");
  if (rebate) rebate.textContent = rebateText(item.rebate) + " " + item.coin;
}

function patchPositionValues(positions) {
  const byKey = new Map(positions.map((item) => [positionKey(item), item]));
  $("position-list").querySelectorAll(".holding-card").forEach((card) => {
    const item = byKey.get(card.dataset.positionKey);
    if (!item) return;
    card.querySelectorAll("[data-live-value]").forEach((element) => {
      const field = element.dataset.liveValue;
      element.textContent = field === "netPnl" ? signed(item.netPnl, 4) : field === "netRoi" ? roiText(item) : formatNumber(item[field], 8);
      if (field === "netPnl" || field === "netRoi") {
        element.classList.remove("positive", "negative", "neutral");
        element.classList.add(tone(item.netPnl));
        if (field === "netPnl") element.title = signed(item.netPnl, 8);
      }
    });
  });
  patchLiveDrawer(positions);
}

function renderPositionTotals() {
  const positions = (state.data?.positions || []).filter((item) => item.coin === state.coin);
  const totals = (state.data?.currentTotals || []).find((item) => item.coin === state.coin);
  const net = positionTotal(positions, totals, "netPnl");
  const margin = positionTotal(positions, totals, "margin");
  setAmount("open-net", net);
  setAmount("position-net-summary", net, 4);
  $("position-margin-summary").textContent = formatNumber(margin, 4) + " " + state.coin;
  $("position-margin-summary").title = formatNumber(margin, 8) + " " + state.coin;
  ["open-count", "nav-position-count", "position-heading-count"].forEach((id) => { $(id).textContent = positions.length; });
  $("position-summary").textContent = positions.length ? "当前币种 " + positions.length + " 个持仓" : "当前未持仓";
  $("exposure-label").textContent = positions.length ? "有持仓" : "空仓中";
}

async function loadLivePositions() {
  if (liveState.loading || !state.data || document.hidden || navigator.onLine === false) return;
  clearTimeout(liveState.timer);
  liveState.timer = null;
  liveState.loading = true;
  liveState.next = Date.now() + (isCfdAccount() ? 2000 : 1000);
  const controller = new AbortController();
  liveState.controller = controller;
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch("api/positions/live", { cache: "no-store", signal: controller.signal });
    if (window.BitgetSession?.check(response) === false) return;
    if (!response.ok) throw new Error("Live HTTP " + response.status);
    const data = await response.json();
    if (!Array.isArray(data.positions) || !Array.isArray(data.currentTotals) || !data.positionsUpdatedAt) throw new Error("Invalid live positions");
    liveState.failures = 0;
    liveState.failed = false;
    if (positionSnapshotTime(data) >= positionSnapshotTime(state.data)) {
      liveState.payload = data;
      Object.assign(state.data, positionPayloadFields(data));
      renderPositions();
      renderPositionTotals();
      fitNumbers();
    }
  } catch (error) {
    if (!document.hidden) {
      liveState.failed = true;
      liveState.failures += 1;
      liveState.next = Date.now() + Math.min(10000, 1000 * 2 ** Math.min(liveState.failures - 1, 4));
    }
  } finally {
    clearTimeout(timeout);
    liveState.loading = false;
    if (liveState.controller === controller) liveState.controller = null;
    renderLiveStatus();
    if (!document.hidden && navigator.onLine !== false && state.data) {
      liveState.timer = setTimeout(loadLivePositions, Math.max(25, liveState.next - Date.now()));
    }
  }
}

function ledgerHistory(window = chartWindow()) {
  return (state.data?.history || []).filter(item => {
    const time = item.closedAt ? new Date(item.closedAt).getTime() : NaN;
    return item.coin === window.coin && Number.isFinite(time) && time >= window.startTime && time < window.endTime + DAY_MS && time <= window.now;
  });
}

function filterHistory(history) {
  const query = state.query.toLowerCase();
  return history.filter((item) => {
    const queryMatch = !query || String(item.symbol).toLowerCase().includes(query);
    const sideMatch = state.side === "all" || item.sideRaw === state.side;
    const value = numberValue(item.netPnl);
    const resultMatch = state.result === "all" ||
      (state.result === "win" && value > 0) ||
      (state.result === "loss" && value < 0) ||
      (state.result === "flat" && hasNumber(item.netPnl) && value === 0);
    return queryMatch && sideMatch && resultMatch;
  });
}

function renderLedger(history) {
  const sorted = filterHistory(history).sort((a, b) => {
    if (state.sort === "netPnl" && hasNumber(a.netPnl) !== hasNumber(b.netPnl)) return hasNumber(a.netPnl) ? -1 : 1;
    const left = state.sort === "closedAt" ? new Date(a.closedAt).getTime() : numberValue(a.netPnl);
    const right = state.sort === "closedAt" ? new Date(b.closedAt).getTime() : numberValue(b.netPnl);
    return (left - right) * state.direction;
  });
  state.filtered = sorted;
  const pages = Math.max(1, Math.ceil(sorted.length / state.pageSize));
  state.page = Math.min(state.page, pages);
  const start = (state.page - 1) * state.pageSize;
  const visible = sorted.slice(start, start + state.pageSize);

  $("ledger-loading").hidden = true;
  document.querySelector(".ledger-table-wrap").hidden = sorted.length === 0;
  $("ledger-empty").hidden = sorted.length > 0;
  $("result-count").textContent = sorted.length + " 条记录";
  $("trades").innerHTML = visible.map((item, index) =>
    '<tr class="trade-row">' +
      '<td class="trade-contract" data-label="合约 / 方向">' + contractMarkup(item) + '</td>' +
      '<td class="trade-date" data-label="' + (isCfd(item) ? '记录时间' : '平仓时间') + '"><time class="cell-stack" datetime="' + escapeHtml(item.closedAt) + '"><strong>' + escapeHtml(formatDate(item.closedAt, "date")) + '</strong><small>' + escapeHtml(formatDate(item.closedAt, "time")) + '</small></time></td>' +
      '<td class="trade-size num" data-label="数量"><span>' + escapeHtml(quantityText(item)) + '</span></td>' +
      '<td class="trade-open-price num" data-label="开仓价格"><strong class="price-amount" title="' + escapeHtml(formatNumber(item.openPrice, 8)) + '">' + escapeHtml(formatNumber(item.openPrice, 8)) + '</strong></td>' +
      '<td class="trade-close-price num" data-label="平仓价格"><strong class="price-amount" title="' + escapeHtml(formatNumber(item.closePrice, 8)) + '">' + escapeHtml(formatNumber(item.closePrice, 8)) + '</strong></td>' +
      '<td class="trade-duration num" data-label="持仓时间"><span>' + escapeHtml(item.holdingTime) + '</span></td>' +
      '<td class="trade-fees num" data-label="手续费 / 返佣"><div class="cell-stack"><strong>' + escapeHtml(signed(item.transactionFee, 6)) + '</strong><small>返佣 ' + escapeHtml(signed(item.rebate, 6)) + '</small></div></td>' +
      '<td class="trade-net num" data-label="净盈亏"><div class="cell-stack"><strong class="' + tone(item.netPnl) + '" title="' + escapeHtml(signed(item.netPnl, 8)) + '">' + escapeHtml(signed(item.netPnl, 4)) + '</strong><small>' + escapeHtml(item.coin) + '</small></div></td>' +
      '<td class="trade-action" data-label="交易详情"><button type="button" class="trade-detail-button" data-trade-detail="' + (start + index) + '" aria-label="查看 ' + escapeHtml(item.symbol) + ' 交易详情"><span>详情</span><i data-lucide="arrow-up-right"></i></button></td>' +
    '</tr>'
  ).join("");

  $("page-copy").textContent = sorted.length
    ? "显示 " + (start + 1) + "–" + Math.min(start + state.pageSize, sorted.length) + "，共 " + sorted.length + " 笔"
    : "暂无交易";
  $("page").textContent = state.page + " / " + pages;
  $("prev").disabled = state.page === 1;
  $("next").disabled = state.page === pages;

  $("trades").querySelectorAll("[data-trade-detail]").forEach((button) => {
    button.addEventListener("click", () => openDrawer(sorted[Number(button.dataset.tradeDetail)], false, button));
  });
  initializeIcons();
}

function openDrawer(item, live, trigger) {
  state.focusReturn = trigger;
  state.drawerPositionKey = live ? positionKey(item) : null;
  const cfd = isCfd(item);
  const sideClass = item.sideRaw === "long" ? "long" : "short";
  const closedOrOpened = live ? item.openedAt : item.closedAt;
  const details = live
    ? [
      ["持仓数量", quantityText(item), "size"],
      ["开仓均价", formatNumber(item.openPrice, 8), "openPrice"],
      [priceLabel(item), formatNumber(item.markPrice, 8), "markPrice"],
      ["杠杆 / 模式", (hasNumber(item.leverage) ? formatNumber(item.leverage, 2) + "x" : "—") + " / " + (item.marginMode === "crossed" ? "全仓" : item.marginMode === "isolated" ? "逐仓" : item.marginMode || "—")],
      [cfd ? "记录间隔" : "持仓时间", positionDuration(item.openedAt, item.holdingTime), "duration"],
      ["保证金", formatNumber(item.margin, 8) + " " + item.coin, "margin"],
      ["净收益率", roiText(item), "netRoi"],
      ["止盈价格", formatNumber(item.takeProfit, 8), "takeProfit"],
      ["止损价格", formatNumber(item.stopLoss, 8), "stopLoss"],
    ]
    : [
      ["平仓数量", quantityText(item)],
      ["开仓均价", formatNumber(item.openPrice, 8)],
      ["平仓均价", formatNumber(item.closePrice, 8)],
      [cfd ? "记录间隔" : "持仓时间", item.holdingTime],
    ];
  const fees = live
    ? [
      ["未实现盈亏", item.unrealizedPnl, "unrealizedPnl"],
      ["已实现盈亏", item.realizedPnl, "realizedPnl"],
      ["交易手续费", item.transactionFee, "transactionFee"],
      [cfd ? "隔夜利息" : "资金费", item.fundingFee, "fundingFee"],
    ]
    : [
      ["已实现盈亏", item.pnl],
      ["交易手续费", item.transactionFee],
      [cfd ? "隔夜利息" : "资金费", item.fundingFee],
      ["交易所净盈亏", item.exchangeNetPnl],
    ];

  $("drawer-title").textContent = cfd ? (live ? "CFD 持仓详情" : "CFD 结算详情") : live ? "持仓详情" : "平仓详情";
  $("drawer-content").innerHTML =
    '<div class="drawer-symbol"><div><h3>' + escapeHtml(item.symbol) + '</h3><div class="contract-sub"><span>' + escapeHtml(productLabel(item)) + '</span><span class="side ' + sideClass + '"><i data-lucide="' + (sideClass === "long" ? "arrow-up-right" : "arrow-down-right") + '"></i>' + (sideClass === "long" ? "做多" : "做空") + '</span></div></div>' +
      '<div class="drawer-time">' + (cfd ? (live ? "开仓记录时间" : "记录时间") : live ? "开仓时间" : "平仓时间") + '<strong>' + escapeHtml(formatDate(closedOrOpened)) + '</strong></div></div>' +
    '<div class="drawer-net"><span>' + (cfd ? (live ? '持仓盈亏 · 含隔夜利息' : '结算净盈亏') : '扣费后净盈亏 · 含返佣') + '</span><strong class="' + tone(item.netPnl) + '">' + '<span data-live-detail="netPnl">' + escapeHtml(signed(item.netPnl, 8)) + '</span><small>' + escapeHtml(item.coin) + '</small></strong></div>' +
    '<div class="detail-columns"><section class="detail-section parameters-section"><h3>交易参数</h3><dl class="detail-grid">' +
      details.map(([label, value, kind]) => '<div><dt>' + escapeHtml(label) + '</dt><dd>' +
        (kind === "duration" ? '<span data-opened="' + escapeHtml(item.openedAt || "") + '" data-fallback="' +
          escapeHtml(item.holdingTime) + '">' + escapeHtml(value) + '</span>' : kind ? '<span data-live-detail="' + kind + '">' + escapeHtml(value) + '</span>' : escapeHtml(value)) +
        '</dd></div>').join("") +
    '</dl></section>' +
    '<section class="detail-section fees-section"><h3>费用核算</h3><dl>' +
      fees.map(([label, value, field]) => '<div class="detail-row"><dt>' + escapeHtml(label) + '</dt><dd' + (field ? ' data-live-detail="' + field + '"' : '') + ' class="' + tone(value) + '">' + escapeHtml(signed(value, 8)) + " " + escapeHtml(item.coin) + '</dd></div>').join("") +
      '<div class="detail-row rebate-row"><dt>手续费返佣（' + escapeHtml(formatNumber(rebateRate(item) * 100, 0)) + '%）</dt><dd>' + escapeHtml(rebateText(item.rebate)) + " " + escapeHtml(item.coin) + '</dd></div>' +
    '</dl></section></div>';

  initializeIcons();
  state.bodyOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  document.body.classList.add("dialog-open");
  $("trade-drawer").showModal();
}

function restoreDrawerFocus() {
  document.body.style.overflow = state.bodyOverflow;
  document.body.classList.remove("dialog-open");
  if (state.focusReturn?.isConnected) state.focusReturn.focus({ preventScroll: true });
  state.focusReturn = null;
  state.drawerPositionKey = null;
}

function closeDrawer() {
  $("trade-drawer").close();
}

function canvasContext(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  return { context, width: rect.width, height: rect.height };
}

function shanghaiDay(value) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(value));
  const get = (type) => parts.find((part) => part.type === type).value;
  return get("year") + "-" + get("month") + "-" + get("day");
}

function renderChart(history, window = chartWindow()) {
  const canvas = $("performance-chart");
  const content = $("chart-content");
  const scroller = canvas.closest(".chart-stage");
  const viewportWidth = scroller.clientWidth;
  const left = viewportWidth < 500 ? 43 : 52;
  const right = 26;
  const daily = buildDailyPoints(history, window);
  const hasHistory = daily.some((point) => point.count > 0);
  const contentWidth = Math.max(viewportWidth, hasHistory ? left + right + (daily.length - 1) * 44 : 0);
  const targetWidth = Math.ceil(contentWidth) + "px";
  if (content && content.style.width !== targetWidth) content.style.width = targetWidth;
  const scope = state.coin + ":" + state.range;
  if (state.chartScope !== scope) {
    state.chartScope = scope;
    scroller.scrollLeft = 0;
  }
  if ($("chart-scroll-note")) $("chart-scroll-note").hidden = contentWidth <= viewportWidth + 1;
  const { context: ctx, width, height } = canvasContext(canvas);
  if (width < 1 || height < 1) return;
  state.chartWindow = window;
  $("chart-empty").hidden = hasHistory;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  const top = 16;
  const lineBottom = Math.round(height * 0.65);
  const barTop = lineBottom + 26;
  const barBottom = height - 31;
  const barZero = (barTop + barBottom) / 2;
  const plotWidth = Math.max(1, width - left - right);
  const x = (index) => left + (daily.length === 1 ? plotWidth / 2 : index / (daily.length - 1) * plotWidth);
  let minimum = Math.min(0, ...daily.map((point) => point.value));
  let maximum = Math.max(0, ...daily.map((point) => point.value));
  const rawSpan = Math.max(maximum - minimum, 1);
  minimum -= rawSpan * 0.15;
  maximum += rawSpan * 0.15;
  const y = (value) => top + (maximum - value) / (maximum - minimum) * (lineBottom - top);
  const axisNumber = (value) => Math.abs(value) >= 100000
    ? new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value)
    : formatNumber(value, 1);
  state.chartPoints = hasHistory ? daily.map((point, index) => ({ ...point, x: x(index), y: y(point.value) })) : [];

  ctx.font = '10px "Noto Sans CJK SC", sans-serif';
  ctx.textBaseline = "middle";
  for (let index = 0; index < 4; index += 1) {
    const value = maximum - (maximum - minimum) * index / 3;
    const position = y(value);
    ctx.strokeStyle = "#e5e9df";
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 5]);
    ctx.beginPath(); ctx.moveTo(left, position); ctx.lineTo(width - right, position); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#75816c";
    ctx.textAlign = "right";
    ctx.fillText(axisNumber(value), left - 11, position);
  }
  ctx.setLineDash([3, 5]);
  ctx.strokeStyle = "#bdc8b2";
  ctx.beginPath(); ctx.moveTo(left, y(0)); ctx.lineTo(width - right, y(0)); ctx.stroke();
  ctx.setLineDash([]);
  if (!hasHistory) {
    state.hoverIndex = -1;
    $("chart-tip").hidden = true;
    return;
  }

  const points = state.chartPoints;
  ctx.strokeStyle = "#e5e9df";
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 6]);
  ctx.globalAlpha = 0.6;
  points.forEach((point) => {
    ctx.beginPath(); ctx.moveTo(point.x, top); ctx.lineTo(point.x, barBottom); ctx.stroke();
  });
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
  ctx.strokeStyle = "#e5e9df";
  ctx.beginPath(); ctx.moveTo(left, barZero); ctx.lineTo(width - right, barZero); ctx.stroke();
  const maxDaily = Math.max(0.000001, ...points.map((point) => Math.abs(point.daily)));
  const barWidth = 9;
  points.forEach((point) => {
    const barHeight = Math.abs(point.daily) / maxDaily * Math.max(5, (barBottom - barTop) / 2 - 3);
    ctx.fillStyle = point.daily >= 0 ? "#91ab67" : "#cc8590";
    ctx.fillRect(point.x - barWidth / 2, point.daily >= 0 ? barZero - barHeight : barZero, barWidth, barHeight);
  });

  const area = ctx.createLinearGradient(0, top, 0, lineBottom);
  area.addColorStop(0, "rgba(107, 135, 58, 0.12)");
  area.addColorStop(1, "rgba(107, 135, 58, 0.005)");
  ctx.beginPath(); ctx.moveTo(points[0].x, y(0));
  points.forEach((point) => ctx.lineTo(point.x, point.y));
  ctx.lineTo(points[points.length - 1].x, y(0)); ctx.closePath();
  ctx.fillStyle = area; ctx.fill();
  ctx.beginPath();
  points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
  ctx.strokeStyle = "#6b873a";
  ctx.lineWidth = 2.3;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();
  const last = points[points.length - 1];
  ctx.beginPath(); ctx.arc(last.x, last.y, 6, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(107, 135, 58, 0.12)"; ctx.fill();
  points.forEach((point) => {
    ctx.beginPath(); ctx.arc(point.x, point.y, 2.4, 0, Math.PI * 2);
    ctx.fillStyle = "#6b873a"; ctx.fill();
  });

  ctx.fillStyle = "#75816c";
  ctx.textAlign = "center";
  points.forEach((point) => ctx.fillText(point.day.slice(5).replace("-", "/"), point.x, height - 10));
  const hover = points[state.hoverIndex];
  if (hover) {
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = "#96a77e";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(hover.x, top); ctx.lineTo(hover.x, barBottom); ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(hover.x, hover.y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = "#6b873a"; ctx.fill();
    ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2; ctx.stroke();
  }
}

function positionChartTooltip(point) {
  const tooltip = $("chart-tip");
  const content = $("chart-content");
  const scroller = $("performance-chart").closest(".chart-stage");
  tooltip.style.maxWidth = Math.max(1, scroller.clientWidth - 16) + "px";
  const minimum = scroller.scrollLeft + 8;
  const maximum = scroller.scrollLeft + scroller.clientWidth - tooltip.offsetWidth - 8;
  const preferred = point.x + 12 <= maximum ? point.x + 12 : point.x - tooltip.offsetWidth - 12;
  tooltip.style.left = Math.max(minimum, Math.min(maximum, preferred)) + "px";
  tooltip.style.top = Math.max(8, Math.min((content?.clientHeight || scroller.clientHeight) - tooltip.offsetHeight - 8, point.y - tooltip.offsetHeight - 12)) + "px";
}

function ensureChartPointVisible(point) {
  const scroller = $("performance-chart").closest(".chart-stage");
  const left = scroller.scrollLeft;
  if (point.x < left + 24 || point.x > left + scroller.clientWidth - 24) {
    scroller.scrollTo({ left: Math.max(0, point.x - scroller.clientWidth / 2), behavior: "instant" });
  }
}

function showChartPoint(index, ensureVisible = false) {
  const tooltip = $("chart-tip");
  if (!state.chartPoints[index]) { tooltip.hidden = true; return; }
  const changed = state.hoverIndex !== index || tooltip.hidden;
  if (changed) {
    state.hoverIndex = index;
    const window = state.chartWindow || chartWindow();
    renderChart(scopedHistory(window), window);
  }
  const point = state.chartPoints[index];
  if (!point) { tooltip.hidden = true; return; }
  if (ensureVisible) ensureChartPointVisible(point);
  if (changed) {
    const isToday = point.day === state.chartWindow.endDay;
    tooltip.innerHTML = escapeHtml(point.day.replaceAll("-", "/")) + (isToday ? " · 截至当前" : "") +
      '<small>累计净盈亏</small><strong class="' + tone(point.value) + '">' + escapeHtml(signed(point.value, 4)) + " " + escapeHtml(state.coin) + '</strong>' +
      '<small>当日盈亏 <b class="' + tone(point.daily) + '">' + escapeHtml(signed(point.daily, 4)) + " " + escapeHtml(state.coin) + '</b></small>' +
      '<small>当日平仓 ' + point.count + ' 笔</small>';
  }
  tooltip.hidden = false;
  positionChartTooltip(point);
}

function updateView() {
  if (!state.data) return;
  const window = chartWindow();
  const history = scopedHistory(window);
  const stats = calculateStats(history);
  const records = ledgerHistory(window);
  syncAccountControls();
  state.hoverIndex = -1;
  $("chart-tip").hidden = true;

  renderAccount(history, stats);
  renderScore(history, stats);
  renderPositions();
  renderLedger(records);
  renderChart(history, window);
  $("chart-total").textContent = signed(stats.net, 2, true) + " " + state.coin;
  $("chart-total").className = tone(stats.net);
  $("range-dates").textContent = window.startDay.slice(5).replace("-", "/") + " — " + window.endDay.slice(5).replace("-", "/");
  $("range-trades").textContent = history.length + " 笔已平仓交易";
  const historyUnavailable = Boolean(state.data.historyError) && !(state.data.history || []).length;
  if (historyUnavailable) {
    ["net-profit", "average"].forEach(id => setAmount(id, null));
    ["win-rate", "profit-factor"].forEach(id => { $(id).textContent = "—"; $(id).dataset.full = "—"; });
    ["gross-profit", "gross-loss", "wins", "losses", "chart-total"].forEach(id => { $(id).textContent = "—"; });
    ["nav-trade-count", "trade-heading-count"].forEach(id => { $(id).textContent = "—"; });
    $("result-count").textContent = "记录数未确认";
    $("page-copy").textContent = "历史数据尚未读取";
    $("range-trades").textContent = "已平仓记录尚未读取";
    $("win-record").textContent = "历史数据尚未读取";
    $("factor-sample").textContent = "样本未确认";
    $("score-label").textContent = "等待历史数据";
    $("ledger-empty").querySelector("h3").textContent = "历史数据暂时不可用";
    $("ledger-empty").querySelector("p").textContent = "恢复读取后会自动显示交易记录。";
  } else {
    $("ledger-empty").querySelector("h3").textContent = "没有找到相关交易";
    $("ledger-empty").querySelector("p").textContent = "试试其他合约名称，或调整筛选条件。";
  }
  const incomplete = records.length - history.length;
  if (incomplete > 0 && !historyUnavailable) {
    ["net-profit", "average"].forEach(id => setAmount(id, null));
    ["win-rate", "profit-factor"].forEach(id => { $(id).textContent = "—"; $(id).dataset.full = "—"; });
    $("win-record").textContent = stats.wins + " 胜 / " + stats.losses + " 负 · " + incomplete + " 笔收益待确认";
    $("factor-sample").textContent = history.length + " / " + records.length + " 笔收益已确认";
    $("score-label").textContent = "部分收益待确认";
    $("range-trades").textContent = history.length + " 笔收益已确认 · " + incomplete + " 笔待确认";
    $("chart-total").textContent = "部分收益待确认";
  }
  if (!historyUnavailable) ["nav-trade-count", "trade-heading-count"].forEach(id => { $(id).textContent = records.length; });
  $("chart-empty").querySelector("strong").textContent = historyUnavailable ? "历史数据尚未读取" : "该区间暂无交易";
  $("chart-empty").querySelector("span").textContent = historyUnavailable ? "连接恢复后自动更新，当前无法确认收益。" : "平仓后，收益轨迹将在这里呈现";
  $("history-start").textContent = "记录起始 " + String(state.data.historyStart || "—").replaceAll("-", ".");
  $("footer-window").textContent = "统计窗口 " + (state.range === "all" ? "全部" : state.range + "D");
  fitNumbers();
  initializeIcons();
}

function showError(message) {
  $("error-strip").hidden = !message;
  $("error-strip").querySelector("span").textContent = message;
}

function populateCurrencies(data) {
  const values = [...new Set([
    ...(data.accounts || []).map((item) => item.coin),
    ...(data.positions || []).map((item) => item.coin),
    ...(data.history || []).map((item) => item.coin),
  ])].filter(Boolean);
  if (!values.length) values.push("USDT");
  if (!state.initialAccountApplied) {
    state.initialAccountApplied = true;
    if (new URLSearchParams(location.search).get("account")?.toUpperCase() === "CFD" && values.includes("USD") && isCfdAccount("USD", data)) state.coin = "USD";
  }
  if (!values.includes(state.coin)) state.coin = values[0];
  $("currency").innerHTML = values.map((coin) =>
    '<option value="' + escapeHtml(coin) + '"' + (coin === state.coin ? " selected" : "") + ">" + escapeHtml(accountLabel(coin, data)) + "</option>"
  ).join("");
  $("account-switcher").innerHTML = values.map(coin =>
    '<button type="button" data-account-coin="' + escapeHtml(coin) + '" aria-pressed="' + String(coin === state.coin) + '" class="' + (coin === state.coin ? 'selected' : '') + '">' + escapeHtml(accountLabel(coin, data)) + '</button>'
  ).join("");
  $("account-switcher").querySelectorAll("button").forEach(button => button.addEventListener("click", () => selectAccount(button.dataset.accountCoin)));
}

function renderData(data) {
  if (liveState.payload && positionSnapshotTime(liveState.payload) > positionSnapshotTime(data)) {
    Object.assign(data, positionPayloadFields(liveState.payload));
  }
  state.data = data;
  const generated = new Date(data.generatedAt).getTime();
  state.clockBase = Number.isFinite(generated) ? generated : Date.now();
  state.clockStarted = performance.now();
  populateCurrencies(data);
  updateView();
  const errors = [data.accountError, data.historyError].filter(Boolean);
  const hasCachedErrorData = (data.accountError && data.accounts?.length) || (data.historyError && data.history?.length);
  showError(errors.length ? errors.join("；") + (hasCachedErrorData ? "。部分数据沿用上次结果；未获取的指标显示为 —。" : "。未获取的数据暂以 — 显示。") : "");
  tick();
  if (Date.now() >= liveState.next) loadLivePositions();
}

async function loadDashboard() {
  if (state.loading) return;
  state.loading = true;
  if (!state.data) {
    $("ledger-loading").hidden = false;
    $("ledger-loading").classList.remove("has-error");
    $("ledger-loading").innerHTML = '<i data-lucide="loader-circle"></i><span>正在同步交易数据…</span>';
    $("ledger-empty").hidden = true;
    initializeIcons();
  }
  $("refresh").disabled = true;
  $("refresh").classList.add("loading");
  $("retry").disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);

  try {
    const response = await fetch("api/dashboard", { cache: "no-store", signal: controller.signal });
    if (window.BitgetSession?.check(response) === false) return;
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    if (!Array.isArray(data.history) || !Array.isArray(data.positions) || !Array.isArray(data.accounts)) {
      throw new Error("数据格式异常");
    }
    state.fetchFailed = false;
    renderData(data);
  } catch (error) {
    state.fetchFailed = true;
    document.body.classList.remove("healthy");
    document.body.classList.add("stale");
    $("connection").querySelector("span").textContent = "连接中断";
    showError((error.name === "AbortError" ? "读取超时" : "数据暂时无法读取") +
      (state.data ? "，已保留最近一次成功数据。" : "，请稍后重试。"));
    if (!state.data) {
      $("ledger-loading").hidden = false;
      $("ledger-loading").classList.add("has-error");
      $("ledger-loading").innerHTML = '<i data-lucide="cloud-off"></i><span>交易数据暂时无法读取，请使用上方“重试”按钮重新同步。</span>';
      $("ledger-empty").hidden = true;
      $("position-empty").hidden = true;
      $("chart-empty").hidden = true;
      document.querySelector(".ledger-table-wrap").hidden = true;
      initializeIcons();
    }
  } finally {
    clearTimeout(timeout);
    state.loading = false;
    state.nextRefresh = Date.now() + Math.max(5, numberValue(state.data?.refreshSeconds) || 30) * 1000;
    $("refresh").disabled = false;
    $("refresh").classList.remove("loading");
    $("retry").disabled = false;
  }
}

function tick() {
  $("clock").textContent = formatDate(serverNow(), "time");
  document.querySelectorAll("[data-opened]").forEach((element) => {
    element.textContent = positionDuration(element.dataset.opened, element.dataset.fallback);
  });
  if (!document.hidden && Date.now() >= state.nextRefresh && !state.loading) loadDashboard();
  if (!state.data) return;

  const monitorTime = new Date(state.data.monitorUpdatedAt).getTime();
  const age = Number.isFinite(monitorTime) ? Math.max(0, Math.floor((serverNow() - monitorTime) / 1000)) : Infinity;
  const healthy = Boolean(state.data.monitorHealthy) && age <= 90 && !state.fetchFailed;
  document.body.classList.toggle("healthy", healthy);
  document.body.classList.toggle("stale", !healthy);
  $("connection").querySelector("span").textContent = state.fetchFailed ? "连接中断" : healthy ? "同步正常" : "同步延迟";
  $("as-of").textContent = formatDate(state.data.generatedAt);
  renderLiveStatus();
  $("footer-sync").textContent = "最近同步 " + formatDate(state.data.monitorUpdatedAt, "time") + " / " +
    (Number.isFinite(age) ? age + " 秒前" : "时间未知");

}

$("refresh").addEventListener("click", loadDashboard);
$("retry").addEventListener("click", loadDashboard);
$("currency").addEventListener("change", (event) => {
  selectAccount(event.target.value);
});

document.querySelectorAll("[data-range]").forEach((button) => {
  button.addEventListener("click", () => {
    state.range = button.dataset.range;
    state.page = 1;
    document.querySelectorAll("[data-range]").forEach((candidate) => {
      const selected = candidate === button;
      candidate.classList.toggle("selected", selected);
      candidate.setAttribute("aria-pressed", String(selected));
    });
    updateView();
  });
});

$("search").addEventListener("input", (event) => {
  state.query = event.target.value.trim();
  state.page = 1;
  renderLedger(ledgerHistory());
});
$("side").addEventListener("change", (event) => {
  state.side = event.target.value;
  state.page = 1;
  renderLedger(ledgerHistory());
});
$("result").addEventListener("change", (event) => {
  state.result = event.target.value;
  state.page = 1;
  renderLedger(ledgerHistory());
});
$("reset-filter").addEventListener("click", () => {
  state.query = "";
  state.side = "all";
  state.result = "all";
  state.page = 1;
  $("search").value = "";
  $("side").value = "all";
  $("result").value = "all";
  renderLedger(ledgerHistory());
});
$("prev").addEventListener("click", () => {
  state.page = Math.max(1, state.page - 1);
  renderLedger(ledgerHistory());
});
$("next").addEventListener("click", () => {
  state.page += 1;
  renderLedger(ledgerHistory());
});

const sortOrders = {
  latest: ["closedAt", -1],
  earliest: ["closedAt", 1],
  "profit-high": ["netPnl", -1],
  "profit-low": ["netPnl", 1],
};

function updateSortControls() {
  document.querySelectorAll("[data-sort]").forEach((button) => {
    const selected = button.dataset.sort === state.sort;
    button.classList.toggle("active", selected);
    button.closest("th")?.setAttribute("aria-sort", selected
      ? (state.direction === 1 ? "ascending" : "descending") : "none");
    button.querySelectorAll("svg, i[data-lucide]").forEach((icon) => icon.remove());
    const icon = document.createElement("i");
    icon.dataset.lucide = selected
      ? (state.direction === 1 ? "arrow-up" : "arrow-down") : "chevrons-up-down";
    button.append(icon);
  });
  if ($("ledger-order")) {
    $("ledger-order").value = Object.entries(sortOrders)
      .find(([, [key, direction]]) => key === state.sort && direction === state.direction)?.[0] || "latest";
  }
  initializeIcons();
}

document.querySelectorAll("[data-sort]").forEach((button) => {
  button.addEventListener("click", () => {
    state.direction = state.sort === button.dataset.sort ? -state.direction : -1;
    state.sort = button.dataset.sort;
    state.page = 1;
    updateSortControls();
    renderLedger(ledgerHistory());
  });
});
$("ledger-order")?.addEventListener("change", (event) => {
  const order = sortOrders[event.target.value];
  if (!order) return;
  [state.sort, state.direction] = order;
  state.page = 1;
  updateSortControls();
  renderLedger(ledgerHistory());
});
updateSortControls();

const navNames = { account: "概览", positions: "当前持仓", performance: "收益分析", ledger: "交易记录" };
const navigation = [...document.querySelectorAll(".primary-nav a")];
function activateNavigation(id) {
  if (document.body.dataset.workspace && document.body.dataset.workspace !== "dashboard") return;
  navigation.forEach((link) => {
    const selected = link.getAttribute("href") === "#" + id;
    link.classList.toggle("active", selected);
    if (selected) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
  if ($("nav-view-label") && navNames[id]) $("nav-view-label").textContent = navNames[id];
}
navigation.forEach((link) => {
  link.addEventListener("click", () => activateNavigation(link.hash.slice(1)));
});
const navigationSections = Object.keys(navNames).map($).filter(Boolean);
let navigationFrame = 0;
function syncNavigationToScroll() {
  navigationFrame = 0;
  if (document.body.dataset.workspace && document.body.dataset.workspace !== "dashboard") return;
  const topbar = document.querySelector(".topbar");
  const boundary = Math.max(100, (topbar?.getBoundingClientRect().height || 64) + 28);
  let current = navigationSections[0];
  navigationSections.forEach((section) => {
    if (section.getBoundingClientRect().top <= boundary) current = section;
  });
  const maximumScroll = document.documentElement.scrollHeight - innerHeight;
  if (maximumScroll > 1 && scrollY >= maximumScroll - 3) current = navigationSections[navigationSections.length - 1];
  if (current) activateNavigation(current.id);
}
function scheduleNavigationSync() {
  if (!navigationFrame) navigationFrame = requestAnimationFrame(syncNavigationToScroll);
}
if ("IntersectionObserver" in window) {
  const observer = new IntersectionObserver(scheduleNavigationSync, {
    rootMargin: "-12% 0px -65% 0px", threshold: [0, 0.1, 0.5, 1],
  });
  navigationSections.forEach((section) => observer.observe(section));
}
// Nested sections can remain inside the observer band without crossing a threshold.
// Track scroll position as well so the active location always follows the visible section.
window.addEventListener("scroll", scheduleNavigationSync, { passive: true });
window.addEventListener("resize", scheduleNavigationSync);
window.addEventListener("hashchange", scheduleNavigationSync);
activateNavigation(Object.hasOwn(navNames, location.hash.slice(1)) ? location.hash.slice(1) : "account");
scheduleNavigationSync();

$("close-drawer").addEventListener("click", closeDrawer);
$("trade-drawer").addEventListener("click", (event) => {
  if (event.target === $("trade-drawer")) closeDrawer();
});
$("trade-drawer").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeDrawer();
});
$("trade-drawer").addEventListener("close", restoreDrawerFocus);

$("performance-chart").addEventListener("pointermove", (event) => {
  if (!state.chartPoints.length) return;
  const pointerX = event.clientX - event.currentTarget.getBoundingClientRect().left;
  let closest = 0;
  state.chartPoints.forEach((point, index) => {
    if (Math.abs(point.x - pointerX) < Math.abs(state.chartPoints[closest].x - pointerX)) closest = index;
  });
  showChartPoint(closest);
});
$("performance-chart").addEventListener("pointerleave", () => {
  state.hoverIndex = -1;
  $("chart-tip").hidden = true;
  renderChart(scopedHistory());
});
$("performance-chart").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key) || !state.chartPoints.length) return;
  event.preventDefault();
  const next = state.hoverIndex < 0
    ? (event.key === "ArrowRight" ? 0 : state.chartPoints.length - 1)
    : state.hoverIndex + (event.key === "ArrowRight" ? 1 : -1);
  showChartPoint(Math.max(0, Math.min(state.chartPoints.length - 1, next)), true);
});

const chartScroller = $("performance-chart").closest(".chart-stage");
chartScroller.addEventListener("scroll", () => {
  const point = state.chartPoints[state.hoverIndex];
  if (!point) return;
  if (point.x < chartScroller.scrollLeft + 4 || point.x > chartScroller.scrollLeft + chartScroller.clientWidth - 4) {
    state.hoverIndex = -1;
    $("chart-tip").hidden = true;
    const window = state.chartWindow || chartWindow();
    renderChart(scopedHistory(window), window);
  } else {
    positionChartTooltip(point);
  }
}, { passive: true });

let resizeTimer;
new ResizeObserver(() => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (state.data) {
      const window = state.chartWindow || chartWindow();
      renderChart(scopedHistory(window), window);
      const point = state.chartPoints[state.hoverIndex];
      if (point && !$("chart-tip").hidden) positionChartTooltip(point);
      fitNumbers();
    }
  }, 90);
}).observe(chartScroller);

window.addEventListener("online", () => {
  loadDashboard();
  liveState.next = 0;
  loadLivePositions();
});
window.addEventListener("offline", () => {
  liveState.failed = true;
  clearTimeout(liveState.timer);
  liveState.controller?.abort();
  renderLiveStatus();
});
window.addEventListener("pagehide", () => {
  clearTimeout(liveState.timer);
  liveState.controller?.abort();
});
window.addEventListener("pageshow", () => {
  liveState.next = 0;
  loadLivePositions();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(liveState.timer);
    liveState.controller?.abort();
  } else {
    if (Date.now() >= state.nextRefresh) loadDashboard();
    liveState.next = 0;
    loadLivePositions();
  }
});

initializeIcons();
loadDashboard();
setInterval(tick, 1000);
