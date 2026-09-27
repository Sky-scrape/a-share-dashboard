// 竞价页脚本（2026-09-27 方案 O-3c 推广：自 index.html 内联抽出；静态根直供 /auction/app.js）。零构建约定：直接改此文件。
"use strict";
/* ============ 实时竞价页（2026-08-31 新增，第五板块） ============ */
var POLL_MS = 10000, SERIES_POLL_MS = 30000;
var state = { payload: null, series: null, prevItems: null, alerts: [], histAlerts: [], rankKey: "pct",
              rankSort: null, rankDir: -1, curveCode: null, etag: null, seriesEtag: null,
              fetchTimer: null, finalItems: [], candKey: "std", candCodes: [], sectorSide: "up" };

function $(id) { return document.getElementById(id); }
/* 主题色板（lib/theme.js）：晨报纸色/夜台霓虹；canvas 图表不认 var()，setOption 时取值 */
var P = AKTHEME.C;
/* 全站图表统一字族（canvas 不认 CSS 变量，option 里显式声明） */
var CHART_FONT = '"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif';
/* esc 统一在 /lib/util.js（五页同源，含单引号转义） */
/* toast 已收敛 AK.toast（lib/util.js）：保留同名包装，页面调用点不用动 */
function toast(msg, type, ms) { AK.toast(msg, type, ms); }
function fmtPct(v) {
  if (v == null || isNaN(v)) return "-";
  var s = (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%";
  return s;
}
function pctClass(v) { return v > 0.005 ? "up" : (v < -0.005 ? "down" : "flat"); }
function fmtAmt(yuan) {
  if (yuan == null || isNaN(yuan)) return "-";
  var abs = Math.abs(yuan);
  if (abs >= 1e8) return (yuan / 1e8).toFixed(2) + "亿";
  if (abs >= 1e4) return (yuan / 1e4).toFixed(0) + "万";
  return String(Math.round(yuan));
}
function fmtVol(hand) {
  if (hand == null || isNaN(hand)) return "-";
  return Math.abs(hand) >= 1e4 ? (hand / 1e4).toFixed(1) + "万手" : hand + "手";
}
/* 上游数值字段可能是字符串数字或 null：统一过一道 Number 收口，
   避免 "12.3".toFixed / null.toFixed 之类 TypeError（safeRender 会兜住但面板会变「渲染失败」） */
function num(v) { return (v == null || v === "" || isNaN(v)) ? null : Number(v); }
function nowHMS() {
  var d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()];
}
function hms2min(h, m) { return (+h) * 60 + (+m); }   // 强转：传字符串时会变拼接（"09"*60+"17" = "54017"），阶段判断会全部穿透
function pad2(n) { return String(n).padStart(2, "0"); }
/* 本地日期工具已收敛 /lib/util.js（AK.dates，2026-09-10）：保留同名薄委托，
   本页十来处调用点（含「数据日」标注与体检面板口径）不改。 */
function today8() { return AK.dates.today8(); }
function mdOf(d8) { return AK.dates.mdOf(d8); }
function isWeekend() { var w = new Date().getDay(); return w === 0 || w === 6; }
/* 数据新鲜度统一口径（隔日 / 周末 / 漏采 / 今日只采到 live 没采到终态）。
   只信数据文件自带的竞价日：.status/auction.json.date 是「任务上次运行日」，
   空池跳过也会写今天，拿来当数据日会把隔日旧数据误判成今日新鲜数据。 */
function dataFreshness(p) {
  p = p || state.payload || {};
  var t = today8();
  var fd = String((p.final && p.final.date) || "");
  var ld = String((p.live && p.live.date) || "");
  var liveToday = ld === t, finalToday = fd === t;
  var hasAny = !!(fd || ld);
  return {
    date8: (liveToday || finalToday) ? t : (fd || ld),   // 今日任一口径存在就计作今日
    hasAny: hasAny,
    today: hasAny ? (liveToday || finalToday) : null,    // null = 尚无数据（三态，调用方用 === false 判隔日）
    liveToday: liveToday, finalToday: finalToday
  };
}

/* 一轮采算是否落在集合竞价窗口（09:15:00–09:25:59）。
   采集器会给每轮写 in_window；旧数据没这个字段就从 ts 时刻回推。
   窗口外的补抓拿到的永远是 09:25 定盘冻结值，不能当竞价逐轮连线。 */
function roundInWindow(r) {
  if (typeof r.in_window === "boolean") return r.in_window;
  var t = String(r.ts || "").slice(11, 19);
  return t.length === 8 && t >= "09:15:00" && t <= "09:25:59";
}

/* ---- 顶栏导航（五页共用；竞价排在轮动之前）---- */
try { AKBAR.renderNavgroup({ slot: "navgroupSlot", active: "/auction" }); } catch (e) {}
/* 主题切换（晨报⇄夜台）：重绘注册制（AKTHEME.onRedraw，2026-09-10 收敛，不再手写
   akthemechange 监听清单）；三张图按当前 state 重画，P() 会取到新主题色 */
AKTHEME.onRedraw(function () {
  safeRender(renderDist);
  safeRender(renderSector);
  if (state.curveCode && state.series) renderCurve();
});

/* ============ 阶段判定（本地时钟 + 数据日期 + 上游 data_status 三口径） ============ */
function phaseInfo() {
  var t = nowHMS(), m = hms2min(t[0], t[1]);
  var p = state.payload || {};
  var live = p.live, fin = p.final;
  var fr = dataFreshness();
  var out;
  // 先按「数据是不是今天的」定性，再看时钟：避免周末/节假日/漏采时把旧数据说成「已开盘」
  if (!fr.hasAny) {
    out = { key: "idle", text: "暂无竞价数据（交易日 09:14 计划任务自动采集，或点「立即补抓」）", noise: false };
  } else if (fr.today === false) {
    out = { key: "idle", text: (isWeekend() ? "周末休市·无今日竞价" : "今日竞价未采集") +
      " · 展示 " + mdOf(fr.date8) + " 数据", noise: false };
  } else if (m < hms2min(9, 15)) {
    out = { key: "idle", text: "竞价未开始（展示今日已有轮次）", noise: false };
  } else if (m < hms2min(9, 20)) {
    out = { key: "live", text: "集合竞价 · 可撤单", noise: true };
  } else if (m < hms2min(9, 25)) {
    out = { key: "lock", text: "集合竞价 · 不可撤单", noise: false };
  } else if (m < hms2min(9, 30)) {
    out = { key: "final", text: fr.finalToday ? "已撮合 · 待开盘"
                                          : "已撮合·今日无终态（定格在最后 live 轮）", noise: false };
  } else {
    out = { key: "final", text: fr.finalToday ? "已开盘（竞价定格）"
                                          : "已开盘（今日缺终态，定格在最后 live 轮）", noise: false };
  }
  // 上游诚实口径：接口自身回报的阶段（盘后补抓常见 closed/final）
  var rnd = (live && live.round) || (fin && fin.round) || {};
  if ((rnd.phase === "closed" || rnd.data_status === "final") && (out.key === "live" || out.key === "lock")) {
    out.text += "（上游:closed）";
  }
  if (fin && fin.provisional && out.key !== "idle") out.text += " · 终态未就绪";
  return out;
}

/* ============ 轮询（/api/auction 带 ETag/304） ============ */
var _pollSeq = 0;   // 请求序号：保存观察池后手动 poll 与 10s 定时 poll 并发时，旧响应后到不得覆盖新状态
function poll() {
  if (document.hidden) return;   // 后台标签页不轮询（回来时 visibilitychange 立即补拉一次）
  var seq = ++_pollSeq;
  var headers = {};
  if (state.etag) headers["If-None-Match"] = state.etag;
  fetch("/api/auction", { headers: headers }).then(function (r) {
    if (r.status === 304) return null;
    state.etag = r.headers.get("ETag");
    return r.json();
  }).then(function (d) {
    if (seq !== _pollSeq) return;   // 已有更新的 poll 完成，这份旧响应丢弃
    if (d) safeRender(applyPayload, d);
    safeRender(renderPhase);
    safeRender(renderCountdown);
    safeRender(renderFresh);
  }).catch(function (e) { if (seq === _pollSeq) renderFresh("bad", "请求失败: " + e); });
}

/* 单块渲染抛错只污染自己（复盘页同口径：避免「改一处全页空白」）。
   2026-09-10 收敛：实现统一在 lib/util.js AK.safeRender（console.error + AK.toast），
   本页保留同名薄委托，调用点与行为（含可选实参透传）不变。 */
function safeRender(fn) {
  var args = Array.prototype.slice.call(arguments, 1);
  return AK.safeRender(function () { return fn.apply(null, args); }, (fn && fn.name) || "render");
}

function applyPayload(d) {
  state.payload = d;
  var items = currentItems(d);
  if (d.alerts && d.alerts.items) state.histAlerts = d.alerts.items;   // 后端全天回算，盘后/刷新后不丢
  safeRender(detectAlerts, items, d);
  state.prevItems = items;
  safeRender(renderAlertList);
  safeRender(renderStats, d);
  safeRender(renderRank);
  safeRender(renderDist);
  safeRender(renderSector);
  safeRender(renderSelf);
  safeRender(renderCand);
  safeRender(renderRelay);
  safeRender(renderRealize);
  safeRender(renderFlip);
  safeRender(renderPoolExec);
  fetchSeries(false);   // flip 面板靠 series 逐轮；ETag 命中即 304，很轻
  safeRender(renderCheck);
  safeRender(renderFinal, d);
  safeRender(renderBench, d);
  if (state.curveCode) safeRender(renderCurve);
  if (d.fetching) watchAuctionFetch();   // 页面打开时已有补抓在跑，按钮同步上锁
  // 采集刚更新一轮时，打开中的曲线弹窗也刷新
}

function currentItems(d) {
  var src = (d.live && d.live.round) || (d.final && d.final.round);
  return (src && src.items) || [];
}

