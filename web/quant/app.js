// 量化页脚本（2026-09-27 方案 O-3c 推广：自 index.html 内联抽出；静态根直供 /quant/app.js）。零构建约定：直接改此文件。
"use strict";
/* ============================================================
   量化平台工作台（原生）
   - 全部计算走本项目 server 的 /api/quant/*，由内置 quant_sim 引擎执行
   - 与 CLI 同引擎同口径（115 项回归守护）
   ============================================================ */
var $ = function (id) { return document.getElementById(id); };
/* COL 全部即时取值（getter）：canvas 图表不认 var()，且主题切换（晨报⇄夜台，lib/theme.js）
   重绘时必须拿到新值。panel/text1/text2/text4/green2/bg1 是图表配色补充的语义位。 */
var COL = (function () {
  var map = {
    up: ["--up", "#B4372C"], down: ["--down", "#23714A"], flat: ["--flat", "#8A6D2E"],
    txt3: ["--text-3", "#8A8478"], line: ["--line", "#DDD5C6"], blue: ["--blue", "#3D5A80"],
    accent: ["--accent", "#8C6D1F"], panel: ["--bg-2", "#FDFBF6"], text1: ["--text-1", "#21201C"],
    text2: ["--text-2", "#57534A"], text4: ["--text-4", "#B0AAA0"], green2: ["--green2", "#5F9B79"],
    bg1: ["--bg-1", "#F6F3EC"]
  };
  var o = {};
  Object.keys(map).forEach(function (k) {
    Object.defineProperty(o, k, { get: function () {
      var v = getComputedStyle(document.documentElement).getPropertyValue(map[k][0]).trim();
      return v || map[k][1];
    } });
  });
  return o;
})();
/* 全站图表统一字族（canvas 不认 CSS 变量，option 里显式声明） */
var CHART_FONT = '"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif';
var META = null, BOARD = null, STRATS = [];
var CMP = [], CUR_REQ = null, lastGridR = null, JOBS = [], FOCS = "";
var CMPRES = null;   // 最近一次对比回测结果（主题切换重绘用）
var curResult = null;
var RULE = { entry: { mode: "all", conds: [defCond()] }, exit: { mode: "any", conds: [] } };
function defCond() {
  return { left: { t: "field", f: "close" }, op: "gt", right: { t: "ind", i: "sma", p: { n: 20 }, c: "" } };
}
/* esc 统一在 /lib/util.js（五页同源，含单引号转义） */
function pct(v, d) { if (v == null) return "–"; var x = (v * 100).toFixed(d == null ? 1 : d); return (v > 0 ? "+" : "") + x + "%"; }
function cls(v) { return v > 0.0001 ? "up" : (v < -0.0001 ? "down" : "dim"); }
function num(id, def) { var v = parseFloat($(id).value); return isNaN(v) ? def : v; }
function parseCodes(s) { return String(s || "").replace(/，/g, " ").split(/[\s,;、]+/).filter(Boolean); }
function errBox(msg) {
  var b = $("errBox");
  b.style.display = msg ? "block" : "none";
  b.textContent = msg || "";
  if (msg) qtoast(msg);   // errBox 在「造策略」页签里：其他页签提交的任务失败也必须看得见原因
}
/* 跨页签可见的错误浮层：已收敛 AK.toast（lib/util.js，样式见 tokens.css #akToast）；
   errBox 的伴随提示仍走这里（6 秒自动消失），跨页签也能看到失败原因 */
function qtoast(msg, type, ms) { AK.toast(msg, type, ms || 6000); }
/* JSON 请求包装已收敛 lib/util.js（AK.postJson/AK.getJson，2026-09-10）：本页保留同名薄委托，
   十余处调用点不改；语义与原实现逐字一致（超时看门狗/400+error 抛错）。 */
function postJson(url, body, timeoutMs) { return AK.postJson(url, body, timeoutMs); }
function getJson(url) { return AK.getJson(url); }

/* ---------- 顶栏与页签 ---------- */
/* 顶栏导航：公共组件渲染（五页单一来源；顺序见 appbar.js PAGES，竞价在轮动之前） */
try { AKBAR.renderNavgroup({ slot: "navgroupSlot", active: "/quant" }); } catch(e){}
/* 主题切换（晨报⇄夜台）：重绘注册制（AKTHEME.onRedraw，2026-09-10 收敛，不再手写
   akthemechange 监听清单）；重画有数据在手的图表，COL getter 会自动取到新主题色。
   每块 AK.safeRender 单独隔离：一块重绘失败不再连带其余（原为整块单 try）。 */
AKTHEME.onRedraw(function () {
  if (curResult) AK.safeRender(function () { renderEqChart(curResult); }, "renderEqChart");
  if (BOARD) AK.safeRender(function () { renderOvFiltered(BOARD); }, "renderOvFiltered");
  if (lastGridR) AK.safeRender(function () { renderHeat(); }, "renderHeat");
  if (CMPRES) AK.safeRender(function () { renderCompare(CMPRES); }, "renderCompare");
});
$("btnRefresh").onclick = function () { location.reload(); };
var charts = {};
function showTab(name) {
  var cap = name[0].toUpperCase() + name.slice(1);
  document.querySelectorAll("section.tab").forEach(function (s) { s.classList.remove("on"); });
  document.querySelectorAll("nav.tabsbar .tabbtn").forEach(function (b) { b.classList.remove("on"); });
  $("tab" + cap).classList.add("on");
  $("tabBtn" + cap).classList.add("on");
  setTimeout(function () { Object.keys(charts).forEach(function (k) { try { charts[k].resize(); } catch (e) {} }); }, 30);
  if (name === "store") loadStrategies();
  if (name === "signal") refreshSigNames();
  if (name === "ov") loadBoard();
}
document.querySelectorAll("nav.tabsbar .tabbtn").forEach(function (b) { b.onclick = function () { showTab(b.dataset.tab); }; });
/* 图表 resize 统一走 lib 防抖 AK.onResize（150ms；此前本页无防抖，拖拽窗口时连续 resize 很重） */
AK.onResize(function () { Object.keys(charts).forEach(function (k) { try { charts[k].resize(); } catch (e) {} }); });

/* ============================================================
   概览（研究流水门户）
   ============================================================ */
