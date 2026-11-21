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

  /* 本地日期已收敛 lib/util.js（AK.dates，2026-09-10）：本文件此前自带 localDate8/
     localDate10 副本，与五个页面各写一份口径重复。加载顺序：宿主页先 util.js 后本文件。 */
  function localDate8() { return AK.dates.today8(); }
  function localDate10() { return AK.dates.today10(); }
  function grade(seg, todayStr) {
    if (!seg || !seg.last_date) return { c: "bad", t: "无数据" };
    var fresh = seg.last_date === todayStr;
    var ageH = seg.age_hours;
    var errs = (seg.failed && seg.failed.length) || (seg.errors && seg.errors.length);
    var incomplete = seg.coverage && seg.coverage.complete === false;
    // 盘中采集停滞（工作日盘中无新数据点）：比「缺数据」更急，明确标出防静默
    if (seg.stall && seg.stall.stalled) return { c: "warn", t: "采集中断" };
    // 周末宽容：周六/周日凌晨起上一交易日数据本就应是最新的，阈值从 36h 放宽到 120h
    var dow = new Date().getDay();
    var staleLimit = (dow === 0 || dow === 6) ? 120 : 36;
    if (!fresh && (ageH == null || ageH > staleLimit)) return { c: "bad", t: shortDate(seg.last_date) + " 未更新" };
    if (errs || incomplete) return { c: "warn", t: shortDate(seg.last_date) + " 数据不全" };
    if (!fresh) return { c: "warn", t: shortDate(seg.last_date) };
    return { c: "ok", t: shortDate(seg.last_date) };
  }
  function shortDate(s) {
    if (!s) return "";
    if (s.length >= 10) return s.slice(5).replace("-", "/");
    if (s.length === 8) return s.slice(4, 6) + "/" + s.slice(6, 8);   // 复盘 8 位日期
    return s;
  }
  function shortStamp(s) {          // "2026-11-17 17:05:38" -> "11-17 17:05"（探测时刻）
    if (!s || s.length < 16) return s || "";
    return s.slice(5, 10) + " " + s.slice(11, 16);
  }
  function detailsHtml(h) {
    var html = "";
    var r = h.rotation || {}, c = h.recap || {};
    html += "<h5>轮动数据 " + (r.last_date || "-") + (r.fetching ? "（抓取中…）" : "") + "</h5>";
    if (r.stall && r.stall.stalled) {
      html += '<div class="err">⚠ ' + esc(r.stall.note) + "</div>";
    }
    if (r.coverage && r.coverage.complete === false) {
      html += '<div class="err">分时数据不全：共 ' + (r.coverage.minutes || 0) + " 分钟，最后 " +
        (r.coverage.last_time || "-") + (r.coverage.trimmed_note ? "（" + esc(r.coverage.trimmed_note) + "）" : "") + "</div>";
    }
    if (r.failed && r.failed.length) {
      html += '<div class="err">' + r.failed.length + " 个板块抓取失败：" +
        r.failed.slice(0, 6).map(function (x) { return esc(x.code || x[0] || ""); }).join("、") +
        (r.failed.length > 6 ? " …" : "") + "</div>";
    }
    html += "<h5>复盘数据 " + (c.last_date || "-") + (c.fetching ? "（抓取中…）" : "") + "</h5>";
    if (c.errors && c.errors.length) {
      html += '<div class="err">' + c.errors.length + " 项数据抓取失败：" +
        c.errors.map(function (x) { return esc(x.module); }).join("、") + "</div>";
    }
    var dk = h.duckdb || {};
    if (dk.present) {
      html += "<h5>研究数据 " + (dk.probed ? shortDate(dk.probed) : "-") + "</h5>";
      if (dk.updated_at) {   // 探测时刻：区分「刚查的」与「几小时前的旧状态」
        html += '<div style="color:var(--text-3)">检查于 ' + esc(shortStamp(dk.updated_at)) + "</div>";
      }
      if (dk.stale) {
        html += '<div class="err">⚠ ' + esc(dk.note || "研究数据缺最近交易日") + "</div>";
        if (dk.fallback && dk.fallback.n) {
          html += "<div>备用源补回 " + (dk.fallback.ok || 0) + "/" + esc(dk.fallback.n) + " 只</div>";
        }
      } else {
        html += '<div style="color:var(--text-3)">研究数据完整 ✓</div>';
      }
    }
    var now10 = localDate10(), now8 = localDate8();
    var rotStale = r.last_date !== now10, capStale = c.last_date !== now8;
    if (rotStale) html += '<div><button data-f="rotation">↻ 重新抓取轮动</button></div>';
    if (capStale) html += '<div><button data-f="recap">↻ 重新抓取复盘（约 2-4 分钟）</button></div>';
    if (!rotStale && !capStale) html += '<div style="color:var(--text-3)">今日数据已更新 ✓</div>';
    return html;
  }

  var awayHandler = null;   // 外部点击关闭监听：由 closePop 统一摘除，任何关闭路径（胶囊再点/重抓按钮/点外部）都不泄漏
  function closePop() {
    if (pop) { pop.remove(); pop = null; }
    if (awayHandler) { document.removeEventListener("click", awayHandler); awayHandler = null; }
  }
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
      awayHandler = function (e) {
        if (pop && !pop.contains(e.target) && !slot.contains(e.target)) closePop();   // 摘除逻辑收拢在 closePop
      };
      document.addEventListener("click", awayHandler);
    }, 0);
  }

  var lastHealth = null;
  function refresh() {
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      lastHealth = h;
      var g1 = grade(h.rotation, localDate10());
      var g2 = grade(h.recap, localDate8());
      var dkStale = h.duckdb && h.duckdb.present && h.duckdb.stale;   // 研究库日线层缺口（0914 实例）
      var worst = g1.c === "bad" || g2.c === "bad" ? "bad"
        : (g1.c === "warn" || g2.c === "warn" || dkStale ? "warn" : "ok");
      slot.className = "fresh-pill " + worst;
      slot.innerHTML = '<span class="dot"></span>轮动 ' + esc(g1.t) + " · 复盘 " + esc(g2.t);
      slot.title = "数据更新情况（点击查看）";
    }).catch(function () {
      slot.className = "fresh-pill warn";
      slot.innerHTML = '<span class="dot"></span>暂时连不上服务';
    });
  }
  slot.addEventListener("click", function () {
    if (pop) { closePop(); return; }
    if (lastHealth) openPop(lastHealth); else refresh();
  });
  refresh();
  // 回前台立即核对一次（后台期间数据可能已过期，等下一轮 45s 会晚）
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
  setInterval(function () {
    if (document.hidden) return;   // 后台标签页停轮询（与主/竞价/量化页同先例，P2-3）
    refresh();
  }, 45000);   // 45s 自动核对一次（原先 120s，盘中断采预警最坏要 4 分钟才浮上来）
}