/* ============ 状态条 ============ */
function renderPhase() {
  var ph = phaseInfo();
  var badge = $("phaseBadge");
  badge.className = "ph-" + (ph.key === "live" || ph.key === "lock" || ph.key === "final" ? ph.key : "idle");
  $("phaseText").textContent = ph.text;
  $("phaseNoise").innerHTML = ph.noise ? '<span class="tag-noise">可撤单 · 含虚假申报噪音</span>' : "";
}

function renderCountdown() {
  var t = nowHMS(), m = hms2min(t[0], t[1]), s = t[2];
  var pill = $("countdownPill"), txt, cls = "fresh-pill";
  var fr = dataFreshness();
  if (isWeekend()) {
    txt = "周末休市 · 无竞价";
  } else if (m >= hms2min(9, 15) && m < hms2min(9, 25)) {
    var left = (hms2min(9, 25) - m) * 60 - s;
    txt = "距定盘 " + pad2(Math.floor(left / 60)) + ":" + pad2(left % 60);
    cls = "fresh-pill warn";
  } else if (m >= hms2min(9, 25) && m < hms2min(9, 30)) {
    var left2 = (hms2min(9, 30) - m) * 60 - s;
    txt = "距开盘 " + pad2(Math.floor(left2 / 60)) + ":" + pad2(left2 % 60);
    cls = "fresh-pill ok";
  } else {
    txt = "竞价 " + (m < hms2min(9, 15) ? "未开始" : "已结束");
    if (fr.hasAny && fr.today === false) { txt += " · 展示 " + mdOf(fr.date8); cls = "fresh-pill warn"; }
  }
  pill.className = cls;
  pill.innerHTML = '<span class="dot"></span>' + txt;
  pill.title = fr.hasAny ? "数据所属竞价日: " + fr.date8 : "尚无竞价数据（交易日 09:14 计划任务，或点「立即补抓」）";
}
setInterval(renderCountdown, 1000);

function renderFresh(force, msg) {
  var pill = $("freshPill"), p = state.payload;
  if (!p) return;
  var st = p.status || {};
  var items = currentItems(p);
  var fr = dataFreshness();
  var cls = "ok", txt;
  if (force === "bad") { cls = "bad"; txt = msg || "异常"; }
  else if (!items.length && !st.last_run) { cls = "warn"; txt = "暂无数据"; }
  else {
    var src = (p.live && p.live.round) || (p.final && p.final.round) || {};
    var full = src.ts || st.last_run || "";
    // 隔日数据要一眼看得出来：胶囊改标「非今日 MM/DD」；今日数据只到秒（窄屏不挤顶栏，完整时间在状态条）
    txt = fr.today === false ? ("非今日 " + mdOf(fr.date8)) : ("更新 " + (full.slice(11, 19) || "无时间戳"));
    if (fr.today === false) cls = "warn";
    if (p.fetching) { txt += " · 抓取中"; cls = "warn"; }
    if ((src.errors || []).length) { cls = "warn"; txt += " · " + src.errors.length + " 批失败"; }
    if (!items.length) cls = "warn";
  }
  pill.className = "fresh-pill " + cls;
  pill.innerHTML = '<span class="dot"></span>' + esc(txt);
  pill.title = "数据日: " + (fr.date8 || "-") + " · 上次采集: " + (st.last_run || "-") +
    " · 轮次 " + (st.rounds == null ? "-" : st.rounds) +
    (st.mode ? " · " + (st.mode === "manual" ? "手动补抓" : st.mode) : "") +
    (st.note ? " · " + st.note : "") + "（完整时间看顶部状态条「更新」）";
}

function renderStats(d) {
  var rnd = (d.live && d.live.round) || (d.final && d.final.round) || {};
  // 采集器会把失败批（最多记 6 条）带在 round.errors 里：不标出来，用户只会看到默默少了几只股。
  // 注意：round.errors 是数组（错误文本），与 rounds_meta.rounds[].errors（数字，批次数）同名不同型——
  // 本页 renderCheck 按 (r.errors || 0) 累加的是后者，改动两处时勿混用口径。
  var errs = rnd.errors || [];
  $("statCover").textContent = (rnd.count != null ? rnd.count + "/" + rnd.codes_total : "-") +
    (errs.length ? "（" + errs.length + " 批失败）" : "");
  $("statCover").title = errs.length ? errs.join("\n") : "本批观察池全部返回";
  $("statPool").textContent = (d.config && d.config.max_codes ? "≤" + d.config.max_codes : "-") +
    "（自选 " + (d.watchlist || []).length + "）";
  $("statRounds").textContent = (d.status && d.status.rounds != null) ? d.status.rounds : "-";
  $("statUpdated").textContent = (((d.live && d.live.updated_at) || (d.final && d.final.fetched_at) || "-").slice(5, 19)) || "-";
  $("statPhase").textContent = rnd.phase || rnd.data_status || "-";
}

/* ============ 热榜 ============ */
$("rankSeg").addEventListener("click", function (ev) {
  var b = ev.target.closest("button"); if (!b) return;
  // 切排序口径时回到降序：否则上一列调成的升序会闷着带到新列（热榜变成榜底）
  state.rankKey = b.dataset.k; state.rankSort = null; state.rankDir = -1;
  Array.prototype.forEach.call($("rankSeg").children, function (x) { x.classList.toggle("on", x === b); });
  renderRank();
});
$("rankTable").querySelector("thead").addEventListener("click", function (ev) {
  var th = ev.target.closest("th"); if (!th || !th.dataset.sort) return;
  if (state.rankSort === th.dataset.sort) state.rankDir = -state.rankDir;
  else { state.rankSort = th.dataset.sort; state.rankDir = -1; }
  renderRank();
});

function renderRank() {
  var d = state.payload; if (!d) return;
  var items = currentItems(d).slice();
  if (!items.length) {
    // 池子被清空/上游空返回时必须清掉旧表，否则上一轮渲染的行永久残留
    $("rankBody").innerHTML = '<tr><td colspan="8" class="dim" style="text-align:center;padding:26px">暂无数据</td></tr>';
    $("rankNote").textContent = "点击个股查看竞价曲线";
    return;
  }
  var key = state.rankSort ||
    ({ pct: "auction_pct", amount: "auction_amount", volume: "auction_volume" })[state.rankKey];
  var dir = state.rankDir;
  items.sort(function (a, b) { return ((a[key] || 0) - (b[key] || 0)) * dir; });
  var self = new Set(d.watchlist || []);
  var wm = d.watchmap || {};   // 行业后缀用采集时已落盘的 watchmap，不新增上游调用
  var html = "";
  // 全量渲染（行数下拉已取消）：观察池上限 300 只，多出的行靠 .rankScroll 内部滚动看
  items.forEach(function (r) {
    var pc = pctClass(r.auction_pct);
    var price = num(r.auction_price), um = num(r.auction_unmatched), to = num(r.auction_turnover_pct);
    html += '<tr class="' + (self.has(r.thscode) ? "self" : "") + '" data-code="' + esc(r.thscode) + '">' +
      "<td>" + esc(r.name || r.ticker) +
      '<span class="sub-ind">' + esc((wm[r.thscode] || {}).i || "未分类") + "</span></td>" +
      '<td class="mono dim">' + esc(r.ticker) + "</td>" +
      '<td class="mono"><span class="' + pc + '">' + (price != null ? price : "-") +
        " / " + fmtPct(r.auction_pct) + "</span></td>" +
      '<td class="mono">' + fmtAmt(r.auction_amount) + "</td>" +
      '<td class="mono">' + fmtVol(r.auction_volume) + "</td>" +
      '<td class="mono ' + (um > 0 ? "up" : (um < 0 ? "down" : "dim")) + '">' +
        (um != null ? (um > 0 ? "+" : "") + um : "-") + "</td>" +
      '<td class="mono">' + (to != null ? to.toFixed(2) + "%" : "-") + "</td>" +
      '<td class="mono">' + (vrMult(r) != null ? vrMult(r).toFixed(2) : "-") + "</td></tr>";
  });
  $("rankBody").innerHTML = html || '<tr><td colspan="8" class="dim" style="text-align:center">空</td></tr>';
  /* 表头排序方向指示（全站表格规范）：当前排序列 ▼/▲，其余清空 */
  $("rankTable").querySelectorAll("th[data-sort]").forEach(function (th) {
    var ind = th.querySelector(".sort-ind");
    if (!ind) { ind = document.createElement("span"); ind.className = "sort-ind"; th.appendChild(ind); }
    ind.textContent = state.rankSort === th.dataset.sort ? (state.rankDir === -1 ? "▼" : "▲") : "";
  });
  var src = (d.live && d.live.round) || (d.final && d.final.round) || {};
  $("rankNote").textContent = "口径: " + (src.stage || "-") + (src.phase ? "/" + src.phase : "") +
    " · 共 " + items.length + " 只 · 滚动查看 · 点行看曲线";
}
$("rankBody").addEventListener("click", function (ev) {
  var tr = ev.target.closest("tr[data-code]"); if (!tr) return;
  openCurve(tr.dataset.code);
});