function loadBoard(force) {
  Promise.all([getJson("/api/quant" + (force ? "?fresh=1" : "")), getJson("/api/quant/jobs").catch(function () { return { jobs: [] }; })]).then(function (xs) {
    var d = xs[0]; JOBS = (xs[1] && xs[1].jobs) || [];
    if (d.error) { $("engineHint").textContent = d.error; return; }
    BOARD = d;
    var p = d.platform;
    $("rootState").textContent = p.exists ? "内置" : "缺失";
    $("kpiRuns").textContent = d.backtests.length;
    $("kpiReports").textContent = d.reports.length;
    $("kpiStrats").textContent = d.strategies.length;
    $("kpiResearch").textContent = d.research.length;
    renderFocusBar(d); renderOvFiltered(d);
    $("docList").innerHTML = (p.docs || []).map(function (x) { return "<span class='path-mono'>" + esc(String(x.path || "").split("/").pop()) + "</span> · " + esc(x.mtime.slice(0, 10)); }).join("<br>") || "未找到文档";
  }).catch(function (e) { $("engineHint").textContent = "总览加载失败：" + e.message; });
}
$("discChips").innerHTML = ["T+1 解锁", "涨跌停禁成交", "整手(100股)", "佣金/印花税/过户费", "价差滑点", "参与率上限", "停牌跳过", "防未来函数(昨收信号)", "成本压测防自欺"].map(function (x) { return "<span class='chip'>" + x + "</span>"; }).join("");
function renderBtTable(rows) {
  var t = $("btTable");
  if (!rows.length) { t.innerHTML = '<tbody><tr><td class="tl dim" style="padding:16px">暂无回测流水 —— 去「造策略·回测」跑第一次。</td></tr></tbody>'; return; }
  var h = "<thead><tr><th class='tl'>时间</th><th class='tl'>标题</th><th class='tl'>策略</th><th class='tl'>池</th><th>区间</th><th>累计</th><th>年化</th><th>回撤</th><th>夏普</th><th>笔数</th></tr></thead><tbody>";
  for (var i = rows.length - 1; i >= 0; i--) { var r = rows[i];
    h += "<tr><td class='tl mono dim'>" + esc((r.time || "").slice(5, 16)) + "</td><td class='tl'>" + esc(r.title)
      + (r.store ? " <span class='chip' title='已归集到存档「" + esc(r.store) + "」'>📌</span>" : "") + "</td><td class='tl dim'>" + esc(r.strategy) + "</td>"
      + "<td class='tl dim mono' title='" + esc(r.symbols.join(" ")) + "'>" + r.symbols.length + " 只</td><td class='mono dim'>" + esc(r.window) + "</td>"
      + "<td class='mono " + cls(r.cum_ret) + "'>" + pct(r.cum_ret) + "</td><td class='mono " + cls(r.cagr) + "'>" + pct(r.cagr) + "</td>"
      + "<td class='mono dim'>" + (r.max_dd == null ? "–" : (r.max_dd * 100).toFixed(1) + "%") + "</td><td class='mono'>" + (r.sharpe == null ? "–" : r.sharpe.toFixed(2)) + "</td>"
      + "<td class='mono dim'>" + (r.trades == null ? "–" : r.trades) + "</td></tr>"; }
  t.innerHTML = h + "</tbody>";
  var last = rows[rows.length - 1];
  $("logNote").textContent = (FOCS ? "筛选 " + rows.length + " 条 · " : "最近：") + (last.time || "").slice(0, 16) + " · " + last.title;
}
/* ---------- 概览·聚焦分析（选存档策略/类型 → 流水/散点/报告联动筛选） ---------- */
function focusRows(rows) {
  if (!FOCS) return rows;
  var i = FOCS.indexOf(":"), t = FOCS.slice(0, i), v = FOCS.slice(i + 1);
  if (t === "store") return rows.filter(function (r) { return r.store === v || (r.title || "").indexOf(v) >= 0; });
  return rows.filter(function (r) { return r.strategy === v; });
}
function renderOvFiltered(d) {
  var rows = focusRows(d.backtests);
  renderBtTable(rows); renderScatter(rows);
  var reps = d.reports || [];
  if (FOCS.slice(0, 6) === "store:") { var nm = FOCS.slice(6); reps = reps.filter(function (r) { return r.name === nm; }); }
  renderRepTable(reps); renderSide(d);
}
function renderFocusBar(d) {
  var sel = $("ovFocusSel"), prev = sel.value || FOCS;
  var names = (d.strategies || []).map(function (x) { return x.name; });
  var fams = []; (d.backtests || []).forEach(function (r) { if (r.strategy && names.indexOf(r.strategy) < 0 && fams.indexOf(r.strategy) < 0) fams.push(r.strategy); });
  var h = "<option value=''>全部跑次（总览）</option>";
  names.forEach(function (n) { h += "<option value='store:" + esc(n) + "'>📌 " + esc(n) + "（存档策略）</option>"; });
  fams.forEach(function (f) { h += "<option value='fam:" + esc(f) + "'>⚙ 类型 · " + esc(f) + "</option>"; });
  sel.innerHTML = h;
  sel.value = prev && (prev === "" || h.indexOf("value='" + prev + "'") >= 0) ? prev : "";
  FOCS = sel.value;
  var chip = function (k, v, c) { return "<span class='chip'>" + k + " <b class='" + (c || "") + "'>" + v + "</b></span>"; };
  var rows = focusRows(d.backtests), st;
  if (!FOCS) st = chip("流水", d.backtests.length) + chip("报告", (d.reports || []).length) + chip("存档", names.length)
    + "<span class='hint'>选一个存档策略或类型 → 下方流水/散点/报告只看它</span>";
  else if (!rows.length) st = "<span class='hint'>该对象暂无关联跑次 —— 跑回测时填「存档名」即可自动归集到这里</span>";
  else {
    var cums = rows.map(function (r) { return r.cum_ret; }).filter(function (x) { return x != null; }).sort(function (a, b) { return a - b; });
    var shs = rows.map(function (r) { return r.sharpe; }).filter(function (x) { return x != null; });
    var best = cums[cums.length - 1], worst = cums[0], med = cums[Math.floor((cums.length - 1) / 2)];
    st = chip("跑次", rows.length) + (cums.length ? chip("最好", pct(best), "up") + chip("中位", pct(med), cls(med)) + chip("最差", pct(worst), "down")
      + chip("正收益", cums.filter(function (x) { return x > 0; }).length + "/" + cums.length) : "")
      + chip("平均夏普", shs.length ? (+ (shs.reduce(function (a, b) { return a + b; }, 0) / shs.length).toFixed(2)) : "–")
      + "<span class='hint'>最近 " + esc((rows[rows.length - 1].time || "").slice(5, 16)) + "</span>";
  }
  $("ovFocusStats").innerHTML = st;
  var name = FOCS.slice(0, 6) === "store:" ? FOCS.slice(6) : null;
  var rep = name ? (d.reports || []).filter(function (r) { return r.name === name; })[0] : null;
  $("btnFocusRun").style.display = name ? "" : "none";
  $("btnFocusSig").style.display = name ? "" : "none";
  $("btnFocusRep").style.display = rep ? "" : "none";
  $("btnFocusRun").onclick = function () {
    postJson("/api/quant/strategies/get", { name: name }).then(function (x) {
      fillBuilderFrom({ name: x.name, kind: x.kind, payload: x.payload, data_symbols: x.data_symbols });
      $("inSaveName").value = name;
    }).catch(function (e) { errBox("载入失败：" + e.message); });
  };
  $("btnFocusSig").onclick = function () { showTab("signal"); refreshSigNames(function () { $("sigName").value = name; }); };
  if (rep) $("btnFocusRep").onclick = function () { window.open("/quant-results/" + encodeURIComponent(rep.html || (name + "_report.html"))); };
}
$("ovFocusSel").onchange = function () { FOCS = this.value; if (BOARD) { renderFocusBar(BOARD); renderOvFiltered(BOARD); } };
function renderScatter(rows) {
  if (!charts.sc) charts.sc = echarts.init($("scChart"));
  var pts = rows.filter(function (r) { return r.cum_ret != null && r.sharpe != null; })
    .map(function (r) { return { value: [+(r.cum_ret * 100).toFixed(2), +r.sharpe.toFixed(3), r.trades || 1], name: r.title, time: r.time, strategy: r.strategy }; });
  var maxAbs = Math.max.apply(null, [1].concat(pts.map(function (p) { return Math.abs(p.value[0]); })));
  charts.sc.setOption({
    backgroundColor: "transparent", textStyle: { fontFamily: CHART_FONT },
    grid: { left: 44, right: 18, top: 26, bottom: 30 },
    tooltip: { trigger: "item", confine: true, backgroundColor: COL.panel, borderColor: COL.line, textStyle: { color: COL.text1 },
      formatter: function (a) { var d = a.data; return "<b>" + esc(d.name) + "</b><br>" + esc(d.strategy || "") + "<br>累计 " + d.value[0] + "% ｜ 夏普 " + d.value[1] + " ｜ " + d.value[2] + " 笔<br><span style='color:" + COL.txt3 + "'>" + esc(d.time) + "</span>"; } },
    xAxis: { type: "value", name: "累计 %", nameTextStyle: { color: COL.txt3 }, min: -maxAbs * 1.2, max: maxAbs * 1.2, axisLabel: { color: COL.txt3 }, splitLine: { lineStyle: { color: COL.line } } },
    yAxis: { type: "value", name: "夏普", nameTextStyle: { color: COL.txt3 }, axisLabel: { color: COL.txt3 }, splitLine: { lineStyle: { color: COL.line } } },
    series: [{ type: "scatter", data: pts, symbolSize: function (v) { return Math.min(30, 7 + Math.sqrt(v[2]) * 3); },
      itemStyle: { color: function (p) { return p.value[0] >= 0 ? AKTHEME.alpha(COL.up, .75) : AKTHEME.alpha(COL.down, .75); }, borderColor: COL.panel, borderWidth: 1 },
      markLine: { silent: true, symbol: "none", lineStyle: { color: COL.flat, type: "dashed" }, label: { show: false }, data: [{ xAxis: 0 }, { yAxis: 0 }] } }],
    graphic: pts.length ? [] : [{ type: "text", left: "center", top: "middle", style: { text: "暂无可绘散点", fill: COL.txt3, fontSize: 12 } }]
  });
}
function renderRepTable(rows) {
  var t = $("repTable");
  if (!rows.length) { t.innerHTML = '<tbody><tr><td class="tl dim" style="padding:16px">暂无研究报告。</td></tr></tbody>'; $("repNote").textContent = ""; return; }
  var h = "<thead><tr><th class='tl'>报告</th><th class='tl'>基准</th><th>区间</th><th>累计</th><th>回撤</th><th>夏普</th><th>笔数</th><th class='tl'>HTML</th></tr></thead><tbody>";
  rows.forEach(function (r) {
    h += "<tr><td class='tl mono'>" + esc(r.name) + "</td><td class='tl dim mono'>" + esc(r.benchmark || "–") + "</td><td class='mono dim'>" + esc(r.window) + "</td>"
      + "<td class='mono " + cls(r.cum_ret) + "'>" + pct(r.cum_ret) + "</td><td class='mono dim'>" + (r.max_dd == null ? "–" : (r.max_dd * 100).toFixed(1) + "%") + "</td>"
      + "<td class='mono'>" + (r.sharpe == null ? "–" : r.sharpe.toFixed(2)) + "</td><td class='mono dim'>" + (r.trades == null ? "–" : r.trades) + "</td>"
      + "<td class='tl'>" + (r.html ? "<a href='/quant-results/" + encodeURIComponent(r.html) + "' target='_blank' style='color:var(--accent,#8C6D1F)'>打开↗</a>" : "<span class='dim'>–</span>") + "</td></tr>"; });
  t.innerHTML = h + "</tbody>";
  $("repNote").textContent = "共 " + rows.length + " 份";
}
function renderSide(d) {
  var html = "";
  html += "<h4>策略库</h4>";
  html += d.strategies.length ? d.strategies.map(function (s) { return "<div class='sitem'><span class='nm'>" + esc(s.name) + "</span><span class='tag " + esc(s.kind) + "'>" + esc(s.kind) + "</span><span class='mt'>" + esc((s.updated_at || "").slice(5, 10)) + "</span></div>"; }).join("") : "<div class='empty'>还没有存档 —— 去「造策略·回测」跑完点「存策略库」。</div>";
  html += "<h4>今日信号归档</h4>";
  html += d.signals.length ? d.signals.slice(0, 8).map(function (s) { return "<div class='sitem'><span class='nm'>" + esc(s.name) + "</span><span class='mt'>" + esc((s.mtime || "").slice(5, 10)) + "</span></div>"; }).join("") : "<div class='empty'>暂无 —— 去「今日信号」跑一次。</div>";
  html += "<h4>参数研究</h4>";
  html += d.research.length ? d.research.map(function (s) { return "<div class='sitem'><span class='nm mono'>" + esc(s.name) + "</span><span class='rv'>" + (s.rows == null ? "" : s.rows + " 行") + "</span></div>"; }).join("") : "<div class='empty'>暂无网格产出。</div>";
  html += "<h4>任务历史（点击查看结果）</h4>";
  var JK = { screener: "选股", signals: "信号", grid: "网格", wf: "前瞻", refresh: "数据" };
  html += JOBS.length ? JOBS.slice(0, 8).map(function (j) {
    var dot = j.status === "done" ? "ok" : (j.status === "running" ? "warn" : (j.status === "error" ? "bad" : "dim"));
    return "<div class='sitem jobitem' data-jid='" + esc(j.id) + "'><span class='nm'>" + (JK[j.kind] || esc(j.kind)) + " <span class='dim'>" + esc(j.brief || "") + "</span></span><span class='mt'><span class='dot " + dot + "' style='display:inline-block;width:7px;height:7px;border-radius:50%;background:" + (dot === "ok" ? COL.down : dot === "warn" ? COL.accent : dot === "bad" ? COL.up : COL.txt3) + "'></span> " + esc((j.started || "").slice(5, 16)) + "</span></div>";
  }).join("") : "<div class='empty'>暂无后台任务。</div>";
  $("sideList").innerHTML = html;
  $("sideList").querySelectorAll(".jobitem").forEach(function (el) { el.onclick = function () { viewJob(el.dataset.jid); }; });
}
function viewJob(id) {
  getJson("/api/quant/job/" + id).then(function (d) {
    var r = d.result;
    if (d.status !== "done" || !r) { errBox("任务 " + id + " 状态：" + d.status + (d.error ? " · " + d.error : "（可能还在跑，稍后再看）")); showTab("build"); return; }
    if (d.kind === "screener") { showTab("screen"); renderScreenResult(r); $("screenStatus").textContent = "历史任务 " + id; }
    else if (d.kind === "signals") { showTab("signal"); renderSigResult(r); $("sigStatus").textContent = "历史任务 " + id; }
    else if (d.kind === "grid") { showTab("grid"); renderGridResult(r); $("gridStatus").textContent = "历史任务 " + id; }
    else if (d.kind === "wf") { showTab("grid"); renderWfResult(r); $("wfStatus").textContent = "历史任务 " + id; }
    else if (d.kind === "refresh") { showTab("ov"); getJson("/api/quant/meta?fresh=1").then(function (m) { if (m.data_freshness) { META = m; renderFresh(m.data_freshness); } }); }
  }).catch(function (e) { errBox("任务详情获取失败：" + e.message); });   // 无 catch 时网络错误 = 点了没反应
}
function renderFresh(f) {
  var el = $("freshState"), n = $("freshN");
  if (!el) return;
  if (!f || !f.latest) { el.textContent = "无缓存"; n.textContent = ""; return; }
  el.textContent = f.latest;
  var lag = Math.round((Date.now() - new Date(f.latest + "T15:00:00").getTime()) / 864e5);
  n.textContent = f.symbols + " 只 · 落后≈" + lag + "天";
  el.style.color = lag > 4 ? "var(--flat,#8C6D1F)" : "";
  el.title = (f.oldest && f.oldest.length) ? "更旧的序列：" + f.oldest.map(function (o) { return o.symbol + "@" + o.last; }).join("  ") : "全部序列齐平";
}
$("btnRefreshData").onclick = function () {
  var btn = this;
  var baseHint = $("engineHint").textContent;   // engineHint 被借作任务状态条，完成后恢复常驻的缓存规模提示
  submitJob("refresh", {}, btn, $("engineHint"), function (r) {
    $("engineHint").textContent = "缓存刷新完成：" + r.written + "/" + r.symbols_in + " 只 → " + (r.after || "?");
    setTimeout(function () {
      $("engineHint").textContent = "引擎内置 · 本地缓存 " + (META ? META.local_symbols.length : "?") + " 只标的";
    }, 8000);
    getJson("/api/quant/meta?fresh=1").then(function (m) { if (m.data_freshness) renderFresh(m.data_freshness); });
  });
};
$("btnReload").onclick = function () { loadBoard(true); };

