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
  }
};