/* ============ 高开低开分布（echarts 直方图） ============ */
var distChart = echarts.init($("distChart"));
function renderDist() {
  var items = currentItems(state.payload || {});
  if (!items.length) return;
  var bins = [-10, -7, -5, -3, -1, -0.3, 0.3, 1, 3, 5, 7, 10];
  var labels = [], counts = new Array(bins.length - 1).fill(0);
  for (var i = 0; i < bins.length - 1; i++) labels.push(bins[i] + "%~" + bins[i + 1] + "%");
  var flat = 0;
  items.forEach(function (r) {
    var v = r.auction_pct; if (v == null) return;
    if (Math.abs(v) <= 0.005) { flat++; return; }
    for (var j = 0; j < bins.length - 1; j++) {
      if (v >= bins[j] && v < bins[j + 1]) { counts[j]++; return; }
    }
    if (v <= bins[0]) counts[0]++;
    if (v >= bins[bins.length - 1]) counts[counts.length - 1]++;
  });
  var hi = items.filter(function (r) { return r.auction_pct > 0.005; }).length;
  var lo = items.filter(function (r) { return r.auction_pct < -0.005; }).length;
  $("distNote").textContent = "高开 " + hi + " · 低开 " + lo;
  $("distFlat").textContent = "平开 " + flat + "（" + (items.length ? (flat / items.length * 100).toFixed(1) : 0) + "%）";
  distChart.setOption({
    textStyle: { fontFamily: CHART_FONT },
    // 标签完全保持原形式（-10%~-7% 旋转 30°、interval 用默认），只调边框：
    // 旋转标签竖向要 ~39px，原 grid.bottom:26 会把尾巴裁到面板外 → 给 44，容器 180→210px。
    // 桌面宽度下槽位 ≥79px、旋转标签只占 ~38px，默认 interval 已是 11 档全显；
    // 只有手机级窄屏才会自动抽稀（保留原有降级行为，不致于叠成一团）。
    grid: { left: 40, right: 10, top: 14, bottom: 44 },
    tooltip: { trigger: "axis", backgroundColor: P().panel, borderColor: P().line2, textStyle: { color: P().text1 } },
    xAxis: { type: "category", data: labels, axisLabel: { fontSize: 10, rotate: 30, color: P().text3 }, axisLine: { lineStyle: { color: P().line } } },
    yAxis: { type: "value", minInterval: 1, axisLabel: { color: P().text3 }, splitLine: { lineStyle: { color: P().split } } },
    series: [{
      type: "bar", data: counts.map(function (c, i) {
        return { value: c, itemStyle: { color: bins[i] >= 0.3 ? P().up : (bins[i + 1] <= -0.3 ? P().down : P().flat) } };
      }), barWidth: "70%"
    }]
  }, true);
}

/* ============ 板块聚合 ============ */
$("sectorSideSeg").addEventListener("click", function (ev) {
  var b = ev.target.closest("button"); if (!b || b.dataset.k === state.sectorSide) return;
  state.sectorSide = b.dataset.k;
  Array.prototype.forEach.call($("sectorSideSeg").children, function (x) { x.classList.toggle("on", x === b); });
  renderSector();
});
var sectorChart = echarts.init($("sectorChart"));
function renderSector() {
  var d = state.payload; if (!d) return;
  var rows, srcNote;
  var titleEl = $("sectorTitle");
  // 首选：一级行业指数开盘缺口——指数由全部成分股集合竞价撮合而来，是「板块内所有票」的真口径。
  // 门槛=与页面数据日一致（dataFreshness().date8，final/live 自带的竞价日），而非本地日历日：
  // 周末/节假日/盘前 sector 停在上一交易日，与整页展示的是同一竞价日，正是该日真实全板块口径，
  // 应照常展示并诚实标注数据日；只有 sector 落后于数据日（采集盘中中断）才退回观察池偏样本。
  var sec = d.sector || {};
  var secDate = String(sec.date || "");
  var sgn = state.sectorSide === "down" ? -1 : 1;  // 涨=+1 只收高开；跌=-1 只收低开
  if ((sec.rows || []).length && (secDate === today8() || secDate === dataFreshness().date8)) {
    if (titleEl) titleEl.textContent = "板块竞价强度";
    // 涨/跌切换：先按方向过滤（跌榜混入高开行业会自相矛盾），再取幅度最大的 12 个；
    // 数组 reverse 后都是幅度最大的排在图表顶部
    rows = sec.rows.filter(function (r) { return sgn * r.open_pct > 0; })
      .sort(function (a, b) { return sgn * (b.open_pct - a.open_pct); })
      .slice(0, 12)
      .map(function (r) { return { name: r.n, pct: r.open_pct, n: null, amt: null, last: r.last_pct, full: true }; })
      .reverse();
    srcNote = "一级行业指数开盘缺口（全部成分股集合竞价撮合，共 " + sec.rows.length + " 个行业）"
      + (secDate !== today8() ? " · 数据日 " + mdOf(secDate) : "")
      + (rows.length ? "" : " · 当前无" + (state.sectorSide === "down" ? "低开" : "高开") + "行业");
  } else {
    // 回退：观察池聚合是偏样本（涨停池+热股为主），绝不能冒充「整个板块的强度」——
    // 标题必须显性降级，并给出恢复路径（采集器盘中静默死亡时 sector 会停在旧日期；
    // 服务端有自愈补抓，这里同时提示手动补救入口）
    if (titleEl) titleEl.textContent = "板块竞价强度 · 观察池偏样本";
    var items = currentItems(d); if (!items.length) return;
    var wm = d.watchmap || {};
    var agg = {};
    items.forEach(function (r) {
      var ind = ((wm[r.thscode] || {}).i) || "未分类";
      var a = agg[ind] || (agg[ind] = { pct: 0, n: 0, amt: 0 });
      if (r.auction_pct != null) { a.pct += r.auction_pct; a.n++; }
      a.amt += r.auction_amount || 0;
    });
    rows = Object.keys(agg).map(function (k) {
      var a = agg[k];
      return { name: k, pct: a.n ? a.pct / a.n : 0, n: a.n, amt: a.amt, full: false };
    }).filter(function (r) { return sgn * r.pct > 0; })
      .sort(function (x, y) { return sgn * (y.pct - x.pct); }).slice(0, 12).reverse();
    srcNote = (sec.date && sec.date !== today8()
        ? "板块指数数据为 " + sec.date + " 旧数据（采集盘中中断？）"
        : "今日板块指数未采集")
      + " · 以下为观察池行业聚合，非全板块口径"
      + (rows.length ? "" : " · 当前无" + (state.sectorSide === "down" ? "低开" : "高开") + "行业")
      + (d.fetching ? " · 后台补抓中…" : " · 点右上「立即补抓」恢复全成分口径");
  }
  $("sectorNote").textContent = srcNote;
  var top = rows;
  if (!top.length) {
    // 空态必须画在图体内：只靠右上角小字注释，用户看到的是一整块白板，分不清「没数据」还是「没渲染」
    var side = state.sectorSide === "down" ? "低开" : "高开";
    var msg;
    if ((sec.rows || []).length && (secDate === today8() || secDate === dataFreshness().date8)) {
      var opp = sec.rows.filter(function (r) { return sgn * r.open_pct <= 0; });
      var ext = opp.length ? opp.reduce(function (a, b) { return (sgn * b.open_pct) < (sgn * a.open_pct) ? b : a; }) : null;
      msg = (secDate !== today8() ? mdOf(secDate) + " " : "") + "全部 " + opp.length + " 个一级行业竞价" + side
        + (ext ? "（" + (sgn < 0 ? "最强 " : "最深 ") + fmtPct(ext.open_pct) + " · " + esc(ext.n) + "）" : "")
        + "，无" + side + "行业" + (state.sectorSide === "down" ? "；点「涨」看高开" : "；点「跌」看低开");
    } else {
      msg = "观察池内无" + side + "行业聚合（非全板块口径）";
    }
    sectorChart.setOption({
      textStyle: { fontFamily: CHART_FONT },
      graphic: [{ type: "text", left: "center", top: "middle", silent: true,
        style: { text: msg, fill: P().text3, fontSize: 13, fontFamily: CHART_FONT, lineHeight: 20 } }],
      xAxis: { show: false }, yAxis: { show: false }, series: []
    }, true);
    return;
  }
  sectorChart.setOption({
    textStyle: { fontFamily: CHART_FONT },
    grid: { left: 90, right: 60, top: 8, bottom: 22 },
    tooltip: { backgroundColor: P().panel, borderColor: P().line2, textStyle: { color: P().text1 },
      formatter: function (p) {
      var r = top[p.dataIndex];
      return r.name + "<br>开盘缺口 " + fmtPct(r.pct)
        + (r.full && r.last != null ? "<br>收盘涨跌 " + fmtPct(r.last) : "")
        + (!r.full && r.n != null ? "<br>池内 " + r.n + " 只 · 竞价额 " + fmtAmt(r.amt) : "");
    } },
    // 参考刻度用得上：窄面板里默认 5 等分会把 0/2/4/6/8/10% 挤成一团，柱端标签已有精确值，这里取稀疏整数档
    xAxis: { type: "value", axisLabel: { formatter: "{value}%", color: P().text3 }, splitLine: { lineStyle: { color: P().split } } },
    yAxis: { type: "category", data: top.map(function (r) { return r.name; }), axisLabel: { fontSize: 11, interval: 0, color: P().text2 }, axisLine: { lineStyle: { color: P().line } } },
    // 柱长=缺口幅度绝对值（零轴固定在左，跌榜与涨榜同向易读）；柱色/柱端标签保留真实方向
    series: [{
      type: "bar", barWidth: "62%",
      data: top.map(function (r) {
        return { value: Math.abs(r.pct), itemStyle: { color: r.pct >= 0 ? P().up : P().down } };
      }),
      label: { show: true, position: "right", fontSize: 10,
        formatter: function (p) { return top[p.dataIndex].pct.toFixed(2) + "%"; } }
    }]
  }, true);
}