/* ============================================================
   造策略 · 回测
   ============================================================ */
var KIND = "builtin";
$("kindSeg").querySelectorAll("button").forEach(function (b) {
  b.onclick = function () {
    KIND = b.dataset.k;
    $("kindSeg").querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
    $("famWrap").style.display = KIND === "builtin" ? "" : "none";
    $("ruleWrap").style.display = KIND === "rule" ? "" : "none";
    if (KIND === "rule") { renderRuleRows(); scheduleRulePreview(); }
  };
});
function renderFamWrap(family, params) {
  var f = META.families.filter(function (x) { return x.key === family; })[0] || META.families[0];
  var h = "<div class=\"frow\"><label>策略族</label><select id=\"inFam\">";
  META.families.forEach(function (x) { h += "<option value='" + x.key + "'" + (x.key === f.key ? " selected" : "") + ">" + esc(x.label) + "</option>"; });
  h += "</select></div><div id=\"famParams\">";
  f.params.forEach(function (p) {
    var v = (params && params[p.k] != null) ? params[p.k] : p.default;
    if (p.type === "bool") h += "<div class='frow'><label><input type='checkbox' data-pk='" + p.k + "'" + (v ? " checked" : "") + "> " + esc(p.label) + "</label></div>";
    else h += "<div class='frow'><label>" + esc(p.label) + "</label><input type='number' data-pk='" + p.k + "' value='" + v + "' min='" + (p.min != null ? p.min : 0) + "' max='" + (p.max != null ? p.max : 1e9) + "' step='" + (p.step || 1) + "'></div>";
  });
  h += "</div>";
  $("famWrap").innerHTML = h;
  $("inFam").onchange = function () { renderFamWrap(this.value); };
}
function collectFamParams() {
  var out = {};
  $("famWrap").querySelectorAll("[data-pk]").forEach(function (el) {
    if (el.type === "checkbox") { out[el.dataset.pk] = el.checked; return; }
    var v = parseFloat(el.value);
    out[el.dataset.pk] = isNaN(v) ? 0 : v;
  });
  return out;
}
$("inCodes").addEventListener("input", function () {
  var n = parseCodes(this.value).length;
  $("codesN").textContent = n ? n + " 只" : "";
});

/* ---------- 规则搭建 ---------- */
function operandHtml(tag, o) {
  o = o || { t: "field", f: "close" };
  var h = "<select class='ot' data-tag='" + tag + "'>";
  [["field", "字段"], ["ind", "指标"], ["const", "常数"]].forEach(function (x) { h += "<option value='" + x[0] + "'" + (o.t === x[0] ? " selected" : "") + ">" + x[1] + "</option>"; });
  h += "</select> ";
  if (o.t === "field") {
    h += "<select class='of'>";
    META.rule.fields.forEach(function (f) { h += "<option value='" + f + "'" + (o.f === f ? " selected" : "") + ">" + f + "</option>"; });
    h += "</select>";
  } else if (o.t === "const") {
    h += "<input class='ov' type='number' step='any' value='" + (o.v != null ? o.v : 0) + "' style='width:80px'>";
  } else {
    var meta = META.rule.indicators[o.i] || META.rule.indicators.sma;
    h += "<select class='oi'>";
    Object.keys(META.rule.indicators).forEach(function (k) { h += "<option value='" + k + "'" + (o.i === k ? " selected" : "") + ">" + esc(META.rule.indicators[k].label) + "</option>"; });
    h += "</select>";
    var comps = meta.comps;
    if (comps.length > 1 || (comps.length === 1 && comps[0] !== "")) {
      h += "<select class='oc'>";
      comps.forEach(function (c) { h += "<option value='" + c + "'" + ((o.c || "") === c ? " selected" : "") + ">" + (c || "值") + "</option>"; });
      h += "</select>";
    }
    Object.keys(meta.params).forEach(function (pk) {
      var v = (o.p && o.p[pk] != null) ? o.p[pk] : meta.params[pk];
      h += "<input class='opn-" + pk + "' type='number' step='any' value='" + v + "' style='width:56px' title='" + pk + "'>";
    });
  }
  return h;
}
function condHtml(ruleKey, idx, c) {
  var h = "<div class='cond' data-rule='" + ruleKey + "' data-idx='" + idx + "'><div class='crow'>"
    + operandHtml("left", c.left) + "<select class='op'>";
  Object.keys(META.rule.ops).forEach(function (op) { h += "<option value='" + op + "'" + (c.op === op ? " selected" : "") + ">" + op + "(" + META.rule.ops[op] + ")</option>"; });
  h += "</select>" + operandHtml("right", c.right) + "<button class='del' title='删除条件'>×</button></div></div>";
  return h;
}
function readCond(el) {
  // crow 子元素序列：[ot, left控件..., op, ot, right控件..., del]；按位置切分两侧操作数
  function readOperands(crow) {
    var kids = Array.prototype.slice.call(crow.children);
    var ots = [], opEl = null;
    kids.forEach(function (x) {
      if (x.classList.contains("ot")) ots.push(x);
      if (x.classList.contains("op")) opEl = x;
    });
    var i0 = kids.indexOf(ots[0]), iOp = kids.indexOf(opEl), i1 = kids.indexOf(ots[1]);
    return [pickOperand(kids.slice(i0 + 1, iOp)), pickOperand(kids.slice(i1 + 1))];
  }
  function pickOperand(controls) {
    var out = null;
    controls.every(function (x) {
      if (x.classList.contains("of")) { out = { t: "field", f: x.value }; return false; }
      if (x.classList.contains("ov")) { out = { t: "const", v: parseFloat(x.value) || 0 }; return false; }
      if (x.classList.contains("oi")) {
        var o = { t: "ind", i: x.value, p: {}, c: "" };
        controls.forEach(function (y) {
          if (y.classList.contains("oc")) o.c = y.value;
          if (y.className.indexOf("opn-") === 0) {
            var nv = parseFloat(y.value);
            o.p[y.className.replace("opn-", "")] = isNaN(nv) ? 0 : nv;   // 输入框清空时 NaN 会 stringify 成 null 送后端
          }
        });
        out = o; return false;
      }
      return true;
    });
    return out || { t: "field", f: "close" };
  }
  var crow = el.querySelector(".crow");
  var sides = readOperands(crow);
  return { left: sides[0], op: crow.querySelector("select.op").value, right: sides[1] };
}
function syncRuleFromDom() {
  if (!META) return;
  [["entry", "entryRows", "entryMode"], ["exit", "exitRows", "exitMode"]].forEach(function (m) {
    var box = $(m[1]);
    RULE[m[0]].mode = $(m[2]).value;
    RULE[m[0]].conds = Array.prototype.map.call(box.querySelectorAll(".cond"), function (el) { return readCond(el); });
  });
}
function renderRuleRows() {
  if (!META) return;
  [["entry", "entryRows"], ["exit", "exitRows"]].forEach(function (m) {
    var rk = m[0], box = $(m[1]);
    box.innerHTML = RULE[rk].conds.map(function (c, i) { return condHtml(rk, i, c); }).join("") || "<div class='hint'>无附加条件" + (rk === "exit" ? "（仅止损止盈出场）" : "（永不入场，请加条件）") + "</div>";
    box.querySelectorAll(".cond").forEach(function (el) { bindCond(el); });
    box.querySelectorAll("select.ot").forEach(function (s) { s.onchange = function () { syncRuleFromDom(); renderRuleRows(); scheduleRulePreview(); }; });
    box.querySelectorAll("select.oi").forEach(function (s) { s.onchange = function () { syncRuleFromDom(); renderRuleRows(); scheduleRulePreview(); }; });
  });
  $("entryMode").value = RULE.entry.mode;
  $("exitMode").value = RULE.exit.mode;
}
function bindCond(el) {
  el.querySelectorAll("select").forEach(function (s) {
    s.onchange = function () {
      if (s.classList.contains("ot") || s.classList.contains("oi")) return;   // 这两个触发重建
      syncRuleFromDom(); scheduleRulePreview();
    };
  });
  el.querySelectorAll("input").forEach(function (i) { i.oninput = function () { syncRuleFromDom(); scheduleRulePreview(); }; });
  el.querySelector(".del").onclick = function () {
    syncRuleFromDom();
    var rk = el.dataset.rule;
    RULE[rk].conds.splice(+el.dataset.idx, 1);
    renderRuleRows(); scheduleRulePreview();
  };
}
document.querySelectorAll(".addrow").forEach(function (b) {
  b.onclick = function () {
    syncRuleFromDom();
    var rk = b.dataset.rule;
    if (RULE[rk].conds.length >= 5) return;
    RULE[rk].conds.push(defCond());
    renderRuleRows(); scheduleRulePreview();
  };
});
["entryMode", "exitMode"].forEach(function (id) { $(id).onchange = function () { syncRuleFromDom(); scheduleRulePreview(); }; });
function buildSpec() {
  syncRuleFromDom();
  return {
    symbols: parseCodes($("inCodes").value), entry: RULE.entry, exit: RULE.exit,
    target_weight: num("rTarget", 0.95), stop_loss_pct: num("rStop", 0) / 100,
    take_profit_pct: num("rTake", 0) / 100, trailing_pct: num("rTrail", 0) / 100,
  };
}
var prevTimer = null;
function scheduleRulePreview() {
  if (KIND !== "rule" || !META) return;
  clearTimeout(prevTimer);
  prevTimer = setTimeout(function () {
    $("advRule").style.display = "";
    $("advPanel").style.display = "";
    postJson("/api/quant/rule/preview", { spec: buildSpec() }).then(function (r) {
      $("ruleCode").textContent = r.code || "";
      $("ruleProblems").innerHTML = (r.problems && r.problems.length)
        ? "<span class='hint err'>" + r.problems.map(esc).join("<br>") + "</span>"
        : "<span class='hint'>spec 校验通过 ✓（代码由引擎同源生成，非手拼）</span>";
    }).catch(function (e) { $("ruleProblems").textContent = "预览失败：" + e.message; });
  }, 500);
}

