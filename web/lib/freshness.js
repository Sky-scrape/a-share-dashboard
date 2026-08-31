/* A股看板 · 数据新鲜度胶囊（两页共用）
   用法：页面放一个 <span id="freshPillSlot"></span>，然后 renderFreshness("freshPillSlot")。
   读 /api/health：绿=今日已抓且无失败；黄=缺模块/数据不全；红=超 36 小时无新数据。
   点击展开明细（失败板块/模块、抓取锁状态），并给出对应「重抓」按钮。
   依赖：先加载 lib/util.js（全局 esc——2026-09-04 收敛，本文件不再自带转义副本）。 */
"use strict";
function renderFreshness(slotId) {
  var slot = document.getElementById(slotId);
  if (!slot) return;
  var pop = null;

  function localDate8() {
    var d = new Date();
    return "" + d.getFullYear() + String(d.getMonth() + 1).padStart(2, "0") + String(d.getDate()).padStart(2, "0");
  }
  function localDate10() {
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function grade(seg, todayStr) {
    if (!seg || !seg.last_date) return { c: "bad", t: "无数据" };
    var fresh = seg.last_date === todayStr;
    var ageH = seg.age_hours;
    var errs = (seg.failed && seg.failed.length) || (seg.errors && seg.errors.length);
    var incomplete = seg.coverage && seg.coverage.complete === false;
    // 盘中采集停滞（工作日盘中无新数据点）：比「缺数据」更急，明确标出防静默
    if (seg.stall && seg.stall.stalled) return { c: "warn", t: "盘中断采" };
    // 周末宽容：周六/周日凌晨起上一交易日数据本就应是最新的，阈值从 36h 放宽到 120h
    var dow = new Date().getDay();
    var staleLimit = (dow === 0 || dow === 6) ? 120 : 36;
    if (!fresh && (ageH == null || ageH > staleLimit)) return { c: "bad", t: shortDate(seg.last_date) + " 停滞" };
    if (errs || incomplete) return { c: "warn", t: shortDate(seg.last_date) + " 缺口" };
    if (!fresh) return { c: "warn", t: shortDate(seg.last_date) };
    return { c: "ok", t: shortDate(seg.last_date) };
  }
  function shortDate(s) {
    if (!s) return "";
    if (s.length >= 10) return s.slice(5).replace("-", "/");
    if (s.length === 8) return s.slice(4, 6) + "/" + s.slice(6, 8);   // 复盘 8 位日期
    return s;
  }
  function detailsHtml(h) {
    var html = "";
    var r = h.rotation || {}, c = h.recap || {};
    html += "<h5>板块轮动 " + (r.last_date || "-") + (r.fetching ? "（抓取中…）" : "") + "</h5>";
    if (r.stall && r.stall.stalled) {
      html += '<div class="err">⚠ ' + esc(r.stall.note) + "</div>";
    }
    if (r.coverage && r.coverage.complete === false) {
      html += '<div class="err">分时不完整：' + (r.coverage.minutes || 0) + " 分钟点，尾点 " +
        (r.coverage.last_time || "-") + (r.coverage.trimmed_note ? "（" + esc(r.coverage.trimmed_note) + "）" : "") + "</div>";
    }
    if (r.failed && r.failed.length) {
      html += '<div class="err">' + r.failed.length + " 个板块失败：" +
        r.failed.slice(0, 6).map(function (x) { return esc(x.code || x[0] || ""); }).join("、") +
        (r.failed.length > 6 ? " …" : "") + "</div>";
    }
    html += "<h5>盘后复盘 " + (c.last_date || "-") + (c.fetching ? "（抓取中…）" : "") + "</h5>";
    if (c.errors && c.errors.length) {
      html += '<div class="err">' + c.errors.length + " 个模块失败：" +
        c.errors.map(function (x) { return esc(x.module); }).join("、") + "</div>";
    }
    var now10 = localDate10(), now8 = localDate8();
    var rotStale = r.last_date !== now10, capStale = c.last_date !== now8;
    if (rotStale) html += '<div><button data-f="rotation">↻ 重抓轮动分时</button></div>';
    if (capStale) html += '<div><button data-f="recap">↻ 重抓复盘快照（约2-4分钟）</button></div>';
    if (!rotStale && !capStale) html += '<div style="color:var(--text-3)">今日数据均已抓取 ✓</div>';
    return html;
  }

  function closePop() { if (pop) { pop.remove(); pop = null; } }
  function openPop(h) {
    closePop();
    pop = document.createElement("div");
    pop.className = "fresh-pop";
    pop.innerHTML = detailsHtml(h);
    document.body.appendChild(pop);
    var rBtn = pop.querySelector('button[data-f="rotation"]');
    var cBtn = pop.querySelector('button[data-f="recap"]');
    if (rBtn) rBtn.onclick = function () { fetch("/api/fetch-rotation", { method: "POST" }).then(function () { closePop(); }); };
    if (cBtn) cBtn.onclick = function () { fetch("/api/fetch", { method: "POST" }).then(function () { closePop(); }); };
    setTimeout(function () {
      document.addEventListener("click", function away(e) {
        if (pop && !pop.contains(e.target) && !slot.contains(e.target)) { closePop(); document.removeEventListener("click", away); }
      });
    }, 0);
  }

  var lastHealth = null;
  function refresh() {
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      lastHealth = h;
      var g1 = grade(h.rotation, localDate10());
      var g2 = grade(h.recap, localDate8());
      var worst = g1.c === "bad" || g2.c === "bad" ? "bad" : (g1.c === "warn" || g2.c === "warn" ? "warn" : "ok");
      slot.className = "fresh-pill " + worst;
      slot.innerHTML = '<span class="dot"></span>轮动 ' + esc(g1.t) + " · 复盘 " + esc(g2.t);
      slot.title = "数据新鲜度（点击看明细）";
    }).catch(function () {
      slot.className = "fresh-pill warn";
      slot.innerHTML = '<span class="dot"></span>服务状态未知';
    });
  }
  slot.addEventListener("click", function () {
    if (pop) { closePop(); return; }
    if (lastHealth) openPop(lastHealth); else refresh();
  });
  refresh();
  setInterval(refresh, 120000);   // 2 分钟自动核对一次
}