/* ============ 自选卡片 ============ */
function renderSelf() {
  var d = state.payload; if (!d) return;
  var self = d.watchlist || [];
  if (!self.length) {
    // 观察池清空后必须同步清空卡片：只 return 会让上一轮渲染的旧自选永久挂在页面上
    $("selfNote").textContent = "";
    $("selfCards").innerHTML = '<div class="dim" style="padding:12px;font-size:12px">暂无自选，点「观察池」添加</div>';
    return;
  }
  var byCode = {};
  currentItems(d).forEach(function (r) { byCode[r.thscode] = r; });
  var html = "", hit = 0;
  self.forEach(function (tc) {
    var r = byCode[tc]; if (!r) return; hit++;
    var pc = pctClass(r.auction_pct);
    var px = num(r.auction_price);
    // 空数据要给原因而不是一排 "-"：920xxx 北交所等票源不覆盖/未参与撮合时，四个字段全空
    var empty = (r.auction_pct == null && px == null && !r.auction_amount && !r.auction_volume);
    html += '<div class="scard" data-code="' + esc(tc) + '">' +
      '<div class="nm"><span>' + esc(r.name || tc) + '</span><span class="cd">' + esc(r.ticker || "") + "</span></div>" +
      (empty
        ? '<div class="px dim" style="font-size:12px">无竞价数据</div>' +
          '<div class="sub"><span>源未覆盖或未参与撮合</span></div>'
        : '<div class="px ' + pc + '">' + (px != null ? px : "-") +
          ' <span style="font-size:12px">' + fmtPct(r.auction_pct) + "</span></div>" +
          '<div class="sub"><span>额 ' + fmtAmt(r.auction_amount) + '</span><span>量 ' + fmtVol(r.auction_volume) + "</span></div>") +
      "</div>";
  });
  $("selfCards").innerHTML = html || '<div class="dim" style="padding:12px;font-size:12px">自选股暂无竞价数据</div>';
  $("selfNote").textContent = hit + "/" + self.length;
}
$("selfCards").addEventListener("click", function (ev) {
  var c = ev.target.closest(".scard"); if (c) openCurve(c.dataset.code);
});

/* ============ 异动提醒（相邻两轮环比） ============ */
function detectAlerts(items, d) {
  var rnd = (d.live && d.live.round);
  if (!rnd || !state.prevItems || state.prevItems === items) return;
  var prev = {};
  state.prevItems.forEach(function (r) { prev[r.thscode] = r; });
  var ts = (rnd.ts || "").slice(11, 19);
  var noise = phaseInfo().noise;
  var fresh = [];
  items.forEach(function (r) {
    var p0 = prev[r.thscode]; if (!p0 || r.auction_pct == null || p0.auction_pct == null) return;
    var dp = r.auction_pct - p0.auction_pct;
    var v0 = p0.auction_volume || 0, v1 = r.auction_volume || 0;
    var volJump = v0 > 20 && v1 > v0 * 1.5;
    if (Math.abs(dp) >= 1.5 || volJump) {
      fresh.push({
        t: ts, code: r.thscode, name: r.name || r.ticker, noise: noise,
        msg: (Math.abs(dp) >= 1.5 ? "涨幅 " + fmtPct(p0.auction_pct) + "→" + fmtPct(r.auction_pct) + " " : "") +
             (volJump ? "匹配量 " + fmtVol(v0) + "→" + fmtVol(v1) : "")
      });
    }
  });
  if (fresh.length) state.alerts = fresh.concat(state.alerts).slice(0, 30);
}

/* 合并展示：页面实时增量（最新在前）+ 后端全天回算，同轮同股去重。
   盘中两源会重叠（实时检测过的轮次稍后也进 series 回算），按 时刻+代码 去重即可。 */
function renderAlertList() {
  var seen = {}, merged = [];
  (state.alerts || []).concat(state.histAlerts || []).forEach(function (a) {
    var k = a.t + "|" + a.code;
    if (seen[k]) return;
    seen[k] = 1; merged.push(a);
  });
  merged = merged.slice(0, 30);
  $("alertList").innerHTML = merged.length ? merged.map(function (a) {
    return '<li><span class="t">' + esc(a.t) + '</span><b>' + esc(a.name) + "</b> " + esc(a.msg) +
      (a.noise ? '<span class="tag-noise">可撤单期</span>' : "") + "</li>";
  }).join("") : '<li class="dim" style="border:none">等待数据…</li>';
  $("alertNote").textContent = merged.length ? merged.length + " 条 · 相邻两轮环比突变" : "相邻两轮环比突变";
}

/* ============ 09:25 定盘 ============ */
function renderFinal(d) {
  var fin = d.final, rnd = fin && fin.round;
  if (!rnd || !rnd.items || !rnd.items.length) {
    $("finalNote").textContent = !fin ? "9:25 撮合完成后冻结"
      : (fin.date && fin.date !== today8() ? "今日无终态（文件里只有 " + mdOf(fin.date) + " 的）" : "终态为空");
    state.finalItems = [];
    $("btnFinal2Quant").disabled = true;   // 无终态时禁用送量化（与候选面板按钮同口径）
    return;
  }
  var notes = [];
  if (fin.date && fin.date !== today8()) notes.push("数据日 " + mdOf(fin.date) + "（非今日）");
  if (fin.pre_market) notes.push("盘前补抓，可能为上一交易日口径");
  if (fin.provisional) notes.push("上游终态未就绪（" + (rnd.data_status || "?") + "），暂为盘中快照");
  $("finalNote").textContent = notes.length ? "⚠ " + notes.join(" · ")
    : (fin.mode === "manual" ? "手动补抓 · " + String(fin.fetched_at || "").slice(5, 19)
                              : "9:25 撮合完成后冻结");
  var items = rnd.items;
  var hi = items.filter(function (r) { return r.auction_pct > 0.005; });
  var lo = items.filter(function (r) { return r.auction_pct < -0.005; });
  var amt = items.reduce(function (s, r) { return s + (r.auction_amount || 0); }, 0);
  $("fvOpenRate").textContent = (hi.length / items.length * 100).toFixed(1) + "%";
  $("fvHigh").textContent = hi.length;
  $("fvLow").textContent = lo.length;
  $("fvAmount").textContent = fmtAmt(amt);
  var top = items.slice().sort(function (a, b) { return (b.auction_pct || 0) - (a.auction_pct || 0); }).slice(0, 10);
  $("finalBody").innerHTML = top.map(function (r) {
    return '<tr data-code="' + esc(r.thscode) + '"><td>' + esc(r.name || r.ticker) + '</td>' +
      '<td class="mono dim">' + esc(r.ticker) + '</td>' +
      '<td class="mono up">' + fmtPct(r.auction_pct) + '</td>' +
      '<td class="mono">' + fmtAmt(r.auction_amount) + "</td></tr>";
  }).join("");
  state.finalItems = items;
  $("btnFinal2Quant").disabled = false;
}
$("finalBody").addEventListener("click", function (ev) {
  var tr = ev.target.closest("tr[data-code]"); if (tr) openCurve(tr.dataset.code);
});
$("btnFinal2Quant").addEventListener("click", function () {
  var items = state.finalItems || [];
  var codes = items.slice().sort(function (a, b) { return (b.auction_pct || 0) - (a.auction_pct || 0); })
    .slice(0, 30).map(function (r) { return r.ticker; });
  goQuant(codes);
});
/* ============ 送入量化（代码清洗口径单一来源在 lib/util.js AK.cleanCodes） ============ */
function goQuant(codes) {
  var clean = AK.cleanCodes(codes);
  if (!clean.length) { toast("无可送入的代码", "err"); return; }
  try { localStorage.setItem("A_QUANT_POOL", clean.join(" ")); } catch (e) {}
  toast("已送入量化标的池（" + clean.length + " 只）", "ok");
  setTimeout(function () { location.href = "/quant"; }, 500);
}

/* ============ 基准 ============ */
function renderBench(d) {
  var b = d.benchmark;
  if (!b || !(b.items || []).length) { $("benchNote").textContent = "暂无"; return; }
  $("benchNote").textContent = (b.date || "") + " · " + b.items.length + " 条";
  $("benchList").innerHTML = b.items.map(function (r) {
    return "<li><span>" + esc(r.name || r.ticker) +
      ' <span class="up mono">' + fmtPct(r.auction_pct) + "</span></span>" +
      '<span class="tags">' + esc((r.tags || []).join(" / ")) + "</span></li>";
  }).join("");
}

/* ============ 右栏补充：强势候选 / 涨停接力 / 采集体检（高开兑现已并入左列定盘面板） ============
   四个面板全部只读 payload 里已有字段（items + watchmap + rounds_meta + status），
   不新增任何上游接口调用。 */
var CAND_RULES = {
  loose:  { pct: 1, ratio: 1.5, imb: 0.05, amt: 2e6, txt: "高开≥1% · 量比昨≥1.5 · 剩/匹≥5%" },
  std:    { pct: 2, ratio: 2,   imb: 0.15, amt: 3e6, txt: "高开≥2% · 量比昨≥2 · 剩/匹≥15%" },
  strict: { pct: 3, ratio: 3,   imb: 0.30, amt: 5e6, txt: "高开≥3% · 量比昨≥3 · 剩/匹≥30%" }
};
/* ---- 强势候选：高开 + 放量 + 买方承接（门槛 + 承接强度 + 强度分排序） ----
   2026-09-04 优化：旧规则对承接只看「委买剩余>0」这个方向条件——剩余 311 手
   （剩/匹 1%）与剩余 62 万手（剩/匹 994%）同等算「有承接」，弱承接股混进榜单；
   排序纯按高开，也让弱承接股常年压住强承接股。现在：
   ① 剩余必须占匹配量一定比例（剩/匹 = 未匹配/匹配量）才算真承接；
   ② 竞价额下限滤掉小资金就能打出的假高开（200/300/500 万三档）；
   ③ 排序按强度分 = 高开 + 量比分（min(量比,10)×0.5）+ 承接分（min(剩/匹,500%)×2），
     双封顶防单因子爆表；行悬停可见分项。
   量比缺失的股票不评（明示数量，不静默吞掉）。 ---- */