/* ---------- 回测执行 ---------- */
function buildReq() {
  var codes = parseCodes($("inCodes").value);
  var req = {
    codes: codes, start: $("inStart").value || null, end: $("inEnd").value || null,
    benchmark: $("inBench").value || null, cash: num("inCash", 1e6),
    execution: $("inExec").value, adjust: "qfq", data_mode: $("inMode").value,
    commission_wp: num("inComm", 2.5), min_comm: num("inMinC", 5), stamp_wp: num("inStamp", 5),
    slip_model: $("inSlipM").value, slip_value: num("inSlipV", 5e-4), participation: num("inPart", 0.05),
    max_dd_halt: num("inHalt", 0) / 100, block_limit: $("inBlock").checked, liquidate_on_end: $("inLiq").checked,
    warmup_bars: num("inWarm", 0), title: $("inTitle").value.trim(),
    save: $("chkSaveRpt") ? $("chkSaveRpt").checked : false, save_name: $("inSaveName").value.trim(),
  };
  req.strategy = KIND === "builtin"
    ? { kind: "builtin", family: $("inFam") ? $("inFam").value : "dual_ma", params: collectFamParams() }
    : { kind: "rule", spec: buildSpec() };
  return req;
}
$("btnRun").onclick = function () {
  errBox("");
  var req = buildReq();
  if (!req.codes.length) return errBox("标的池为空：先填代码（如 510300 510500）");
  var btn = this; btn.disabled = true; btn.textContent = "回测中…";
  var t0 = performance.now();
  // 10 分钟看门狗：本地缓存通常秒级，auto 首次远端取数可能数分钟；hang 死不再永久锁按钮
  postJson("/api/quant/backtest", req, 600000).then(function (d) {
    btn.disabled = false; btn.textContent = "▶ 运行回测";
    if (d.error) return errBox(d.error);
    curResult = d; CUR_REQ = req;
    $("eqNote").textContent = "耗时 " + ((performance.now() - t0) / 1000).toFixed(1) + "s · "
      + (req.strategy.kind === "builtin" ? req.strategy.family : "规则策略") + "（已入研究流水）";
    renderResult(d);
  }).catch(function (e) { btn.disabled = false; btn.textContent = "▶ 运行回测"; errBox("回测失败：" + e.message); });
};
var KPI_ORDER = ["年化超额收益", "信息比率", "累计收益率", "年化收益率", "最大回撤", "夏普比率", "卡玛比率", "胜率", "交易次数", "总手续费", "滑点成本"];
function fmtMetric(k, v) {
  if (typeof v !== "number") return v;
  if (k === "交易次数") return v;
  if (k === "总手续费" || k === "滑点成本") return Math.round(v).toLocaleString();
  if (/收益|回撤|胜率/.test(k)) return (v * 100).toFixed(1) + "%";
  return (+v.toFixed(3));
}
function renderResult(d) {
  var m = d.metrics;
  $("eqEmpty").style.display = "none";     // 首次出报告后撤掉空态引导
  $("tradesEmpty").style.display = "none";
  $("kpiRow").innerHTML = KPI_ORDER.filter(function (k) { return m[k] != null; }).map(function (k) {
    var c = /收益/.test(k) ? cls(m[k]) : "";
    return "<span class='chip'>" + k + " <b class='" + c + "'>" + fmtMetric(k, m[k]) + "</b></span>";
  }).join("");
  renderEqChart(d);
  renderTrades(d);
  $("advPanel").style.display = "";
  $("warnList").innerHTML = (d.warnings && d.warnings.length) ? d.warnings.map(esc).join("<br>") : "无";
  var evs = (d.risk_events || []).filter(function (e) { return e && typeof e === "object"; });
  $("logBox").textContent = (evs.length ? "风控事件：\n" + evs.map(function (e) {
    return [e.date, e.kind, e.symbol || "", e.message, e.action].filter(Boolean).join(" · ");
  }).join("\n") + "\n\n" : "") + (d.logs || []).join("\n");
  if (evs.length) $("advLogs").setAttribute("open", "");
  var lr = $("lnkReport");
  if (d.report_url) { lr.style.display = ""; lr.href = d.report_url; } else { lr.style.display = "none"; }
  $("btnAddCmp").style.display = CUR_REQ ? "" : "none";
  renderTrades(d, d.force_liquidated);   // 强平标记随数据传入，不再靠 renderTrades 从 DOM 文本回读
}
function renderEqChart(d) {
  if (!charts.eq) charts.eq = echarts.init($("eqChart"));
  var c = d.curve, showW = $("chkW").checked;
  var base = (c.equity || [])[0];
  if (base == null) { charts.eq.clear(); return; }   // 无净值点：清空画布而不是画出一屏 NaN
  // 基准首日可能停牌为 null：用首个有效值归一（全部缺失则不画基准线，不静默 NaN）
  var b0 = c.benchmark ? (c.benchmark.filter(function (x) { return x != null; })[0] || null) : null;
  var bench = (c.benchmark && b0) ? c.benchmark.map(function (x) { return x == null ? null : +(x / b0 * 100).toFixed(2); }) : null;
  var series = [
    { name: "策略净值", type: "line", data: c.equity.map(function (x) { return +(x / base * 100).toFixed(2); }), lineStyle: { color: COL.up, width: 1.6 }, itemStyle: { color: COL.up }, showSymbol: false, xAxisIndex: 0, yAxisIndex: 0, z: 5 },
  ];
  if (bench) series.push({ name: "基准", type: "line", data: bench, lineStyle: { color: COL.accent, width: 1.2, type: "dashed" }, itemStyle: { color: COL.accent }, showSymbol: false, xAxisIndex: 0, yAxisIndex: 0 });
  var evPts = [];
  (d.risk_events || []).forEach(function (e) {
    if (!e || typeof e !== "object" || !e.date) return;
    var di = c.dates.indexOf(String(e.date).slice(0, 10));
    if (di >= 0 && evPts.length < 60) evPts.push({ coord: [di, +(c.equity[di] / base * 100).toFixed(2)], _e: e });
  });
  if (evPts.length) series[0].markPoint = {
    symbol: "pin", symbolSize: 26, data: evPts,
    itemStyle: { color: COL.accent, borderColor: COL.panel, borderWidth: 1 },
    label: { show: false }, silent: false,
    tooltip: { trigger: "item", confine: true, backgroundColor: COL.panel, borderColor: COL.line, textStyle: { color: COL.text1 },
      formatter: function (o) { var e = o.data._e; return "<b>" + esc(e.kind) + "</b> " + esc(e.date) + (e.symbol ? "<br>" + esc(e.symbol) : "") + "<br>" + esc(e.message || "") + (e.action ? "<br><span style='color:" + COL.txt3 + "'>action: " + esc(e.action) + "</span>" : ""); } },
  };
  series.push({ name: "回撤", type: "line", data: c.drawdown.map(function (x) { return +(x * 100).toFixed(2); }), lineStyle: { color: COL.blue, width: 1 }, itemStyle: { color: COL.blue }, areaStyle: { color: AKTHEME.alpha(COL.blue, .12) }, showSymbol: false, xAxisIndex: 1, yAxisIndex: 1, z: 2 });
  if (showW && c.weights) {
    Object.keys(c.weights).forEach(function (s, i) {
      series.push({ name: "仓位·" + s, type: "line", data: c.weights[s], lineStyle: { width: 0 }, stack: "w", showSymbol: false, xAxisIndex: 0, yAxisIndex: 2, silent: true });
    });
  }
  charts.eq.setOption({
    backgroundColor: "transparent",
    textStyle: { fontFamily: CHART_FONT },
    color: [COL.up, COL.accent, COL.blue, AKTHEME.alpha(COL.accent, .25), AKTHEME.alpha(COL.blue, .25), AKTHEME.alpha(COL.green2, .25), AKTHEME.alpha(COL.red2, .25), AKTHEME.alpha(COL.up, .2)],
    // 默认 tooltip formatter 不转义 HTML，series 名含用户自由输入（仓位·代码 / 标题），必须自定义转义
    tooltip: { trigger: "axis", confine: true, backgroundColor: COL.panel, borderColor: COL.line, textStyle: { color: COL.text1 },
      axisPointer: { type: "cross", label: { backgroundColor: COL.text2 } },
      formatter: function (ps) {
        if (!ps || !ps.length) return "";
        var html = "<b>" + esc(String(ps[0].axisValue == null ? "" : ps[0].axisValue)) + "</b>";
        ps.forEach(function (p) {
          if (p.seriesName === "仓位·隐线") return;
          var v = Array.isArray(p.value) ? p.value[p.value.length - 1] : p.value;
          html += "<br/>" + (p.marker || "") + esc(p.seriesName == null ? "" : p.seriesName)
            + " <b>" + (v == null ? "–" : (+v).toLocaleString("zh-CN", { maximumFractionDigits: 2 })) + "</b>";
        });
        return html;
      } },
    legend: { top: 4, textStyle: { color: COL.txt3, fontSize: 11 }, itemWidth: 14 },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [{ left: 56, right: 18, top: 32, height: "42%" }, { left: 56, right: 18, top: "62%", height: "20%" }],
    xAxis: [
      { type: "category", data: c.dates, gridIndex: 0, axisLabel: { color: COL.txt3 }, axisLine: { lineStyle: { color: COL.line } } },
      { type: "category", data: c.dates, gridIndex: 1, axisLabel: { show: false }, axisLine: { lineStyle: { color: COL.line } } },
    ],
    yAxis: [
      { type: "value", scale: true, gridIndex: 0, name: "净值(=100)", nameTextStyle: { color: COL.txt3 }, axisLabel: { color: COL.txt3 }, splitLine: { lineStyle: { color: COL.line } } },
      { type: "value", gridIndex: 1, name: "回撤%", nameTextStyle: { color: COL.txt3 }, axisLabel: { color: COL.txt3 }, splitLine: { lineStyle: { color: COL.line } } },
      { type: "value", gridIndex: 0, show: false, min: 0, max: 1 },
    ],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }, { type: "slider", xAxisIndex: [0, 1], height: 14, bottom: 4 }],
    series: series,
  }, { replaceMerge: ["series"] });
}
$("chkW").onchange = function () { if (curResult) renderEqChart(curResult); };
function renderTrades(d, forceLiq) {
  var t = $("tradesTable"), rows = d.trades || [];
  $("tradesNote").textContent = ((d.metrics["交易次数"] || 0) + " 笔（成交 " + d.fills_n + " 次） · " + (forceLiq ? "⚠️ 期末强制平仓" : ""));
  if (!rows.length) { t.innerHTML = '<tbody><tr><td class="tl dim" style="padding:12px">无成交。</td></tr></tbody>'; return; }
  var h = "<thead><tr><th class='tl'>标的</th><th class='tl'>买入</th><th class='tl'>卖出</th><th>买价</th><th>卖价</th><th>数量</th><th>净盈亏</th><th>收益率</th><th>持有天</th></tr></thead><tbody>";
  rows.forEach(function (r) {
    h += "<tr><td class='tl mono'>" + esc(r.symbol) + "</td><td class='tl dim mono'>" + esc(String(r.entry_date || "").slice(0, 10)) + "</td><td class='tl dim mono'>" + esc(String(r.exit_date || "").slice(0, 10)) + "</td>"
      + "<td class='mono'>" + esc(r.entry_price) + "</td><td class='mono'>" + esc(r.exit_price) + "</td><td class='mono dim'>" + ((r.quantity || 0).toLocaleString ? (r.quantity || 0).toLocaleString() : r.quantity) + "</td>"
      + "<td class='mono " + cls(r.net_pnl) + "'>" + Math.round(r.net_pnl).toLocaleString() + "</td><td class='mono " + cls(r.return_pct) + "'>" + pct(r.return_pct) + "</td><td class='mono dim'>" + (r.holding_days == null ? "–" : r.holding_days) + "</td></tr>";
  });
  t.innerHTML = h + "</tbody>";
}

