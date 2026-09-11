/* 共享工具函数（2026-09-04 收敛）。
   esc() 此前在五个页面各写一份且口径不一：global/quant 两份不转义单引号，
   而龙虎榜/热股等上游文本是直接进 innerHTML 的——那是实际注入面。
   现在全部页面统一引用这一份（HTML 转义：外部数据插入 innerHTML 前必须过 esc）。 */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
  });
}

/* 跨页公共工具（AK 命名空间；esc 保持全局以便旧模板直接引用）。
   2026-09-04 第二轮收敛：剪贴板与送量化代码清洗此前各页自写且行为不一——
   剪贴板在 http://IP 非安全上下文（手机 Tailscale 访问）必然失败，旧代码静默吞掉后仍提示「已复制」；
   代码清洗 auction 版从任意位置抓 6 位数字、recap 版带 ^\d{6}$ 复验，同一脏输入两页行为不同。 */
var AK = {
  /* 复制文本到剪贴板，返回 Promise<boolean>（真实成功与否，调用方按结果显示文案）。
     非安全上下文走隐藏 textarea + execCommand 兜底（iOS Safari 亦可用）。 */
  copyText: function (text) {
    return new Promise(function (resolve) {
      var done = function (ok) { resolve(!!ok); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(AK._execCopy(text)); });
      } else {
        done(AK._execCopy(text));
      }
    });
  },
  _execCopy: function (text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      var ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (e) { return false; }
  },
  /* 送量化标的池的代码清洗：先归一到前 6 位数字，再 ^\d{6}$ 复验，去重保序。
     此前 auction 版 /(\d{6})/ 任意位置抓（"SH600519.XX" 之类会抓错位），recap 版带复验，现已统一。 */
  cleanCodes: function (codes) {
    return Array.from(new Set((codes || []).map(function (c) {
      var m = String(c).replace(/^\D*(\d{6}).*$/, "$1");
      return /^\d{6}$/.test(m) ? m : null;
    }).filter(Boolean)));
  },
/* 统一轻提示（2026-09-08 收敛）：此前 auction 硬编码纸色、recap/quant 各自内联实现、
   index/global 抓取失败无反馈。样式见 tokens.css #akToast；type 可选 "ok"/"err"。 */
  toast: function (msg, type, ms) {
    var el = document.getElementById("akToast");
    if (!el) {
      el = document.createElement("div");
      el.id = "akToast";
      document.body.appendChild(el);
    }
    el.textContent = msg == null ? "" : String(msg);
    el.className = type ? "show " + type : "show";
    clearTimeout(AK._toastTimer);
    AK._toastTimer = setTimeout(function () { el.className = ""; }, ms || 2600);
  },
  /* 单块渲染错误隔离（2026-09-10 收敛）：此前 auction 有 safeRender、recap 有 try/catch
     渲染循环、quant/global 各自内联 try/catch，行为不一。统一：
     - 默认 console.error + AK.toast（auction 原行为）；
     - onErr(e, label, msg) 供需要自定义降级的页面用（recap 把错误写进各自容器 errHtml）；
     异常被吞掉但留痕，单面板失败不再连带全页空白。 */
  safeRender: function (fn, label, onErr) {
    try {
      return fn();
    } catch (e) {
      var name = label || (fn && fn.name) || "render";
      var msg = (e && e.message) ? e.message : String(e);
      console.error(name + " 渲染失败", e);
      AK.toast(name + " 渲染失败：" + msg);
      if (typeof onErr === "function") { try { onErr(e, name, msg); } catch (e2) { /* 降级自身出错不再放大 */ } }
      return undefined;
    }
  },
  /* JSON 请求包装（2026-09-10 收敛）：此前 quant 自带 postJson/getJson，其余页面裸 fetch
     各写一套 .then(r=>r.json())。语义与 quant 原版一致（不判 r.ok，由调用方按业务降级）：
     - getJson(url) → Promise<data>；网络失败 reject（调用方 catch 后走各自 toast/兜底）。
     - postJson(url, body, timeoutMs)：timeoutMs 为看门狗秒→毫秒由调用方给，超时 abort 并
       抛「请求超时」；HTTP≥400 且 body.error 时抛该 error。 */
  getJson: function (url) {
    return fetch(url).then(function (r) { return r.json(); });
  },
  postJson: function (url, body, timeoutMs) {
    var ctrl = (typeof AbortController !== "undefined" && timeoutMs) ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, timeoutMs) : null;
    var finish = function (p) { if (timer) clearTimeout(timer); return p; };
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) { return finish(r.json().then(function (j) { return { code: r.status, data: j }; })); })
      .then(function (x) { if (x.data && x.data.error && x.code >= 400) throw new Error(x.data.error); return x.data; },
        function (e) { if (timer) clearTimeout(timer); if (e && e.name === "AbortError") throw new Error("请求超时（" + Math.round(timeoutMs / 60000) + " 分钟）"); throw e; });
  },
  /* 统一防抖 resize（150ms，2026-09-10 收敛）：此前 quant 无防抖、global 自写 150ms、
     rotation 直连一次性监听。拖拽窗口时图表连续 resize 很重，统一停稳后再执行。 */
  onResize: function (fn) {
    if (!AK._resizeFns) {
      AK._resizeFns = [];
      window.addEventListener("resize", function () {
        clearTimeout(AK._resizeTimer);
        AK._resizeTimer = setTimeout(function () {
          AK._resizeFns.forEach(function (f) { try { f(); } catch (e) { /* 单页回调失败不影响其他页签回调 */ } });
        }, 150);
      });
    }
    AK._resizeFns.push(fn);
  },
  /* 日期工具单一来源（2026-09-10 收敛）：此前五页 + freshness.js 各写一份本地日期格式化
     （今天 8 位/10 位、中文月日、MM/DD 缩写），口径一致但重复五份。全部走本地时区
     （不用 toISOString——UTC 会把凌晨的盘中日算成昨天）。 */
  dates: {
    today8: function () {   // 本地今天 YYYYMMDD（竞价采集/复盘快照日期口径）
      var d = new Date();
      return "" + d.getFullYear() + String(d.getMonth() + 1).padStart(2, "0") + String(d.getDate()).padStart(2, "0");
    },
    today10: function () {  // 本地今天 YYYY-MM-DD（轮动/API 日期口径）
      var d = new Date();
      return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    },
    fmtCn: function (d) {   // 中文日期：当年省略年份，如 2026-08-28 → 8月28日；跨年 → 2025年11月3日
      if (!d) return "";
      var mt = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d));
      if (!mt) return String(d);
      return (+mt[1] === new Date().getFullYear() ? "" : mt[1] + "年") + (+mt[2]) + "月" + (+mt[3]) + "日";
    },
    mdOf: function (d8) {   // 8 位日期缩写：20260908 → 09/08（非 8 位原样返回）
      d8 = String(d8 || "");
      return d8.length === 8 ? d8.slice(4, 6) + "/" + d8.slice(6, 8) : d8;
    }
  }
};