function imbOf(x) {
  var vol = x.auction_volume || 0;
  return vol > 0 ? (x.auction_unmatched || 0) / vol : 0;
}
function candScore(x) {
  return (x.auction_pct || 0) + Math.min(vrMult(x) || 0, 10) * 0.5 + Math.min(imbOf(x), 5) * 2;
}
function limitPriceOf(x) {   // 精确涨停价（口径同 limitPctOf：主板10/创业科创20/北交30）
  var p = x.pre_close_price;
  return p == null ? null : Math.round(p * (1 + limitPctOf(x.ticker) / 100) * 100) / 100;
}
function renderCand() {
  var p = state.payload || {}, items = currentItems(p), wm = p.watchmap || {};
  var rule = CAND_RULES[state.candKey] || CAND_RULES.std;
  var nNoVr = 0;   // 量比缺失被淘汰数
  var hit = items.filter(function (x) {
    if (x.auction_pct == null || x.auction_pct < rule.pct) return false;
    if ((x.auction_amount || 0) < rule.amt) return false;
    var vr = vrMult(x);
    if (vr == null) { nNoVr++; return false; }
    if (vr < rule.ratio) return false;
    return (x.auction_unmatched || 0) > 0 && imbOf(x) >= rule.imb;
  }).sort(function (a, b) {
    return candScore(b) - candScore(a) || (b.auction_pct || 0) - (a.auction_pct || 0);
  });
  state.candCodes = hit.map(function (x) { return x.ticker || x.thscode; });
  var fr = dataFreshness();
  var noteEl = $("candNote");
  var vrTail = nNoVr ? " · 量比缺" + nNoVr + "只未评" : "";
  noteEl.textContent = items.length
    ? (hit.length ? rule.txt + " → " + hit.length + " 只 · 按强度分排序" + vrTail + staleTail(fr)
                  : "无标的通过筛选（可切「宽」）" + vrTail + staleTail(fr))
    : "暂无数据";
  noteEl.className = "note" + (fr.today === false && hit.length ? " warn-txt" : "");
  $("btnCand2Quant").disabled = !hit.length;
  if (!hit.length) {
    $("candBody").innerHTML = '<tr><td colspan="4" class="dim" style="text-align:center;padding:16px">' +
      (items.length ? "无标的命中" : "暂无数据") + "</td></tr>";
    return;
  }
  $("candBody").innerHTML = hit.slice(0, 20).map(function (x) {
    // 接＝上一交易日涨停股；一＝涨停价开盘（排板形态，承接比天然巨大，别当普通高开读）
    var badges = srcOf(wm, x.thscode).indexOf("L") >= 0
      ? '<span class="relay-badge" title="上一交易日涨停股">接</span>' : "";
    var lp = limitPriceOf(x);
    if (x.auction_price != null && lp != null && x.auction_price >= lp - 1e-6) {
      badges += '<span class="relay-badge lim" title="涨停价开盘 · 排板形态">一</span>';
    }
    var vr = vrMult(x) || 0;
    return '<tr data-code="' + esc(x.thscode) + '" title="强度分 ' + candScore(x).toFixed(1) +
      " ＝ 高开 " + (x.auction_pct || 0).toFixed(2) +
      " + 量比分 " + (Math.min(vr, 10) * 0.5).toFixed(1) +
      " + 承接分 " + (Math.min(imbOf(x), 5) * 2).toFixed(1) + '">' +
      '<td>' + esc(x.name || x.ticker) + badges +
      '<span class="sub-ind">' + esc((wm[x.thscode] || {}).i || "未分类") + "</span></td>" +
      '<td class="mono ' + pctClass(x.auction_pct) + '">' + fmtPct(x.auction_pct) + "</td>" +
      '<td class="mono">' + (vrMult(x) || 0).toFixed(2) + "</td>" +
      '<td class="mono">' + fmtVol(x.auction_unmatched) +
      '<span class="sub-ind">' + (imbOf(x) * 100).toFixed(0) + "%</span></td></tr>";
  }).join("") + (hit.length > 20
    ? '<tr><td colspan="4" class="dim" style="font-size:11px;padding:6px">余 ' + (hit.length - 20) + " 只未列出（送量化仍含全部）</td></tr>" : "");
}
function hms2sec(hms) {
  var a = String(hms || "").split(":");
  return a.length === 3 ? (+a[0]) * 3600 + (+a[1]) * 60 + (+a[2]) : NaN;
}
function limitPctOf(code) {   // 涨停幅度口径：创业/科创 20%，北交所 30%，主板 10%
  var c = String(code || "");
  if (c.indexOf("30") === 0 || c.indexOf("68") === 0) return 20;
  if (c.indexOf("92") === 0 || c.indexOf("4") === 0 || c.indexOf("8") === 0) return 30;
  return 10;
}
function avgOf(list, f) {
  var v = list.map(f).filter(function (x) { return x != null && !isNaN(x); });
  return v.length ? v.reduce(function (a, b) { return a + b; }, 0) / v.length : null;
}
function medianOf(list, f) {
  var v = list.map(f).filter(function (x) { return x != null && !isNaN(x); })
    .sort(function (a, b) { return a - b; });
  if (!v.length) return null;
  var m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}
function srcOf(wm, tc) { return String(((wm || {})[tc] || {}).s || ""); }
/* 量比昨（倍数）：上游全量字段只有 auction_yesterday_ratio_pct——注意它虽叫 _pct，
   但实测数值就是昨同刻量比倍数（终态中位数 2.6、最高 132，≥2 的 64/103），不要再除 100；
   旧代码读的 auction_volume_ratio 仅在 09:15 首轮（not_ready 残留）偶发返回，导致强势候选常年空转。 */
function vrMult(x) {
  var v = x && x.auction_yesterday_ratio_pct;
  return (v == null || isNaN(v)) ? null : v;
}
function staleTail(fr) {
  return (fr.today === false && fr.hasAny) ? "（展示 " + mdOf(fr.date8) + " 数据）" : "";
}
function kvRows(rows) {
  return rows.map(function (r) {
    return '<div class="k">' + r[0] + '</div><div class="v ' + (r[2] || "") + '">' + r[1] + "</div>";
  }).join("");
}

$("candSeg").addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-k]"); if (!b) return;
  state.candKey = b.dataset.k;
  Array.prototype.forEach.call(this.querySelectorAll("button"), function (x) { x.classList.toggle("on", x === b); });
  renderCand();
});
$("candTable").addEventListener("click", function (ev) {
  var tr = ev.target.closest("tr[data-code]"); if (tr) openCurve(tr.dataset.code);
});
$("btnCand2Quant").addEventListener("click", function () { goQuant(state.candCodes); });

/* ---- 涨停接力：上一交易日涨停池今日竞价表现（赚钱效应能不能接上） ----
   只做池子级统计；涨停股逐只榜单不再是独立 TOP 表（与强势候选重复排同一批股票），
   改为强势候选表内「接」徽标。 ---- */
function renderRelay() {
  var p = state.payload || {}, items = currentItems(p), wm = p.watchmap || {};
  var fr = dataFreshness();
  var el = $("relayStats"), note = $("relayNote");
  if (!items.length) { el.innerHTML = '<div class="k">暂无数据</div>'; return; }
  if (!items.some(function (x) { return srcOf(wm, x.thscode); })) {
    el.innerHTML = '<div class="k">观察池来源未标注</div><div class="v dim">重跑一次采集后可见</div>';
    note.textContent = "需新版采集器写 watchmap.s";
    return;
  }
  var isL = function (x) { return srcOf(wm, x.thscode).indexOf("L") >= 0; };
  var L = items.filter(isL), rest = items.filter(function (x) { return !isL(x); });
  if (!L.length) {
    el.innerHTML = '<div class="k">池内无上一交易日涨停股</div><div class="v dim">看「观察池」构成</div>';
    return;
  }
  var red = L.filter(function (x) { return (x.auction_pct || 0) > 0; }).length;
  var zt = L.filter(function (x) { return (x.auction_pct || 0) >= limitPctOf(x.ticker) - 0.2; }).length;
  var avgL = avgOf(L, function (x) { return x.auction_pct; });
  var avgR = avgOf(rest, function (x) { return x.auction_pct; });
  var gap = (avgL != null && avgR != null) ? avgL - avgR : null;
  el.innerHTML = kvRows([
    ["入池涨停股", L.length + " 只", ""],
    ["红盘率", (red / L.length * 100).toFixed(0) + "%", red / L.length >= 0.5 ? "up" : "down"],
    ["平均竞价涨幅", fmtPct(avgL), pctClass(avgL)],
    ["池内其余均值", avgR == null ? "-" : fmtPct(avgR), pctClass(avgR)],
    ["接力差值", gap == null ? "-" : (gap >= 0 ? "+" : "") + gap.toFixed(2) + "pct", gap == null ? "dim" : (gap >= 0 ? "up" : "down")],
    ["涨停价开盘", zt + " 只", zt ? "up" : "dim"]
  ]);
  note.textContent = "上一交易日涨停池 → 今日竞价 · 个股见强势候选「接」标" + staleTail(fr);
  note.className = "note" + (fr.today === false ? " warn-txt" : "");
}

/* ---- 定盘 → 现价兑现（原「高开兑现」独立面板）：竞价信号到底有没有用 ---- */
function renderRealize() {
  var p = state.payload || {}, items = currentItems(p);
  var el = $("realizeStats"), note = $("realizeNote");
  var ph = phaseInfo();
  if (!items.length) { el.innerHTML = '<div class="k">暂无数据</div>'; return; }
  if (ph.key === "live" || ph.key === "lock") {
    el.innerHTML = '<div class="k">竞价未结束</div><div class="v dim">09:30 后可算</div>';
    note.textContent = "定盘 → 开盘 → 现价（开盘后生效）";
    return;
  }
  var hi = items.filter(function (x) { return (x.auction_pct || 0) > 0; });
  var wOpen = hi.filter(function (x) { return x.open_price != null && x.pre_close_price; });
  var withLast = items.filter(function (x) {
    return x.last_price != null && x.auction_price && x.pre_close_price;
  });
  if (!wOpen.length && !withLast.length) {
    el.innerHTML = '<div class="k">尚无开盘/现价</div><div class="v dim">上游未返回或尚未开盘</div>';
    return;
  }
  var kept = wOpen.filter(function (x) { return x.open_price >= x.pre_close_price; }).length;
  var dl = function (x) { return (x.last_price - x.auction_price) / x.pre_close_price * 100; };
  var med = medianOf(withLast, dl);
  var above = withLast.filter(function (x) { return dl(x) > 0.05; }).length;
  var rnd = (p.live && p.live.round) || (p.final && p.final.round) || {};
  // 高开只数行已删：与本面板顶部定盘统计卡（定盘口径）重复，现价轮口径差异只会造成困惑
  el.innerHTML = kvRows([
    ["开盘守住昨收", wOpen.length ? kept + " 只（" + (kept / wOpen.length * 100).toFixed(0) + "%）" : "-",
      wOpen.length && kept / wOpen.length >= 0.5 ? "up" : "down"],
    ["定盘→现价 中位", med == null ? "-" : fmtPct(med), pctClass(med)],
    ["现价高于定盘", withLast.length ? above + " 只（" + (above / withLast.length * 100).toFixed(0) + "%）" : "-",
      above >= withLast.length / 2 ? "up" : "flat"]
  ]);
  note.textContent = (rnd.ts ? "现价取自 " + String(rnd.ts).slice(11, 19) + " 那轮（非实时）" : "定盘 → 开盘 → 现价") +
    staleTail(dataFreshness());
  note.className = "note" + (dataFreshness().today === false ? " warn-txt" : "");
}

