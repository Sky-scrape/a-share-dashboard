/* A股看板 · 主题切换单一来源（五页共用，2026-09-05）
   两个主题：paper 晨报（默认暖纸色）/ cyber 夜台（深色霓虹），token 定义见 lib/tokens.css。
   职责边界：
   - CSS 变量仍是色彩单一来源，本模块只负责状态（data-theme + localStorage）、
     跨页同步与把变量取值暴露给 canvas 类渲染（ECharts/SVG 画布不解析 var()）。
   - AKTHEME.C() 按当前主题即时取值（按主题缓存）：图表代码在 setOption 时调用即可；
     切换主题后监听 akthemechange 事件重绘，自然拿到新色。
   - localStorage("ak.theme") 持久化；storage 事件让多标签页 / 父页↔iframe（轮动内嵌复盘）
     自动跟随，无需各自再写同步代码。
   依赖：宿主页在 <head> 里先引本文件（首屏无闪烁），appbar.js 的切换按钮调用 AKTHEME。 */
"use strict";
(function (global) {
  var KEY = "ak.theme";
  var THEMES = ["paper", "cyber"];

  function get() {
    try {
      var v = localStorage.getItem(KEY);
      if (THEMES.indexOf(v) >= 0) return v;
    } catch (e) { /* 隐私模式等读不到就走默认 */ }
    return "paper";
  }

  var _cache = {};   // 按主题缓存色板；切换时整体失效

  /* 语义色板：值来自 tokens.css 当前主题的 CSS 变量（fallback 为晨报纸色历史值）。
     chart 专属多色板（cat/dist9/heatTxt）无法用一个变量表达，按主题内联在本文件，
     与 CSS 变量一样遵循「切换即整体换」的口径。 */
  function buildPalette(t) {
    var cs = getComputedStyle(document.documentElement);
    var T = function (k, fb) { var v = cs.getPropertyValue(k).trim(); return v || fb; };
    var P = {
      up: T("--up", "#B4372C"), down: T("--down", "#23714A"), flat: T("--flat", "#8A6D2E"),
      blue: T("--blue", "#3D5A80"), accent: T("--accent", "#8C6D1F"), gold: T("--gold", "#8C6D1F"),
      warn: T("--warn", "#B4530A"), red2: T("--red2", "#CE7A6B"), green2: T("--green2", "#5F9B79"),
      text1: T("--text-1", "#21201C"), text2: T("--text-2", "#57534A"), text3: T("--text-3", "#8A8478"),
      text4: T("--text-4", "#B0AAA0"),
      line: T("--line", "#DDD5C6"), line2: T("--line-2", "#C4BBA6"), split: T("--split", "#E7E0D2"),
      panel: T("--bg-2", "#FDFBF6"), panel2: T("--bg-3", "#F1ECE1"), hover: T("--hover-row", "#F6F0E2"),
      bg0: T("--bg-0", "#EFEAE0"), bg1: T("--bg-1", "#F6F3EC"),
      ramp: [T("--ramp-1", "#23714A"), T("--ramp-2", "#5F9B79"), T("--ramp-3", "#B3D4BF"), T("--ramp-4", "#F1ECE1"),
             T("--ramp-5", "#D98E7E"), T("--ramp-6", "#C05F52"), T("--ramp-7", "#B4372C")],
      cat: (t === "cyber"
        ? ["#35E0FF", "#FF4D6D", "#F0C674", "#2BE39B", "#B18CFF", "#FF9E57", "#6C9EFF", "#3ADBC7", "#FF7AB6", "#9AE65C", "#8FA5FF", "#E8C170"]
        : ["#8C6D1F", "#B4372C", "#3D5A80", "#23714A", "#7E5A9E", "#C0734A", "#4E7E9E", "#8FA98F", "#B08D3E", "#9E4A6E", "#6B8E5A", "#7A7568"]),
      /* 涨跌分布 9 档（同花顺风格：中间深两端浅，红涨绿跌） */
      dist9: (t === "cyber"
        ? ["#FF9FB0", "#FF6B8B", "#F2355F", "#D91F4E", "#3A4C74", "#0F7A54", "#17A673", "#35D99B", "#7FF0C9"]
        : ["#D98E7E", "#CE7A6B", "#C05F52", "#A93E33", "#B8B2A4", "#5F9B79", "#3D7F5C", "#8CBFA3", "#B3D4BF"]),
      /* 分时热力文字/格子 4 档（弱→强，红涨绿跌）+ 平盘 */
      heatTxt: (t === "cyber"
        ? { pos: ["#FF97AB", "#FF6B8B", "#F2355F", "#D91F4E"], neg: ["#63E6B5", "#35D99B", "#17A673", "#0B7A52"], zero: "#5D7099" }
        : { pos: ["#FF8A80", "#EF5350", "#E53935", "#B71C1C"], neg: ["#4DB6AC", "#26A69A", "#00897B", "#00695C"], zero: "#78909C" }),
      /* 图表高亮专用（历史字面色，纸色下比语义色更亮） */
      hiGold: t === "cyber" ? "#FFD166" : "#FFB300",
      hiBlue: t === "cyber" ? "#7FAFFF" : "#4C8DFF",
      amber: t === "cyber" ? "#FFD166" : "#D9A23B"
    };
    /* up/down 的 rgb 三元组字符串：ECharts 渐变 areaStyle 直接拼 rgba(r,g,b,a) 用 */
    var hex2rgb = function (h) {
      var n = parseInt(String(h).replace("#", ""), 16);
      return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    };
    P.upRgb = hex2rgb(P.up).join(",");
    P.downRgb = hex2rgb(P.down).join(",");
    return P;
  }

  /* 取当前主题色板（每次调用返回同一引用，直到主题切换失效） */
  function C() {
    var t = get();
    if (!_cache[t]) _cache[t] = buildPalette(t);
    return _cache[t];
  }

  /* 通用工具：#rrggbb → rgba(r,g,b,a)（canvas 图表半透明填充统一走这里） */
  function alpha(hex, a) {
    var n = parseInt(String(hex).replace("#", ""), 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  /* 按变量名即时取 rgb 三元组（数组）：热力连续色带 mix 用 */
  function rgbOf(varName, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
    if (!/^#[0-9A-Fa-f]{6}$/.test(v)) return (fallback || [0, 0, 0]).slice();
    var n = parseInt(v.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function apply(t, fromStorage) {
    if (THEMES.indexOf(t) < 0) t = "paper";
    if (document.documentElement.getAttribute("data-theme") !== t) {
      document.documentElement.setAttribute("data-theme", t);
    }
    _cache = {};
    if (!fromStorage) {
      try { localStorage.setItem(KEY, t); } catch (e) { /* 写不进就仅本次生效 */ }
    }
    try {
      global.dispatchEvent(new CustomEvent("akthemechange", { detail: { theme: t } }));
    } catch (e) { /* 极旧浏览器无 CustomEvent：页面图表留旧色，CSS 主题仍生效 */ }
  }

  function set(t) { apply(t, false); }
  function toggle() { set(get() === "cyber" ? "paper" : "cyber"); }
  function isCyber() { return get() === "cyber"; }
  /* 切换按钮文案：显示将要切换到的风格 */
  function label() { return isCyber() ? "晨报" : "夜台"; }
  /* 切换按钮图标：显示「晨报」时为全圆（●），显示「夜台」时为半圆（◑，半月=夜台） */
  function icon() { return isCyber() ? "●" : "◑"; }

  /* 多标签页 / 父页↔iframe 同步：storage 事件不回写 localStorage（避免互相触发死循环） */
  global.addEventListener("storage", function (e) {
    if (e.key === KEY && e.newValue && THEMES.indexOf(e.newValue) >= 0) apply(e.newValue, true);
  });

  /* 首次加载立即落 data-theme：本文件在 <head> 引入，先于首帧，不会闪纸色 */
  apply(get(), true);

  global.AKTHEME = { get: get, set: set, toggle: toggle, isCyber: isCyber, label: label, icon: icon, C: C, alpha: alpha, rgbOf: rgbOf };
})(window);