/* ---------- 多策略对比 ---------- */
function cmpLabel(req) {
  var st = req.strategy || {};
  var core = st.kind === "builtin" ? (st.family || "内置") : (st.kind === "rule" ? "规则" : "代码");
  if (st.kind === "builtin" && st.params) {
    var ps = Object.keys(st.params).slice(0, 2).map(function (k) { return k + "=" + st.params[k]; }).join(" ");
    if (ps) core += " " + ps;
  }
  return String(req.title || core || "run").slice(0, 22);
}
function renderCmpSlots() {
  var box = $("cmpSlots");
  if (!CMP.length) { box.innerHTML = "<span class='hint'>跑一次回测后点报告卡头的「＋加入对比」攒槽位（2~4 条）。</span>"; return; }
  box.innerHTML = CMP.map(function (c, i) {
    return "<span class='chip'>" + (i + 1) + ". " + esc(c.label) + " <b data-i='" + i + "' style='cursor:pointer' title='移除'>×</b></span>";
  }).join("");
  box.querySelectorAll("b[data-i]").forEach(function (b) {
    b.onclick = function () { CMP.splice(+b.dataset.i, 1); renderCmpSlots(); if (CMP.length < 2) { $("cmpChart").style.display = "none"; $("cmpTable").innerHTML = ""; } };
  });
}
$("btnAddCmp").onclick = function () {
  if (!CUR_REQ || !curResult) return errBox("先跑一次回测再加入对比");
  if (CMP.length >= 4) return errBox("对比槽位最多 4 条：先移除一条");
  CMP.push({ label: cmpLabel(CUR_REQ) + " @" + new Date().toTimeString().slice(0, 5), req: CUR_REQ });
  $("cmpPanel").style.display = ""; renderCmpSlots(); errBox("");
};
$("btnCmpClear").onclick = function () { CMP = []; renderCmpSlots(); $("cmpChart").style.display = "none"; $("cmpTable").innerHTML = ""; };
$("btnCmpRun").onclick = function () {
  if (CMP.length < 2) return errBox("对比至少需要 2 个槽位");
  var btn = this; btn.disabled = true; btn.textContent = "对比回测中…";
  postJson("/api/quant/compare", { items: CMP }).then(function (d) {
    btn.disabled = false; btn.textContent = "▶ 对比回测";
    if (d.error) return errBox(d.error);
    CMPRES = d; renderCompare(d);
  }).catch(function (e) { btn.disabled = false; btn.textContent = "▶ 对比回测"; errBox("对比失败：" + e.message); });
};
function renderCompare(d) {
  $("cmpChart").style.display = "";
  if (!charts.cmp) charts.cmp = echarts.init($("cmpChart"));
  var pal = [COL.up, COL.blue, COL.accent, COL.green2];
  var series = d.series.map(function (x, i) {
    return { name: x.label, type: "line", showSymbol: false, lineStyle: { width: 1.6, color: pal[i % 4] }, itemStyle: { color: pal[i % 4] },
      data: x.dates.map(function (dt, j) { return [dt, x.norm[j] == null ? null : +(x.norm[j] * 100).toFixed(2)]; }) };
  });
  if (d.benchmark) series.push({ name: d.benchmark.label, type: "line", showSymbol: false, lineStyle: { width: 1.1, type: "dashed", color: COL.text4 }, itemStyle: { color: COL.text4 },
    data: d.benchmark.dates.map(function (dt, j) { return [dt, d.benchmark.norm[j] == null ? null : +(d.benchmark.norm[j] * 100).toFixed(2)]; }) });
  charts.cmp.setOption({
    backgroundColor: "transparent",
    textStyle: { fontFamily: CHART_FONT },
    // 对比 series 名 = 用户自由输入的「标题(入流水)」（截 22 字）：默认 formatter 不转义，自定义转义版
    tooltip: { trigger: "axis", confine: true, backgroundColor: COL.panel, borderColor: COL.line, textStyle: { color: COL.text1 },
      formatter: function (ps) {
        if (!ps || !ps.length) return "";
        var html = "<b>" + esc(String(ps[0].axisValue == null ? "" : ps[0].axisValue)) + "</b>";
        ps.forEach(function (p) {
          var v = Array.isArray(p.value) ? p.value[1] : p.value;
          html += "<br/>" + (p.marker || "") + esc(p.seriesName == null ? "" : p.seriesName)
            + " <b>" + (v == null ? "–" : (+v).toLocaleString("zh-CN", { maximumFractionDigits: 2 })) + "</b>";
        });
        return html;
      } },
    legend: { top: 2, textStyle: { color: COL.txt3, fontSize: 11 }, itemWidth: 14 },
    grid: { left: 50, right: 16, top: 30, bottom: 40 },
    xAxis: { type: "time", axisLabel: { color: COL.txt3 }, axisLine: { lineStyle: { color: COL.line } } },
    yAxis: { type: "value", scale: true, axisLabel: { color: COL.txt3 }, splitLine: { lineStyle: { color: COL.line } } },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 14, bottom: 4 }],
    series: series,
  }, { replaceMerge: ["series"] });
  var rows = [["累计收益率", 1], ["年化收益率", 1], ["最大回撤", 1], ["夏普比率", 3], ["卡玛比率", 3], ["日胜率", 1], ["年化超额收益", 1], ["信息比率", 3], ["交易次数", 0], ["总手续费", 0]];
  var h = "<thead><tr><th class='tl'>指标</th>" + d.series.map(function (x) { return "<th>" + esc(x.label) + "</th>"; }).join("") + "</tr></thead><tbody>";
  rows.forEach(function (rr) {
    var k = rr[0];
    if (!d.series.some(function (x) { return x.metrics[k] != null; })) return;
    h += "<tr><td class='tl dim'>" + k + "</td>" + d.series.map(function (x) {
      var v = x.metrics[k], disp = v;
      if (typeof v === "number") disp = k === "最大回撤" ? (Math.abs(v) * 100).toFixed(1) + "%" : (rr[1] === 1 ? pct(v) : (rr[1] ? +v.toFixed(rr[1]) : Math.round(v).toLocaleString()));
      var cc = /收益|超额/.test(k) ? cls(v) : "";
      return "<td class='mono " + cc + "'>" + esc(disp == null ? "–" : disp) + "</td>";
    }).join("") + "</tr>";
  });
  if (d.failed && d.failed.length) h += "<tr><td class='tl dim'>失败项</td><td colspan='" + d.series.length + "' class='tl dim'>" + d.failed.map(function (f) { return esc(f.label) + "：" + esc(f.error); }).join("；") + "</td></tr>";
  $("cmpTable").innerHTML = h + "</tbody>";
  setTimeout(function () { try { charts.cmp.resize(); } catch (e) {} }, 30);
}