/* ---- 强弱转换：09:15–09:25 竞价期间匹配价的逐轮轨迹（不看盘后现价） ----
   弱转强＝起点低开且匹配价被买盘抬上去；强转弱＝起点高开且一路回落。
   2026-09-04 优化（此前只比首末两点，起点还落在最不可信的位置）：
   ① 起点锚定 09:20 —— 09:15–09:20 可撤单、骗单多，那一带的"低开/高开"不可信；
     数据未覆盖 09:20 后才回退窗口首轮，并在面板备注明示。
   ② 终点锚定真定盘 —— 同日且 09:26 前抓取的 final 轮才是 09:25 撮合值，追加为终点帧；
     午后手动补抓的 final 是抓取时刻现价快照（2026-09-04 实测 65/70 只数值被现价污染），
     绝不能混进来，此时终点退回最后一轮并明示。
   ③ 形态确认 —— 终点必须是起点之后的全程极值（w2s=最高 / s2w=最低）：
     中途冲高又回落、挖坑又收回的假转换不再入选（对应"一路抬上去/一路回落"的本义）。
   ④ 水平确认 —— w2s 终点须红盘、s2w 终点须绿盘，排掉"转了但没转过 0"的边缘噪声。
   ⑤ 流动性下限 FLIP_AMT（与强势候选中档一致）：小票竞价额太小，转换信号无意义。
   轮次不足时仍诚实置灰，绝不拿盘后定格快照冒充轨迹。 ---- */
var FLIP_DELTA = 0.5;   // 起点→终点的命中阈值（pct 点）
var FLIP_AMT = 3e6;     // 终点轮竞价额下限（元），与强势候选「中」档一致
function renderFlip() {
  var d = state.payload || {}, wm = d.watchmap || {};
  var k = state.flipKey || "w2s", el = $("flipBody"), note = $("flipNote");
  var empty = function (msg) {
    el.innerHTML = '<tr><td colspan="4" class="dim" style="text-align:center;padding:16px">' + msg + "</td></tr>";
    if (note) note.textContent = "";
  };
  var rounds = ((state.series || {}).rounds || []).filter(function (r) { return r.items && roundInWindow(r); });
  rounds.sort(function (a, b) { return String(a.ts || "").localeCompare(String(b.ts || "")); });
  if ((state.series || {}).date !== today8()) {
    var oldSd = (state.series || {}).date;
    empty("等待今日逐轮竞价数据" + (oldSd ? "（当前是 " + oldSd + " 的旧轨迹）" : ""));
    return;
  }
  /* 终点可信门：final 轮只有「当日 + 09:26 前抓取」才是 09:25 撮合值。
     午后手动补抓（如 2026-09-04 12:28 那次）写进 final 的是现价快照，
     混进轨迹会把盘中涨跌冒充成竞价转换。 */
  var fin = d.final || {};
  var finT = String(fin.fetched_at || "").slice(11, 19);
  var finOK = fin.round && (fin.round.items || []).length &&
    fin.date === (state.series || {}).date && finT && finT <= "09:26:00";
  var frames = rounds.map(function (r) { return { ts: r.ts, t: String(r.ts || "").slice(11, 19), items: r.items, fin: false }; });
  if (finOK) frames.push({ ts: fin.round.ts, t: String(fin.round.ts || "").slice(11, 19), items: fin.round.items, fin: true });
  if (frames.length < 2) { empty("窗口内仅 " + frames.length + " 帧，连不成轨迹（需≥ 2 帧，由 09:14 任务自动采）"); return; }
  frames.sort(function (a, b) { return String(a.ts || "").localeCompare(String(b.ts || "")); });
  /* 起点：优先 09:20 后该股首个有效轮（不可撤单窗口）；
     09:15 首轮上游常为 not_ready，历史上锁第一轮曾把整个面板转空（09-03 实测 75 只全跳过）。 */
  var m0 = {}, startMin = null, startAnchor = false;
  frames.forEach(function (r, ri) {
    if (r.t < "09:20:00") return;
    (r.items || []).forEach(function (x) {
      if (x.auction_pct == null || m0[x.thscode]) return;
      m0[x.thscode] = { x: x, ri: ri };
      startAnchor = true;
      if (startMin == null) startMin = r.ts;
    });
  });
  if (startMin == null) {   // 数据未覆盖 09:20 后（如采集中断日）：回退窗口首轮，备注明示
    frames.forEach(function (r, ri) {
      (r.items || []).forEach(function (x) {
        if (x.auction_pct == null || m0[x.thscode]) return;
        m0[x.thscode] = { x: x, ri: ri };
        if (startMin == null) startMin = r.ts;
      });
    });
  }
  if (startMin == null) { empty("窗口内无有效匹配涨幅（各轮均 not_ready）"); return; }
  /* 每股轨迹（从其起点帧起）与终点帧 */
  var traj = {}, endI = {};
  frames.forEach(function (r, ri) {
    (r.items || []).forEach(function (x) {
      if (x.auction_pct == null) return;
      var s = m0[x.thscode];
      if (!s || ri < s.ri) return;
      (traj[x.thscode] = traj[x.thscode] || []).push(x.auction_pct);
      endI[x.thscode] = x;
    });
  });
  var lastFrame = frames[frames.length - 1], rows = [];
  Object.keys(endI).forEach(function (c) {
    var a = m0[c], b = endI[c], tl = traj[c] || [];
    if (!a || tl.length < 2) return;   // 起点即终点＝没有轨迹可言
    var fl = b.auction_pct - a.x.auction_pct;
    var fromSide = k === "w2s" ? a.x.auction_pct < 0 : a.x.auction_pct > 0;   // 弱转强从低开始/强转弱从高开始
    var extreme = k === "w2s" ? b.auction_pct >= Math.max.apply(null, tl)
                             : b.auction_pct <= Math.min.apply(null, tl);
    var level = k === "w2s" ? b.auction_pct > 0 : b.auction_pct < 0;
    if (!fromSide || Math.abs(fl) < FLIP_DELTA || !extreme || !level) return;
    if ((b.auction_amount || 0) < FLIP_AMT) return;
    rows.push({ tc: c, name: b.name, ap: a.x.auction_pct, bp: b.auction_pct, fl: fl, b: b });
  });
  rows.sort(function (x, y) { return k === "w2s" ? y.fl - x.fl : x.fl - y.fl; });
  if (note) note.textContent = "起点 " + String(startMin || "").slice(11, 19) +
    (startAnchor ? "（09:20锁点）" : "（未覆盖09:20后）") + " → 终点 " +
    String(lastFrame.ts || "").slice(11, 19) + (finOK ? "（定盘）" : "") + " · 变化≥" + FLIP_DELTA + "点";
  if (!rows.length) { el.innerHTML = '<tr><td colspan="4" class="dim" style="text-align:center;padding:16px">今日无「' +
    (k === "w2s" ? "弱转强" : "强转弱") + "」标的</td></tr>"; return; }
  el.innerHTML = rows.map(function (r) {
    var badges = srcOf(wm, r.tc).indexOf("L") >= 0
      ? '<span class="relay-badge" title="上一交易日涨停股">接</span>' : "";
    var lp = limitPriceOf(r.b);
    if (r.b.auction_price != null && lp != null && r.b.auction_price >= lp - 1e-6) {
      badges += '<span class="relay-badge lim" title="竞价拉至涨停价开盘">一</span>';
    }
    return '<tr data-code="' + esc(r.tc) + '"><td>' + esc(r.name || r.tc) + badges +
      '<span class="sub-ind">' + esc((wm[r.tc] || {}).i || "未分类") + "</span></td>" +
      '<td class="mono ' + pctClass(r.ap) + '">' + fmtPct(r.ap) + "</td>" +
      '<td class="mono ' + pctClass(r.bp) + '">' + fmtPct(r.bp) + "</td>" +
      '<td class="mono ' + pctClass(r.fl) + '">' + fmtPct(r.fl) + "</td></tr>";
  }).join("");
}
$("flipSeg").addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-k]"); if (!b) return;
  state.flipKey = b.dataset.k;
  Array.prototype.forEach.call(this.querySelectorAll("button"), function (x) { x.classList.toggle("on", x === b); });
  renderFlip();
});
$("flipTable").addEventListener("click", function (ev) {
  var tr = ev.target.closest("tr[data-code]"); if (tr) openCurve(tr.dataset.code);
});

/* ---- 昨日备选池·执行：把「纪律执行是收益的一半」工具化（2026-09-04） ----
   判定在后端 pool_exec（冻结执行层窗口，与验证器/最终方案同一份单一来源）：
   低开不接 / 高开超窗放弃 / 窗内观察。竞价进行中涨幅是暂定值，定盘后即最终判定。
   顶部情绪前瞻：昨涨停池低开占比 >50% 桶在回验中当日池均益为负（样本小，仅提示）。 */
