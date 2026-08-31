/* A股看板 · 顶栏导航公共组件（五页共用单一来源，2026-08-31）
   页面顶栏放一个挂载点：<div id="navgroupSlot" style="display:contents"></div>
   然后引入本文件并调用：AKBAR.renderNavgroup({ active: "/recap" });
   active 取值："/auction" 实时竞价 | "/" 日内轮动 | "/recap" 盘后复盘 | "/global" 全球总览 | "/quant" 量化平台。
   可选 onNav(id, path)：返回 true 表示页面自行处理本次导航（如轮动页页内切换复盘 iframe）。
   内嵌模式（iframe）：轮动 ↔ 复盘优先走父页 switchTab，其余跳转提升到顶层窗口。
   依赖：宿主页需先加载 lib/util.js（全局 esc——2026-09-04 收敛，本文件不再自带副本）。 */
"use strict";
(function (global) {
  /* 板块顺序（顶栏展示优先级，与盯盘节奏一致）：竞价（09:15 开盘决策）→ 轮动（盘中）→ 复盘（盘后）
     → 全球（隔夜背景）→ 量化（研究工具）。全站单一来源：server.py 路由文档/启动横幅、README 均按此对齐。
     注意：顺序只是展示优先级，不改路由映射——根路径 `/` 仍是日内轮动仪表盘，竞价在 /auction。 */
  var PAGES = [
    { id: "navAuction", path: "/auction", full: "实时竞价", mini: "竞价", title: "实时竞价" },
    { id: "navRot", path: "/", full: "日内轮动", mini: "轮动", title: "日内轮动" },
    { id: "navRecap", path: "/recap", full: "盘后复盘", mini: "盘后", title: "盘后复盘" },
    { id: "navGlobal", path: "/global", full: "全球总览", mini: "全球", title: "全球总览" },
    { id: "navQuant", path: "/quant", full: "量化平台", mini: "量化", title: "量化平台" }
  ];

  function isEmbed() {
    try { return window.self !== window.top; } catch (e) { return true; }
  }

  function goTop(path) {
    if (isEmbed()) {
      try { window.top.location.href = path; return; } catch (e) { /* 跨域等异常时降级本页跳转 */ }
    }
    location.href = path;
  }

  function renderNavgroup(opts) {
    opts = opts || {};
    var slot = opts.slot ? document.getElementById(opts.slot) : document.getElementById("navgroupSlot");
    if (!slot) return;
    var active = opts.active || "/";
    var html = '<div class="navgroup"><span class="brand">A股看板<span class="dot">·</span></span>';
    PAGES.forEach(function (p) {
      html += '<button id="' + p.id + '" class="navtab' + (p.path === active ? " active" : "")
        + '" title="' + esc(p.title) + '"><span class="b-full">' + esc(p.full)
        + '</span><span class="b-mini">' + esc(p.mini) + "</span></button>";
    });
    html += '<span class="navsep"></span>';
    /* 主题切换（状态单一来源 lib/theme.js）：按钮文案 = 将要切换到的风格，
       图标随状态变（显示「晨报」=全圆 ●，显示「夜台」=半圆 ◑）；
       theme.js 未加载时降级不渲染（按钮缺席比坏按钮好）。 */
    if (global.AKTHEME) {
      html += '<button id="navTheme" class="navtab navtheme" title="切换界面风格：晨报 ⇄ 夜台">'
        + esc(global.AKTHEME.icon() + " " + global.AKTHEME.label()) + "</button>";
    }
    html += "</div>";
    slot.innerHTML = html;
    PAGES.forEach(function (p) {
      var b = document.getElementById(p.id);
      if (!b) return;
      // 活动页按钮也要绑定：宿主页可能用 onNav 做页内切换（如轮动页 轮动↔盘后 iframe），
      // 否则一旦切走就再也点不回来；同页且 onNav 未处理时视为 no-op。
      b.addEventListener("click", function () {
        if (typeof opts.onNav === "function" && opts.onNav(p.id, p.path)) return;
        if (p.path === active) return;
        // iframe 内嵌：轮动 ↔ 复盘优先走父页页内切换，避免整页重载
        if (isEmbed() && (p.path === "/" || p.path === "/recap") &&
            (active === "/" || active === "/recap")) {
          try {
            if (window.parent && typeof window.parent.switchTab === "function") {
              window.parent.switchTab(p.path === "/");
              return;
            }
          } catch (e) { /* 父页无 switchTab 时走顶层跳转 */ }
        }
        goTop(p.path);
      });
    });
    var tb = document.getElementById("navTheme");
    if (tb && global.AKTHEME) {
      tb.addEventListener("click", function () { global.AKTHEME.toggle(); });
      // 本页其他途径改了主题（storage 同步/控制台）也把按钮图标和文案跟上
      global.addEventListener("akthemechange", function () {
        tb.innerHTML = esc(global.AKTHEME.icon() + " " + global.AKTHEME.label());
      });
    }
  }

  global.AKBAR = { renderNavgroup: renderNavgroup, isEmbed: isEmbed, PAGES: PAGES };
})(window);