/* ---------- 存策略 / 载入 ---------- */
$("btnSave").onclick = function () {
  var name = $("inSaveName").value.trim();
  if (!name) return errBox("先填「存档名」再保存");
  var desc = KIND === "builtin" ? { kind: "builtin", family: $("inFam").value, params: collectFamParams() } : { kind: "rule", spec: buildSpec() };
  postJson("/api/quant/strategies", {
    name: name, kind: KIND, payload: KIND === "rule" ? desc.spec : desc,
    note: $("inTitle").value.trim(), data_symbols: parseCodes($("inCodes").value),
  }).then(function () { errBox(""); $("inSaveName").value = ""; $("eqNote").textContent = "已存档：" + name; })
    .catch(function (e) { errBox("存档失败：" + e.message); });
};
function setKind(k) {
  KIND = k;
  $("kindSeg").querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x.dataset.k === k); });
  $("famWrap").style.display = k === "builtin" ? "" : "none";
  $("ruleWrap").style.display = k === "rule" ? "" : "none";
}
function fillBuilderFrom(strat) {
  var pl = strat.payload || {};
  $("inCodes").value = (strat.data_symbols || pl.symbols || []).join(" ");
  $("codesN").textContent = parseCodes($("inCodes").value).length + " 只";
  if (strat.kind === "builtin" && pl.family) {
    setKind("builtin");
    renderFamWrap(pl.family, pl.params);
  } else if (strat.kind === "rule") {
    setKind("rule");
    RULE = {
      entry: { mode: (pl.entry && pl.entry.mode) || "all", conds: (pl.entry && pl.entry.conds) || [] },
      exit: { mode: (pl.exit && pl.exit.mode) || "any", conds: (pl.exit && pl.exit.conds) || [] },
    };
    $("rTarget").value = pl.target_weight != null ? pl.target_weight : 0.95;
    $("rStop").value = pl.stop_loss_pct != null ? (pl.stop_loss_pct * 100) : 0;
    $("rTake").value = pl.take_profit_pct != null ? (pl.take_profit_pct * 100) : 0;
    $("rTrail").value = pl.trailing_pct != null ? (pl.trailing_pct * 100) : 0;
    renderRuleRows(); scheduleRulePreview();
  } else {
    showTab("build");
    errBox("代码存档请用 CLI / Python 编辑（quant_sim.strategies.sandbox）；可在「今日信号」对其跑信号。");
    return;
  }
  showTab("build");
}

/* ============================================================
   策略库
   ============================================================ */
function loadStrategies() {
  getJson("/api/quant/strategies").then(function (d) {
    STRATS = d.strategies || [];
    var box = $("storeCards");
    if (!STRATS.length) { box.innerHTML = "<div class='empty' style='padding:16px'>策略库为空 —— 去「造策略·回测」搭一个并存档。</div>"; return; }
    box.innerHTML = STRATS.map(function (st0, i) {
      var pl = st0.payload || {}, summ = "";
      if (st0.kind === "builtin") {
        summ = esc((pl.family || "") + " " + Object.keys(pl.params || {}).map(function (k) { return k + "=" + pl.params[k]; }).join(" "));
      } else if (st0.kind === "rule") {
        var en = ((pl.entry && pl.entry.conds) || []).length, ex = ((pl.exit && pl.exit.conds) || []).length;
        summ = "入场 " + en + " 条 · 出场 " + ex + " 条 · 仓位 " + (pl.target_weight != null ? Math.round(pl.target_weight * 100) + "%" : "–")
          + (pl.stop_loss_pct ? " · 止损 " + (pl.stop_loss_pct * 100).toFixed(0) + "%" : "")
          + (pl.trailing_pct ? " · 移动 " + (pl.trailing_pct * 100).toFixed(0) + "%" : "");
      } else { summ = "用户代码策略（回放需显式允许执行）"; }
      var syms = (st0.data_symbols || pl.symbols || []);
      return "<div class='scard'><div class='hd'><span class='nm'>" + esc(st0.name) + "</span><span class='tag " + esc(st0.kind) + "'>" + esc(st0.kind) + "</span><span style='flex:1'></span><span class='sm mono'>v" + esc(st0.version) + "</span></div>"
        + "<div class='sm'>" + summ + "</div>"
        + (st0.note ? "<div class='sm'>" + esc(st0.note) + "</div>" : "")
        + "<div class='sm mono' title='" + esc(syms.join(" ")) + "'>" + (syms.length ? esc(syms.slice(0, 5).join(" ")) + (syms.length > 5 ? " 等 " + syms.length + " 只" : "") : "未绑定标的") + " · " + esc(st0.updated || "") + "</div>"
        + "<div class='row'><button data-a='edit' data-i='" + i + "'>编辑·回测</button><button data-a='sig' data-i='" + i + "'>信号</button><button class='danger' data-a='del' data-i='" + i + "'>删除</button></div></div>";
    }).join("");
    box.querySelectorAll("button[data-a]").forEach(function (b) {
      b.onclick = function () {
        var st0 = STRATS[+b.dataset.i];
        if (b.dataset.a === "edit") fillBuilderFrom(st0);
        if (b.dataset.a === "sig") { showTab("signal"); refreshSigNames(function () { $("sigName").value = st0.name; }); }
        if (b.dataset.a === "del" && confirm("删除存档「" + st0.name + "」？（不可恢复）")) {
          postJson("/api/quant/strategies/delete", { name: st0.name }).then(loadStrategies);
        }
      };
    });
  }).catch(function (e) { errBox("策略库加载失败：" + e.message); });
}
$("btnStoreReload").onclick = loadStrategies;
$("btnStoreNew").onclick = function () { showTab("build"); };
/* ---------- 重置表单为默认 ---------- */
/* todayIso 已收敛 /lib/util.js（AK.dates.today10，五页单一来源，2026-09-10） */
function setSelValue(id, v) {
  var el = $(id);
  for (var i = 0; i < el.options.length; i++) if (el.options[i].value === v) { el.selectedIndex = i; return; }
  if (el.options.length) el.selectedIndex = 0;
}
function resetBuilderForm() {
  if (!confirm("重置策略表单为默认参数？当前未保存的修改将丢失。")) return;
  clearTimeout(prevTimer);
  setKind("builtin");
  RULE = { entry: { mode: "all", conds: [defCond()] }, exit: { mode: "any", conds: [] } };
  if (META) { renderFamWrap("dual_ma"); renderRuleRows(); }
  $("rTarget").value = 0.95; $("rStop").value = 0; $("rTake").value = 0; $("rTrail").value = 0;
  $("inCodes").value = "510300 510500 159915 512880 588000";
  $("codesN").textContent = "5 只";
  $("inStart").value = "2023-01-01"; $("inEnd").value = AK.dates.today10();
  setSelValue("inBench", "000300"); setSelValue("inMode", "local");
  setSelValue("inExec", $("inExec").options.length ? $("inExec").options[0].value : "");
  setSelValue("inSlipM", $("inSlipM").options.length ? $("inSlipM").options[0].value : "");
  $("inTitle").value = ""; $("inCash").value = 1000000;
  $("inComm").value = 2.5; $("inMinC").value = 5; $("inStamp").value = 5; $("inPart").value = 0.05;
  $("inSlipV").value = 0.0005; $("inHalt").value = 0;
  $("inBlock").checked = true; $("inLiq").checked = true; $("inWarm").value = 0;
  $("inSaveName").value = "";
  $("advRule").style.display = "none";
  errBox("");
  $("eqNote").textContent = "表单已重置为默认参数";
}
$("btnResetForm").onclick = resetBuilderForm;
$("btnLoadFromStore").onclick = function () { showTab("store"); };

/* ============================================================
   选股台
   ============================================================ */
function renderScreenerForm() {
  var sc = META.screener;
  $("scTemplate").innerHTML = "<option value=''>（不用配方，手工条件）</option>"
    + Object.keys(sc.templates).map(function (k) { return "<option value='" + k + "'>" + k + "</option>"; }).join("");
  $("scUniverse").innerHTML = sc.universes.map(function (u) { return "<option>" + u + "</option>"; }).join("");
  var condKeys = Object.keys(sc.conditions);
  $("scSort").innerHTML = condKeys.filter(function (k) { return sc.conditions[k].kind !== "bool"; })
    .map(function (k) { return "<option value='" + k + "'" + (k === "amt_20" ? " selected" : "") + ">" + esc(sc.conditions[k].label) + "</option>"; }).join("");
  $("scBoards").innerHTML = sc.boards.map(function (b) { return "<label style='margin-right:8px'><input type='checkbox' class='scb' value='" + b + "'> " + b + "</label>"; }).join("");
  var groups = {};
  condKeys.forEach(function (k) { var c = sc.conditions[k]; (groups[c.group || "其他"] = groups[c.group || "其他"] || []).push(k); });
  $("scConditions").innerHTML = Object.keys(groups).map(function (g) {
    return "<div style='margin:6px 0 2px'><span class='hint' style='letter-spacing:.1em;font-weight:700'>" + esc(g) + "</span>" + groups[g].map(function (k) {
      var c = sc.conditions[k];
      if (c.kind === "bool") return "<label class='chip' style='margin:3px 6px 3px 0;display:inline-block'><input type='checkbox' class='scbool' value='" + k + "'> " + esc(c.label) + "</label>";
      return "<span style='display:inline-block;margin:3px 10px 3px 0;font-size:11px;color:var(--text-2)'>" + esc(c.label)
        + " <input type='number' class='scr-lo' data-k='" + k + "' placeholder='低' step='" + (c.step || 1) + "' style='width:64px'> ~"
        + " <input type='number' class='scr-hi' data-k='" + k + "' placeholder='高' step='" + (c.step || 1) + "' style='width:64px'></span>";
    }).join("") + "</div>";
  }).join("");
  $("scTemplate").onchange = function () {
    var t = sc.templates[this.value];
    document.querySelectorAll(".scbool").forEach(function (b) { b.checked = false; });
    document.querySelectorAll(".scr-lo,.scr-hi").forEach(function (b) { b.value = ""; });
    if (!t) { $("scTmplNote").textContent = ""; return; }
    (t.bools || []).forEach(function (b) { var el = document.querySelector(".scbool[value='" + b + "']"); if (el) el.checked = true; });
    Object.keys(t.ranges || {}).forEach(function (k) {
      var lo = document.querySelector(".scr-lo[data-k='" + k + "']"), hi = document.querySelector(".scr-hi[data-k='" + k + "']");
      if (lo && t.ranges[k][0] != null) lo.value = t.ranges[k][0];
      if (hi && t.ranges[k][1] != null) hi.value = t.ranges[k][1];
    });
    $("scTmplNote").textContent = "配方：" + (t.note || "");
  };
}
function screenParams() {
  var bools = [], ranges = {};
  document.querySelectorAll(".scbool:checked").forEach(function (b) { bools.push(b.value); });
  document.querySelectorAll(".scr-lo").forEach(function (el) {
    var lo = el.value === "" ? null : parseFloat(el.value);
    var hiEl = document.querySelector(".scr-hi[data-k='" + el.dataset.k + "']");
    var hi = !hiEl || hiEl.value === "" ? null : parseFloat(hiEl.value);
    if (lo != null || hi != null) ranges[el.dataset.k] = [lo, hi];
  });
  var boards = []; document.querySelectorAll(".scb:checked").forEach(function (b) { boards.push(b.value); });
  return {
    universe: $("scUniverse").value, boards: boards, bools: bools, ranges: ranges,
    sort: $("scSort").value, ascending: $("scAsc").checked, limit: num("scLimit", 60),
    days: num("scDays", 320), enrich: $("scEnrich").checked, refresh: $("scRefresh").checked,
    custom_codes: $("scCustom").value.trim() || null,
  };
}
var lastScreenRows = [];
$("btnScreenRun").onclick = function () {
  var p = screenParams();
  if (p.enrich) $("screenStatus").textContent = "估值精筛要逐票打远端接口，候选多时可能要几分钟…";
  submitJob("screener", p, $("btnScreenRun"), $("screenStatus"), renderScreenResult);
};
function renderScreenResult(r) {
    lastScreenRows = r.table || [];
    $("screenSteps").textContent = "漏斗：" + Object.keys(r.steps || {}).map(function (k) { return k + "=" + r.steps[k]; }).join(" → ");
    $("screenChips").innerHTML = (r.warnings || []).map(function (w) { return "<span class='chip' style='color:var(--flat)'>" + esc(w) + "</span>"; }).join("")
      + (r.asof ? "<span class='chip'>截面 " + esc(r.asof) + "</span>" : "");
    var cols = (r.columns || []).filter(function (c) { return (r.display_cols || {})[c]; });
    var t = $("screenTable");
    if (!lastScreenRows.length) { t.innerHTML = '<tbody><tr><td class="tl dim" style="padding:14px">无符合条件标的。</td></tr></tbody>'; $("btnScreenFill").disabled = true; return; }
    var h = "<thead><tr>" + cols.map(function (c, i) { return "<th class='" + (i < 3 ? "tl" : "") + "'>" + esc(r.display_cols[c]) + "</th>"; }).join("") + "</tr></thead><tbody>";
    lastScreenRows.forEach(function (row) {
      h += "<tr>" + cols.map(function (c, i) {
        var v = row[c];
        if (typeof v === "boolean") v = v ? "✓" : "";
        else if (typeof v === "number") v = +v.toFixed(2);
        return "<td class='" + (i < 3 ? "tl" : "") + " mono" + (c === "chg_1d" ? " " + cls(v) : "") + "'>" + esc(v == null ? "" : v) + "</td>";
      }).join("") + "</tr>";
    });
    t.innerHTML = h + "</tbody>";
    $("btnScreenFill").disabled = false;
}
$("btnScreenFill").onclick = function () {
  var codes = lastScreenRows.slice(0, 20).map(function (r) { return r.ticker; }).filter(Boolean);
  $("inCodes").value = codes.join(" ");
  $("codesN").textContent = codes.length + " 只";
  showTab("build");
  $("eqNote").textContent = "已从选股台回填 " + codes.length + " 只标的";
};