function renderPoolExec() {
  var p = state.payload || {}, pe = p.pool_exec || {};
  var el = $("pexBody"), note = $("pexNote"), sig = $("pexSig");
  if (!pe.ok) {
    el.innerHTML = '<tr><td colspan="3" class="dim" style="text-align:center;padding:16px">' +
      esc(pe.note || "暂无备选池数据") + "</td></tr>";
    if (sig) { sig.textContent = ""; sig.className = "note"; }
    return;
  }
  var kindCls = { ok: "up", skip: "down", missing: "dim" };
  el.innerHTML = pe.rows.map(function (r) {
    return '<tr data-code="' + esc(r.code) + '"><td>' + esc(r.name || r.code) +
      '<span class="relay-badge' + (r.grp === "nlu" ? "" : " lim") + '" title="' + r.grp_cn + '">' +
      (r.grp === "nlu" ? "低" : "涨") + "</span>" +
      '<span class="sub-ind">' + esc((r.concept || "-") + (r.score ? " · " + r.score + "分" : "")) + "</span></td>" +
      '<td class="mono ' + (r.pct == null ? "dim" : pctClass(r.pct)) + '">' +
      (r.pct == null ? "-" : fmtPct(r.pct)) + "</td>" +
      '<td><span class="' + (kindCls[r.kind] || "dim") + '">' + esc(r.verdict) + "</span>" +
      '<div class="sub-ind" title="' + esc(r.window) + " · " + esc(r.plan) + '">' +
      esc(r.plan) + "</div></td></tr>";
  }).join("");
  note.textContent = (pe.pool_date ? mdOf(pe.pool_date) + " 池" + (pe.env_cn ? "·" + pe.env_cn : "") + " · " : "") +
    "可观察 " + pe.counts.ok + " · 放弃 " + pe.counts.skip +
    (pe.counts.missing ? " · 未入池 " + pe.counts.missing : "") +
    (pe.last_ts ? " · 截至 " + pe.last_ts : "");
  if (sig) {
    var s = pe.signal;
    sig.textContent = s
      ? ("情绪前瞻：昨涨停池低开 " + (s.open_down * 100).toFixed(0) + "%（深低开≤-2% 占 " +
         (s.open_deep * 100).toFixed(0) + "%，n=" + s.lu_n + "）" +
         (s.warn ? " ⚠ 已过 50% 警戒（回验：该桶当日备选池均益 -1.08%）" : "") + " · " + s.study)
      : "情绪前瞻：池内无昨涨停股或暂无竞价轮次";
    sig.className = "note" + (s && s.warn ? " warn-txt" : "");
  }
}
$("pexTable").addEventListener("click", function (ev) {
  var tr = ev.target.closest("tr[data-code]"); if (tr) openCurve(tr.dataset.code);
});

/* ---- 今日采集体检：逐轮覆盖条 + 池子构成 + 失败与终态状态 ----
   这个面板的存在理由：采集默默失效时（日历误判/任务没跑/批次报错）页面以前完全看不出来。 */
function renderCheck() {
  var p = state.payload || {}, cfg = p.config || {}, st = p.status || {};
  var rm = p.rounds_meta || {}, rounds = rm.rounds || [];
  var wm = p.watchmap || {}, wl = p.watchlist || [];
  var fr = dataFreshness();
  var step = cfg.interval || 30;
  var start = hms2sec(cfg.live_start || "09:15:00"), end = hms2sec(cfg.live_end || "09:24:59");
  var fin = hms2sec(cfg.final_at || "09:25:10");
  var slots = [];
  for (var t = start; !isNaN(t) && !isNaN(end) && t <= end; t += step) slots.push({ sec: t, kind: "live" });
  if (!isNaN(fin)) slots.push({ sec: fin, kind: "final" });
  var inWinAll = rounds.filter(roundInWindow);
  var snapAll = rounds.filter(function (r) { return !roundInWindow(r); });
  // rounds_meta 是隔日残留时不得计入今日覆盖（否则昨天空条会被当成今天采齐了）
  var metaToday = !rm.date || rm.date === today8();
  var inWin = metaToday ? inWinAll : [];
  var snaps = metaToday ? snapAll : [];
  var secOf = function (r) { return hms2sec(String(r.ts || "").slice(11, 19)); };
  var covered = function (slot) {
    return inWin.some(function (r) {
      var s = secOf(r);
      // 一轮只能顶一个槽位：ts 是抓取结束时刻（比槽位晚几秒到十几秒），所以下界留 5 秒、
      // 上界取到下一槽（+step）为止。写成 +step*2 会让一轮顶两格，把真漏采盖掉。
      return !isNaN(s) && s >= slot.sec - 5 && s < slot.sec + step;
    });
  };
  var hhmmss = function (sec) {
    return pad2(Math.floor(sec / 3600) % 24) + ":" + pad2(Math.floor(sec / 60) % 60);
  };
  var hitN = 0;
  $("covBar").innerHTML = slots.map(function (sl) {
    var ok = covered(sl);
    if (ok) hitN++;
    var cls = ok ? (sl.kind === "final" ? "cov fin" : "cov hit") : "cov";
    return '<i class="' + cls + '" title="' + hhmmss(sl.sec) + (sl.kind === "final" ? " 定盘" : " live 轮") +
      (ok ? " 已采" : " 缺轮") + '"></i>';
  }).join("") + (snaps.length
    ? '<i class="cov snap" title="窗口外补抓 ' + String(snaps[snaps.length - 1].ts || "").slice(11, 19) +
      "（只有定盘冻结值）\"></i>" : "");
  var nLim = 0, nHot = 0;
  Object.keys(wm).forEach(function (k) {
    var s = srcOf(wm, k);
    if (s.indexOf("L") >= 0) nLim++;
    if (s.indexOf("H") >= 0) nHot++;
  });
  // rounds_meta.rounds[].errors 是数字（失败批次数），与 round.errors（数组）同名不同型，见 renderStats 注释
  var errs = inWin.reduce(function (a, r) { return a + (r.errors || 0); }, 0);
  // 池子总数只数带来源标注的代码：payload 的 watchmap 可能被展示层兑底补过
  // 今日 items 里的老池代码（s 为空的临时条目），拿字典长度会把兑底算进池子。
  var nPool = Object.keys(wm).filter(function (k) { return wm[k] && wm[k].s; }).length;
  // 行业覆盖：展示层兑底后应恒 100%；不满 = industry_map 没覆盖到（如未进指数成分的新股），看得见才不会静默退「未分类」
  var itemsC = currentItems(p);
  var indHit = itemsC.filter(function (x) { return (wm[x.thscode] || {}).i; }).length;
  var finToday = fr.finalToday;
  var rows = [
    ["窗口内逐轮", hitN + " / " + slots.length + " 格",
      hitN === slots.length ? "up" : (inWin.length ? "warn-txt" : "bad-txt")],
    ["时序数据日", metaToday ? (rm.date ? "今日" : "暂无轮次记录") : "旧数据 " + rm.date + "，不计入覆盖",
      metaToday ? "dim" : "warn-txt"],
    ["盘外补抓", snaps.length ? String(snaps[snaps.length - 1].ts || "").slice(11, 19) + "（只当定格快照）" : "无",
      snaps.length && !inWin.length ? "warn-txt" : "dim"],
    ["观察池", "自选 " + wl.length + " · 涨停池 " + nLim + " · 热股 " + nHot + " = " + nPool +
      " / 封顶 " + (cfg.max_codes || 300), ""],
    ["行业覆盖", itemsC.length ? indHit + " / " + itemsC.length : "—",
      itemsC.length && indHit === itemsC.length ? "up" : "warn-txt"],
    ["批次失败", errs ? errs + " 批" : "0", errs ? "warn-txt" : "dim"],
    ["终态", fr.hasAny ? (finToday ? "今日已取" : "今日缺终态") : "无", finToday ? "up" : "warn-txt"],
    ["数据日", fr.hasAny ? fr.date8 + (fr.today === false ? "（隔日）" : "") : "—",
      fr.today === false ? "warn-txt" : "dim"],
    ["上次采集", st.last_run || "—", "dim"]
  ];
  if (st.note) rows.push(["任务提示", esc(st.note), "warn-txt"]);
  if (!(wl.length || nLim || nHot)) rows.push(["来源标注", "未记录（旧数据）", "dim"]);
  $("checkStats").innerHTML = kvRows(rows);
  $("checkNote").textContent = "深灰=采到 · 金=定盘 · 空=缺轮 · 浅灰=盘外补抓";
}

/* ============ 个股竞价曲线弹窗 ============ */
var curveChart = echarts.init($("curveChart"));
function openCurve(code) {
  state.curveCode = code;
  $("curveMask").classList.add("show");
  curveChart.resize();
  // 先用缓存占位，再无条件拉一次：ETag 命中就 304，很轻。
  // force=true 同时绕过 15s 节流（关掉弹窗再打开时 curveCode 为空、轮询不跑，不重拉就会给出一条过期曲线）。
  if (state.series) renderCurve();
  fetchSeries(true);
}
/* 双通道触发（applyPayload 每 30s 一次 + 定时器每 30s 一次）在竞价期会撞出并发请求：
   15s 节流把两路收敛成一路；force=true（打开曲线弹窗）绕过节流。
   请求序号保证慢响应后到不会覆盖更新过的 state.series。 */
var _seriesSeq = 0, _seriesLast = 0;
function fetchSeries(force) {
  var now = Date.now();
  if (!force && now - _seriesLast < 15000) return;
  _seriesLast = now;
  var seq = ++_seriesSeq;
  var headers = {};
  if (state.seriesEtag) headers["If-None-Match"] = state.seriesEtag;
  fetch("/api/auction/series", { headers: headers }).then(function (r) {
    if (r.status === 304) return null;
    state.seriesEtag = r.headers.get("ETag");
    return r.json();
  }).then(function (d) {
    if (seq !== _seriesSeq) return;
    if (d) { state.series = d; safeRender(renderCurve); safeRender(renderFlip); }
  }).catch(function () {});
}
function renderCurve() {
  var code = state.curveCode; if (!code || !state.payload) return;
  var s = state.series || {};
  var all = (s.rounds || []).filter(function (r) { return r.items; });
  // 按 ts 排序：逐轮与补抓混合时 append 顺序不等于时间顺序，乱序会画出左右跳的曲线
  all.sort(function (a, b) { return String(a.ts || "").localeCompare(String(b.ts || "")); });
  // 只有竞价窗口内的轮次能当逐轮：窗口外（盘后补抓）拿到的永远是 09:25 定盘冻结值，
  // 多条全等快照连起来就是一条假曲线，所以单独取最新一条当参考点。
  var auc = all.filter(roundInWindow);
  var snaps = all.filter(function (r) { return !roundInWindow(r); });
  var snap = snaps.length ? snaps[snaps.length - 1] : null;
  var rounds = auc.length ? auc : (snap ? [snap] : []);
  var xs = [], pcts = [], vols = [];
  rounds.forEach(function (r) {
    var hit = r.items.find(function (x) { return x.thscode === code; });
    if (!hit) return;
    var slot = (r.ts || "").slice(11, 19);
    var canCancel = slot < "09:20:00";
    xs.push(auc.length ? slot : ("补抓 " + slot));
    var pv = num(hit.auction_pct);
    // 颜色必须是字面色：canvas 不解析 CSS 变量（var(--text-3) 会落回默认色）
    pcts.push(pv != null
      ? { value: Number(pv.toFixed(3)),
          itemStyle: { color: auc.length ? (canCancel ? P().amber : P().up) : P().text3 } }
      : null);
    vols.push(num(hit.auction_volume) || 0);
  });
  var onlySnap = !auc.length && !!snap;
  var ph = phaseInfo();
  var liveAuc = ph.key === "live" || ph.key === "lock";   // 正在竞价：价都还是暂定值，不能叫「定盘」
  var cur = currentItems(state.payload).find(function (x) { return x.thscode === code; });
  var nm = cur ? (cur.name || code) : (((state.payload.watchmap || {})[code] || {}).n || code);
  $("curveTitle").textContent = nm + (onlySnap ? " · 竞价定盘（补抓快照）"
                                 : liveAuc ? " · 竞价曲线（进行中）" : " · 竞价曲线");
  var meta = cur ? (
    (liveAuc ? "匹配价" : "定盘价") + " <b class='mono'>" + (cur.auction_price != null ? cur.auction_price : "-") + "</b> · " +
    (liveAuc ? "涨幅（暂定）" : "竞价涨幅") + " <b class='mono " + pctClass(cur.auction_pct) + "'>" + fmtPct(cur.auction_pct) + "</b> · " +
    "竞价额 <b class='mono'>" + fmtAmt(cur.auction_amount) + "</b> · 未匹配 <b class='mono'>" +
      (cur.auction_unmatched != null ? cur.auction_unmatched : "-") + "</b>"
  ) : "（本轮无该股数据）";
  // 开盘/现价只有竞价结束后才有意义（盘中值，不是竞价口径）
  if (cur && !liveAuc && (cur.open_price != null || cur.last_price != null)) {
    meta += " · 开盘 <b class='mono'>" + (cur.open_price != null ? cur.open_price : "-") + "</b> · " +
            "现价 <b class='mono'>" + (cur.last_price != null ? cur.last_price : "-") + "</b>";
  }
  $("curveMeta").innerHTML = meta;
  var fr = dataFreshness();
  var note;
  if (auc.length) {
    note = "共 " + auc.length + " 轮竞价逐轮（" +
      (auc[0].ts || "").slice(11, 19) + " → " + (auc[auc.length - 1].ts || "").slice(11, 19) +
      "）：黄点 09:15–09:20 可撤单（可能含虚假申报），红点 09:20–09:25 不可撤单。";
    if (snap) note += " 另有 " + (snap.ts || "").slice(11, 16) + " 盘后补抓一轮，与定盘全等，不计入曲线。";
  } else if (onlySnap) {
    note = "当日没有竞价时段逐轮：" + (snap.ts || "").slice(11, 19) + " 的补抓只能拿到 09:25 定盘冻结值"
      + "（竞价结束后数值不再变化），只是一个点，画不出竞价过程。"
      + "要看逐轮曲线，需交易日 09:14 采集任务在 09:15 前跑起来。";
  } else {
    note = "当日尚无时序轮次（live 采集 09:15 开始；手动补抓只落定盘快照）";
  }
  if (s.date && fr.date8 && s.date !== fr.date8) {
    note += " 注意：时序数据日 " + s.date + " 与当前面板 " + fr.date8 + " 不同。";
  }
  if (!liveAuc) note += " 现价为抓取时点盘中价，不是竞价口径。";
  $("curveNote").textContent = note;
  curveChart.setOption({
    textStyle: { fontFamily: CHART_FONT },
    tooltip: { trigger: "axis", backgroundColor: P().panel, borderColor: P().line2, textStyle: { color: P().text1 } },
    legend: { data: ["竞价涨幅%", "匹配量(手)"], textStyle: { fontSize: 11, color: P().text3 }, top: 0 },
    grid: { left: 48, right: 48, top: 30, bottom: 24 },
    xAxis: { type: "category", data: xs, axisLabel: { fontSize: 10, color: P().text3 }, axisLine: { lineStyle: { color: P().line } } },
    yAxis: [
      { type: "value", name: "%", scale: true, axisLabel: { fontSize: 10, color: P().text3 }, splitLine: { lineStyle: { color: P().split } } },
      { type: "value", name: "手", axisLabel: { fontSize: 10, color: P().text3 }, splitLine: { show: false } }
    ],
    series: [
      { name: "竞价涨幅%", type: "line", data: pcts, connectNulls: true,
        symbolSize: onlySnap ? 10 : 6, itemStyle: { color: P().up },
        lineStyle: onlySnap ? { type: "dashed", color: P().text4 } : { color: P().up } },
      { name: "匹配量(手)", type: "bar", yAxisIndex: 1, data: vols, itemStyle: { color: AKTHEME.alpha(P().blue, .55) } }
    ]
  }, true);
}
$("curveClose").addEventListener("click", function () {
  $("curveMask").classList.remove("show"); state.curveCode = null;
});
$("curveSend").addEventListener("click", function () {
  var cur = currentItems(state.payload || {}).find(function (x) { return x.thscode === state.curveCode; });
  goQuant([cur ? cur.ticker : state.curveCode]);
});

/* ============ 观察池编辑 ============ */
$("btnPool").addEventListener("click", function () {
  var d = state.payload || {};
  // 回填原文而不是归一化代码列表：保留用户的注释与示例行
  $("poolEdit").value = d.watchlist_text != null ? d.watchlist_text : (d.watchlist || []).join("\n");
  $("poolMax").textContent = (d.config && d.config.max_codes) || 300;
  $("poolMsg").textContent = "";
  $("poolMask").classList.add("show");
});
$("poolClose").addEventListener("click", function () { $("poolMask").classList.remove("show"); });
$("poolSave").addEventListener("click", function () {
  AK.postJson("/api/auction/watchlist", { text: $("poolEdit").value }).then(function (d) {
    var msg = "已保存 " + d.total + " 个自选（" + (d.note || "") + "）";
    if (d.invalid_total) {
      // 不静默丢代码：写错「茅台」或漏一位时会从池子里掉出去
      msg += " ⚠ 认不出 " + d.invalid_total + " 个：" + (d.invalid || []).slice(0, 6).join(" ");
    }
    $("poolMsg").textContent = msg;
    $("poolMsg").style.color = d.invalid_total ? "var(--gold)" : "";
    toast("观察池已保存：" + d.total + " 个" + (d.invalid_total ? "（" + d.invalid_total + " 个被忽略）" : ""));
    state.etag = null; poll();
  }).catch(function (e) { $("poolMsg").textContent = "保存失败: " + e; });
});
[["curveMask", "curveClose"], ["poolMask", "poolClose"]].forEach(function (pair) {
  $(pair[0]).addEventListener("click", function (ev) {
    if (ev.target === $(pair[0])) $(pair[1]).click();
  });
});

/* ============ 立即补抓 ============
   POST 只负责拉起子进程；真正的结束信号靠轻量 /api/auction/status 轮询（锁释放后自动刷新），
   避免旧版「固定 3s 后解禁按钮」在百只股池补抓未完时就能再点、且看不出何时完。 */
function watchAuctionFetch() {
  var tries = 0;
  if (state.fetchTimer) return;
  $("btnFetch").disabled = true;
  state.fetchTimer = setInterval(function () {
    tries++;
    AK.getJson("/api/auction/status").then(function (s) {
      if (s.fetching) return;
      clearInterval(state.fetchTimer); state.fetchTimer = null;
      $("btnFetch").disabled = false;
      state.etag = null; poll();
      toast("补抓结束，已刷新数据", "ok");
    }).catch(function () { /* 单次失败等下一轮 */ });
    if (tries > 120) {   // 10 分钟兜底：服务异常时不要把按钮锁死
      clearInterval(state.fetchTimer); state.fetchTimer = null;
      $("btnFetch").disabled = false;
    }
  }, 5000);
}
$("btnFetch").addEventListener("click", function () {
  var btn = this;
  btn.disabled = true;
  AK.postJson("/api/fetch-auction", {}).then(function (d) {
    if (d.status === "started") { toast("已启动后台补抓（日志 .status/logs/fetch-auction.log）"); watchAuctionFetch(); }
    else { toast(d.status === "running" ? "已有采集在运行" : JSON.stringify(d)); watchAuctionFetch(); }
  }).catch(function (e) { toast("补抓失败: " + e, "err"); setTimeout(function () { btn.disabled = false; }, 3000); });
});

/* ============ 启动 ============ */
window.addEventListener("resize", function () {
  distChart.resize(); sectorChart.resize();
  if (state.curveCode) curveChart.resize();
});
poll();
setInterval(poll, POLL_MS);
setInterval(function () { fetchSeries(false); }, SERIES_POLL_MS);
// 后台标签页轮询已暂停（poll 里 document.hidden 短路），回到前台立即补拉一次，不等下一个周期
document.addEventListener("visibilitychange", function () {
  if (!document.hidden) { poll(); fetchSeries(false); }
});