/* ============================================================
   今日信号
   ============================================================ */
function refreshSigNames(then) {
  return getJson("/api/quant/strategies").then(function (d) {
    STRATS = d.strategies || [];
    $("sigName").innerHTML = STRATS.length ? STRATS.map(function (s) { return "<option>" + esc(s.name) + "</option>"; }).join("") : "<option value=''>（策略库为空）</option>";
    if (typeof then === "function") then();
  }).catch(function () { if (typeof then === "function") then(); });
}
$("btnSigRun").onclick = function () {
  var name = $("sigName").value;
  if (!name) return errBox("策略库为空");
  submitJob("signals", { name: name, start: $("sigStart").value, cash: num("sigCash", 1e6), allow_exec: $("sigAllowExec").checked },
    $("btnSigRun"), $("sigStatus"), renderSigResult);
};
function renderSigResult(r) {
      $("mdOut").innerHTML = md2html(r.report || "");
      var b = $("btnSigCopy");
      b.style.display = "";
      b.onclick = function () {
        // 非安全上下文（http://IP 手机访问）clipboard 为 undefined/必然 reject：按真实结果显示，不假报「已复制」
        AK.copyText(r.report || "").then(function (ok) {
          b.textContent = ok ? "已复制 ✓" : "复制失败（非 HTTPS 环境不可复制）";
          setTimeout(function () { b.textContent = "复制 Markdown"; }, 1800);
        });
      };
}
function md2html(md) {
  var lines = String(md).split(/\r?\n/), out = [], inT = false;
  function inl(s) { return esc(s).replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>"); }
  lines.forEach(function (ln) {
    if (/^\s*\|/.test(ln)) {
      if (/^[\s|:\-]+$/.test(ln)) return;
      if (!inT) { out.push('<table class="qt"><tbody>'); inT = true; }
      out.push("<tr>" + ln.split("|").slice(1, -1).map(function (c) { return "<td class='mono'>" + inl(c.trim()) + "</td>"; }).join("") + "</tr>");
      return;
    }
    if (inT) { out.push("</tbody></table>"); inT = false; }
    var m;
    if (m = ln.match(/^(#{1,4})\s+(.*)/)) out.push("<h4>" + inl(m[2]) + "</h4>");
    else if (/^\s*[-*]\s+/.test(ln)) out.push('<div class="mdli">· ' + inl(ln.replace(/^\s*[-*]\s+/, "")) + "</div>");
    else if (/^\s*---/.test(ln)) out.push("<hr>");
    else if (!ln.trim()) out.push('<div style="height:5px"></div>');
    else out.push("<p>" + inl(ln) + "</p>");
  });
  if (inT) out.push("</tbody></table>");
  return out.join("");
}

/* ============================================================
   参数研究（网格）
   ============================================================ */
function gridReqBase() {
  var grids = {};
  $("gGrid").value.split(/\n+/).forEach(function (ln) {
    var i = ln.indexOf("=");
    if (i > 0) grids[ln.slice(0, i).trim()] = ln.slice(i + 1).trim();
  });
  return {
    strategy: { kind: "builtin", family: $("gFam").value, params: {} },
    codes: parseCodes($("gCodes").value), start: $("gStart").value, end: $("gEnd").value,
    benchmark: $("gBench").value.trim() || null, grids: grids, rank_by: $("gRank").value,
    data_mode: "auto", cash: num("inCash", 1e6), warmup_bars: num("gWarm", 60),
  };
}
$("btnGridRun").onclick = function () {
  var p = gridReqBase();
  if (!p.codes.length) return errBox("网格研究需要先填标的池");
  if (!Object.keys(p.grids).length) return errBox("网格为空：一行一个，如 fast=5,10,20");
  submitJob("grid", p, $("btnGridRun"), $("gridStatus"), function (r) {
    renderGridResult(r);
  });
};
$("btnWfRun").onclick = function () {
  var p = gridReqBase();
  if (!p.codes.length) return errBox("Walk-Forward 需要先填标的池");
  if (!Object.keys(p.grids).length) return errBox("WF 每折都要跑网格，请先填候选值");
  p.train_days = num("gTrain", 244); p.test_days = num("gTest", 63);
  submitJob("wf", p, $("btnWfRun"), $("wfStatus"), renderWfResult);
};

function applyVariant(row) {
  var fam = $("gFam").value;
  showTab("build"); setKind("builtin");
  var params = {};
  var fmeta = META.families.filter(function (x) { return x.key === fam; })[0] || { params: [] };
  fmeta.params.forEach(function (pp) { if (row[pp.k] != null) params[pp.k] = row[pp.k]; });
  renderFamWrap(fam, params);
  if ($("gCodes").value) { $("inCodes").value = $("gCodes").value; $("codesN").textContent = parseCodes($("inCodes").value).length + " 只"; }
  $("inStart").value = $("gStart").value; $("inEnd").value = $("gEnd").value;
  var gb = $("gBench").value.trim();
  setSelValue("inBench", gb);
  // 基准值不在本地列表时 select 会静默落回空——回测照跑但没了基准线，必须明说
  $("eqNote").textContent = "已带入网格参数，可复跑单组回测细看"
    + (gb && $("inBench").value !== gb ? "（注意：基准 " + gb + " 不在本地缓存列表，回测将无基准线）" : "");
}
function fmtCell(c, v) {
  if (v == null) return "–";
  if (typeof v === "number") return /收益率|回撤|胜率/.test(c) ? pct(v) : +v.toFixed(3);
  return String(v).slice(0, 40);
}
function renderGridResult(r) {
  lastGridR = r;
  $("gridNote").textContent = (r.n_variants || (r.rows || []).length) + " 组合 · 按" + r.rank_by + "排名（点击行带入造策略）";
  var t = $("gridTable");
  var show = ["累计收益率", "年化收益率", "最大回撤", "夏普比率", "卡玛比率", "交易次数", "胜率"];
  var cols = (r.columns || []).filter(function (c) { return (r.grid_keys || []).indexOf(c) >= 0 || show.indexOf(c) >= 0; });
  var h = "<thead><tr><th>#</th>" + cols.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr></thead><tbody>";
  (r.rows || []).forEach(function (row, i) {
    h += "<tr data-i='" + i + "'" + (i === 0 ? " class='best'" : "") + "><td class='dim'>" + (i + 1) + "</td>" + cols.map(function (c) {
      var v = row[c];
      var cc = c === "累计收益率" ? cls(v) : "";
      return "<td class='mono " + cc + "'>" + esc(fmtCell(c, v)) + "</td>";
    }).join("") + "</tr>";
  });
  t.innerHTML = h + "</tbody>";
  t.querySelectorAll("tr[data-i]").forEach(function (tr) {
    tr.onclick = function () { applyVariant(r.rows[+tr.dataset.i]); };
  });
  var ok2 = (r.grid_keys || []).length === 2;
  $("heatWrap").style.display = ok2 ? "" : "none";
  if (ok2) renderHeat();
}
function renderHeat() {
  var r = lastGridR; if (!r) return;
  var kx = r.grid_keys[1], ky = r.grid_keys[0], metric = $("gHeatSel").value;
  var xs = [], ys = [];
  (r.rows || []).forEach(function (row) {
    if (xs.indexOf(row[kx]) < 0) xs.push(row[kx]);
    if (ys.indexOf(row[ky]) < 0) ys.push(row[ky]);
  });
  var srt = function (a) { return a.slice().sort(function (p, q) { return (typeof p === "number" && typeof q === "number") ? p - q : String(p) < String(q) ? -1 : 1; }); };
  xs = srt(xs); ys = srt(ys);
  var data = [], vals = [];
  (r.rows || []).forEach(function (row) {
    var v = row[metric];
    if (v == null) return;
    v = +v; vals.push(v);
    data.push([xs.indexOf(row[kx]), ys.indexOf(row[ky]), +v.toFixed(4)]);
  });
  if (!data.length) { $("heatWrap").style.display = "none"; return; }
  if (!charts.heat) charts.heat = echarts.init($("gHeat"));
  charts.heat.setOption({
    backgroundColor: "transparent",
    textStyle: { fontFamily: CHART_FONT },
    tooltip: { position: "top", confine: true, backgroundColor: COL.panel, borderColor: COL.line, textStyle: { color: COL.text1 },
      formatter: function (o) { return esc(String(ky)) + "=" + esc(String(ys[o.data[1]])) + " × " + esc(String(kx)) + "=" + esc(String(xs[o.data[0]])) + "<br>" + esc(metric) + "：<b>" + o.data[2] + "</b>"; } },
    grid: { left: 64, right: 60, top: 28, bottom: 34 },
    xAxis: { type: "category", data: xs.map(String), name: kx, axisLabel: { color: COL.txt3 }, splitArea: { show: true } },
    yAxis: { type: "category", data: ys.map(String), name: ky, axisLabel: { color: COL.txt3 }, splitArea: { show: true } },
    visualMap: { min: Math.min.apply(null, vals), max: Math.max.apply(null, vals), calculable: true, orient: "vertical", right: 4, top: "center", textStyle: { color: COL.txt3, fontSize: 10 }, inRange: { color: [COL.down, COL.bg1, COL.up] } },   // 中点=面板色（随主题）
    series: [{ type: "heatmap", data: data, label: { show: data.length <= 60, fontSize: 9, color: COL.text1, formatter: function (o) { return o.data[2]; } }, itemStyle: { borderColor: COL.line, borderWidth: 1 }, emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(0,0,0,.3)" } } }],
  }, { replaceMerge: ["series"] });
  charts.heat.off("click");
  charts.heat.on("click", function (o) {
    if (o.componentType !== "series") return;
    var row = (r.rows || []).filter(function (x) { return x[kx] === xs[o.data[0]] && x[ky] === ys[o.data[1]]; })[0];
    if (row) applyVariant(row);
  });
  setTimeout(function () { try { charts.heat.resize(); } catch (e) {} }, 30);
}
$("gHeatSel").onchange = renderHeat;

function miniTable(el, rows, cap) {
  var t = $(el);
  if (!rows || !rows.length) { t.innerHTML = "<tbody><tr><td class='tl dim' style='padding:8px'>（无数据）</td></tr></tbody>"; return; }
  var cols = Object.keys(rows[0]).filter(function (c) {
    return rows.some(function (x) { return x[c] != null && x[c] !== ""; });
  }).slice(0, cap || 12);
  var h = "<thead><tr>" + cols.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr></thead><tbody>";
  rows.forEach(function (row) {
    h += "<tr>" + cols.map(function (c) {
      var v = row[c];
      if (typeof v === "number") v = +v.toFixed(4);
      else if (typeof v === "string") v = v.length > 10 && /^\d{4}-\d{2}-\d{2}/.test(v) ? v.slice(0, 10) : v.slice(0, 30);
      return "<td class='mono'>" + esc(v == null ? "–" : v) + "</td>";
    }).join("") + "</tr>";
  });
  t.innerHTML = h + "</tbody>";
}
function renderWfResult(r) {
  $("wfPanel").style.display = "";
  var v = r.verdict || {};
  var lv = String(v.level || v.结论 || "");
  var okLv = /ok|good|stable|通过|稳定/i.test(lv);
  $("wfVerdict").innerHTML = lv ? "<span class='chip' style='color:" + (okLv ? "var(--up,#B4372C)" : "var(--accent,#8C6D1F)") + "'>判定 " + esc(lv) + (v.reason ? " · " + esc(v.reason) : "") + "</span>" : "";
  var om = r.oos_metrics || {};
  var st = "<span class='chip'>IS/OOS 比 " + (r.overfit_ratio == null ? "–" : (+r.overfit_ratio).toFixed(2)) + "</span>"
    + "<span class='chip'>排名 " + esc(r.rank_by || "") + "</span>"
    + "<span class='chip'>预热 " + (r.warmup_days || 0) + " bar</span>"
    + ["累计收益率", "年化收益率", "最大回撤", "夏普比率"].filter(function (k) { return om[k] != null; })
      .map(function (k) { return "<span class='chip'>OOS " + k + " <b class='" + (k === "最大回撤" ? "down" : om[k] > 0 ? "up" : "down") + "'>" + (typeof om[k] !== "number" ? om[k] : k === "最大回撤" ? (Math.abs(om[k]) * 100).toFixed(1) + "%" : /收益/.test(k) ? pct(om[k]) : (+om[k]).toFixed(3)) + "</b></span>"; }).join("");
  $("wfStats").innerHTML = st;
  miniTable("wfFolds", r.folds, 12);
  miniTable("wfStab", r.param_stability, 8);
  var oc = r.oos_curve;
  if (oc && oc.dates && oc.dates.length) {
    if (!charts.wf) charts.wf = echarts.init($("wfChart"));
    var e0 = oc.equity[0] || 1;
    charts.wf.setOption({
      backgroundColor: "transparent",
      textStyle: { fontFamily: CHART_FONT },
      tooltip: { trigger: "axis", confine: true, backgroundColor: COL.panel, borderColor: COL.line, textStyle: { color: COL.text1 } },
      grid: { left: 50, right: 16, top: 26, bottom: 30 },
      xAxis: { type: "category", data: oc.dates, axisLabel: { color: COL.txt3, fontSize: 10 } },
      yAxis: { type: "value", scale: true, name: "OOS 净值(=100)", nameTextStyle: { color: COL.txt3, fontSize: 10 }, axisLabel: { color: COL.txt3 }, splitLine: { lineStyle: { color: COL.line } } },
      series: [{ name: "拼接样本外净值", type: "line", showSymbol: false, lineStyle: { color: COL.up, width: 1.8 }, itemStyle: { color: COL.up }, areaStyle: { color: AKTHEME.alpha(COL.up, .06) }, data: oc.equity.map(function (x) { return x == null ? null : +(x / e0 * 100).toFixed(2); }) }],
    }, { replaceMerge: ["series"] });
    setTimeout(function () { try { charts.wf.resize(); } catch (e) {} }, 30);
  }
}

/* ============================================================
   后台任务通用（screener/signals/grid/wf/refresh）
   ============================================================ */
function submitJob(kind, params, btn, statusEl, onDone) {
  btn.disabled = true;
  postJson("/api/quant/job", { kind: kind, params: params }).then(function (r) {
    var t0 = Date.now(), failN = 0;
    statusEl.innerHTML = '任务已提交（' + r.job_id + '）<span class="dots">运行中</span>';
    var iv = setInterval(function () {
      getJson("/api/quant/job/" + r.job_id).then(function (d) {
        var secs = Math.round((Date.now() - t0) / 1000);
        failN = 0;
        if (d.status === "done") {
          clearInterval(iv); statusEl.textContent = "完成 · 用时 " + secs + "s"; btn.disabled = false;
          onDone(d.result || {});
        } else if (d.status === "error" || d.status === "unknown" || d.status === "not_found") {
          // not_found = 任务文件被清理/服务重启后句柄丢失：不处理会 2.5s 无限轮询 + 按钮永久锁死
          clearInterval(iv); btn.disabled = false;
          statusEl.textContent = d.status === "not_found" ? "任务记录已失效" : "失败";
          errBox(kind + " 任务" + (d.status === "not_found" ? "记录不存在（服务可能重启过），请重新提交" : "失败：")
            + (d.error || "") + "\n" + String(d.log_tail || "").slice(-400));
        } else {
          statusEl.innerHTML = '<span class="dots">运行中 ' + secs + "s（首次全市场导出会较久，可切页别处等）</span>";
          // 30 分钟无终态兜底：服务重启后任务状态可能停在 running，不能让按钮永久锁死
          if (secs > 30 * 60) {
            clearInterval(iv); btn.disabled = false;
            statusEl.textContent = "轮询超时（30 分钟无终态，任务可能已被丢弃）；结果可稍后在右侧「任务历史」查看";
          }
        }
      }).catch(function () {
        // 单次网络抖动不放弃；连续 8 次（≈20s）失败视为服务不可达，停止轮询并解锁
        if (++failN >= 8) {
          clearInterval(iv); btn.disabled = false;
          statusEl.textContent = "任务状态查询失败（服务不可达？）";
        }
      });
    }, 2500);
  }).catch(function (e) { btn.disabled = false; errBox("任务提交失败：" + e.message); });
}

/* ============================================================
   启动
   ============================================================ */
function boot() {
  getJson("/api/quant/meta").then(function (m) {
    if (m.error) { $("engineHint").textContent = m.error; return; }
    META = m;
    $("engineHint").textContent = "引擎内置 · 本地缓存 " + m.local_symbols.length + " 只标的";
    $("symList").innerHTML = m.local_symbols.map(function (s) { return "<option value='" + esc(s) + "'>"; }).join("");
    $("inBench").innerHTML = "<option value=''>（无基准）</option>" + m.local_symbols.map(function (s) { return "<option" + (s === "000300" ? " selected" : "") + ">" + esc(s) + "</option>"; }).join("");
    $("inExec").innerHTML = m.execution_opts.map(function (o) { return "<option value='" + o.v + "'>" + esc(o.label) + "</option>"; }).join("");
    $("inSlipM").innerHTML = m.slip_opts.map(function (o) { return "<option value='" + o.v + "'>" + esc(o.label) + "</option>"; }).join("");
    renderFresh(m.data_freshness);
    $("inEnd").value = AK.dates.today10(); $("gEnd").value = AK.dates.today10();   // 本地今天（AK.dates 单一来源）
    renderFamWrap("dual_ma");
    renderRuleRows();
    renderScreenerForm();
    var pool = null;
    try { pool = localStorage.getItem("A_QUANT_POOL"); if (pool) localStorage.removeItem("A_QUANT_POOL"); } catch (e) {}
    if (pool && parseCodes(pool).length) {
      $("inCodes").value = pool; $("codesN").textContent = parseCodes(pool).length + " 只";
      showTab("build"); $("eqNote").textContent = "已从复盘板块带入 " + parseCodes(pool).length + " 只标的，可直接回测";
    } else if (!$("inCodes").value) { $("inCodes").value = "510300 510500 159915 512880 588000"; $("codesN").textContent = "5 只"; }
  }).catch(function (e) { $("engineHint").textContent = "meta 加载失败：" + e.message; });
  loadBoard();
  // 后台标签页停掉 60s 总览轮询（浏览器本会节流，显式跳过省掉无谓请求），回前台立即补拉一次
  setInterval(function () { if (!document.hidden) loadBoard(); }, 60000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) loadBoard(); });
}
boot();
