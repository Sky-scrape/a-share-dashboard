/* A股看板 · 纸感日历选择器（AKDP）
   替换原生 <input type="date">（其弹层在浏览器 shadow DOM 里，风格无法定制）。
   与 tokens.css 同源：米白纸面、衬线标题、墨色选中圆；红色/绿色保留给涨跌语义，
   控件强调只用金/蓝/墨。比原生多一件事：只有「有数据的交易日」可选并带金点标记，
   其余日期灰显禁用——原生 date input 做不到按集合禁用离散日期。

   用法：
     var dp = AKDP.create({ mount: el, dates: ['2026-08-31' | '20260831', ...],
                            value, onChange: fn });
     dp.setDates(list); dp.setValue(v); dp.getDates(); dp.value
*/
(function (global) {
  'use strict';
  var CSS = [
    '.akdp-btn{font:inherit;color:var(--text-1);background:var(--bg-2);border:1px solid var(--line-2);',
    '  border-radius:var(--r-sm);padding:3px 8px;cursor:pointer;white-space:nowrap;line-height:1.4}',
    '.akdp-btn:hover,.akdp-btn.on{border-color:var(--accent)}',
    '.akdp-btn::after{content:"▾";margin-left:6px;color:var(--text-3);font-size:10px}',
    '.akdp-pop{position:fixed;z-index:3000;background:var(--bg-2);border:1px solid var(--line-2);',
    '  border-radius:var(--r-md);box-shadow:var(--shadow-card);padding:10px 12px 8px;width:264px;',
    '  color:var(--text-1)}',
    '.akdp-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}',
    '.akdp-ttl{font-family:var(--serif);font-weight:700;font-size:14px;letter-spacing:.04em}',
    '.akdp-nav{background:none;border:1px solid transparent;color:var(--text-2);font-size:13px;',
    '  cursor:pointer;padding:1px 9px;border-radius:var(--r-sm);line-height:1.5}',
    '.akdp-nav:hover:not(:disabled){border-color:var(--line-2);background:var(--bg-3)}',
    '.akdp-nav:disabled{color:var(--line-2);cursor:default}',
    '.akdp-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}',
    '.akdp-wd{text-align:center;font-family:var(--serif);color:var(--text-3);font-size:11px;padding:2px 0}',
    '.akdp-day{position:relative;height:30px;line-height:30px;text-align:center;border-radius:50%;',
    '  font-size:12.5px;color:var(--text-3);font-variant-numeric:tabular-nums}',
    '.akdp-day.out{color:var(--line-2)}',
    '.akdp-day.nodata{color:var(--text-3);opacity:.55}',
    '.akdp-day.has{color:var(--text-1);cursor:pointer}',
    '.akdp-day.has:hover{background:var(--bg-3)}',
    '.akdp-day.has::after{content:"";position:absolute;left:50%;bottom:2px;width:3px;height:3px;',
    '  margin-left:-1.5px;border-radius:50%;background:var(--gold)}',
    '.akdp-day.today{box-shadow:inset 0 0 0 1px var(--blue)}',
    '.akdp-day.sel{background:var(--text-1);color:var(--bg-2)!important;font-weight:700}',
    '.akdp-day.sel::after{background:var(--bg-2)}',
    '.akdp-ft{margin-top:6px;padding-top:5px;border-top:1px solid var(--line);color:var(--text-3);',
    '  font-size:10.5px;text-align:center;letter-spacing:.03em}',
  ].join('\n');
  var CSS_DONE = false;
  function injectCss() {
    if (CSS_DONE) return; CSS_DONE = true;
    var s = document.createElement('style');
    s.textContent = CSS;
    document.head.appendChild(s);
  }
  function pad(n) { return ('0' + n).slice(-2); }
  function norm(v) {   /* 'YYYYMMDD' | 'YYYY-MM-DD' -> {y,m,d,iso} */
    var s = String(v), y, m, d;
    if (s.length === 8) { y = +s.slice(0, 4); m = +s.slice(4, 6); d = +s.slice(6, 8); }
    else { var p = s.split('-'); y = +p[0]; m = +p[1]; d = +p[2]; }
    return { y: y, m: m, d: d, iso: y + '-' + pad(m) + '-' + pad(d) };
  }
  function create(opts) {
    injectCss();
    var uid = 'akdp' + Math.random().toString(36).slice(2, 8);
    var dates = [], byIso = {}, value = null;
    var monthY = 0, monthM = 0, months = {};   /* months: 'YYYY-M' -> true */
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'akdp-btn'; btn.title = opts.title || '选择日期';
    var pop = document.createElement('div');
    pop.className = 'akdp-pop'; pop.id = uid; pop.style.display = 'none';
    opts.mount.appendChild(btn);
    document.body.appendChild(pop);

    function reindex() {
      byIso = {}; months = {};
      dates.forEach(function (v) {
        var n = norm(v);
        byIso[n.iso] = v;
        months[n.y + '-' + n.m] = true;
      });
    }
    function renderBtn() {
      if (!value) { btn.textContent = '选择日期'; return; }
      var n = norm(value);
      btn.textContent = n.m + '月' + n.d + '日';
    }
    function goMonth(y, m) { monthY = y; monthM = m; render(); }
    function shift(k) {   /* 跳到有数据的上/下个月，空月自动越过 */
      var t = findMonth(k);
      if (t) goMonth(t.y, t.m);
    }
    function findMonth(k) {   /* 沿 k 方向找下一个有数据的月（最多 36 个月） */
      var y = monthY, m = monthM + k, i = 0;
      while (i++ < 36) {
        if (m < 1) { m = 12; y--; } else if (m > 12) { m = 1; y++; }
        if (months[y + '-' + m]) return { y: y, m: m };
        m += k;
      }
      return null;
    }
    function render() {
      var n7 = new Date(monthY, monthM - 1, 1);
      var offset = (n7.getDay() + 6) % 7;                 /* 周一为第 0 列 */
      var dim = new Date(monthY, monthM, 0).getDate();
      var prevDim = new Date(monthY, monthM - 1, 0).getDate();
      var now = new Date(), todayIso = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
      var selIso = value ? norm(value).iso : null;
      var prevT = findMonth(-1), nextT = findMonth(1);
      var html = '<div class="akdp-hd">' +
        '<button type="button" class="akdp-nav" data-nav="-1"' +
          (prevT ? '' : ' disabled') + '>‹</button>' +
        '<span class="akdp-ttl">' + monthY + '年' + monthM + '月</span>' +
        '<button type="button" class="akdp-nav" data-nav="1"' +
          (nextT ? '' : ' disabled') + '>›</button></div>';
      html += '<div class="akdp-grid">' + ['一', '二', '三', '四', '五', '六', '日'].map(function (w) {
        return '<span class="akdp-wd">' + w + '</span>';
      }).join('');
      var rows = Math.ceil((offset + dim) / 7);
      for (var i = 0; i < rows * 7; i++) {
        var y = monthY, m = monthM, d, out = false;
        if (i < offset) { d = prevDim - offset + 1 + i; out = true; m = monthM - 1; if (m < 1) { m = 12; y--; } }
        else if (i >= offset + dim) { d = i - offset - dim + 1; out = true; m = monthM + 1; if (m > 12) { m = 1; y++; } }
        else { d = i - offset + 1; }
        var iso = y + '-' + pad(m) + '-' + pad(d);
        var has = !out && byIso[iso] !== undefined;
        var cls = 'akdp-day' + (out ? ' out' : has ? ' has' : ' nodata');
        if (!out && iso === todayIso) cls += ' today';
        if (iso === selIso) cls += ' sel';
        html += '<span class="' + cls + '"' + (has ? ' data-v="' + byIso[iso] + '"' : '') + '>' + d + '</span>';
      }
      html += '</div>';
      html += '<div class="akdp-ft">金点 = 有数据 · 共 ' + dates.length + ' 个可选交易日</div>';
      pop.innerHTML = html;
      Array.prototype.forEach.call(pop.querySelectorAll('.akdp-nav'), function (b) {
        b.onclick = function () { shift(+b.getAttribute('data-nav')); };
      });
      Array.prototype.forEach.call(pop.querySelectorAll('.akdp-day.has'), function (el) {
        el.onclick = function () {
          api.value = value = el.getAttribute('data-v');
          close(); renderBtn();
          if (opts.onChange) opts.onChange(value);
        };
      });
    }
    function place() {
      pop.style.display = 'block';
      var r = btn.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
      var left = Math.max(8, Math.min(r.left, global.innerWidth - w - 8));
      var top = r.bottom + 4;
      if (top + h > global.innerHeight - 8) top = Math.max(8, r.top - h - 4);
      pop.style.left = left + 'px'; pop.style.top = top + 'px';
      btn.classList.add('on');
    }
    function openPop() {
      var n = value ? norm(value) : (dates.length ? norm(dates[0]) : norm(new Date().getFullYear() + '-' + pad(new Date().getMonth() + 1) + '-' + pad(new Date().getDate())));
      api._open = true;
      goMonth(n.y, n.m); place();
    }
    function close() { api._open = false; pop.style.display = 'none'; btn.classList.remove('on'); }
    btn.onclick = function (e) { e.stopPropagation(); api._open ? close() : openPop(); };
    pop.onclick = function (e) { e.stopPropagation(); };
    document.addEventListener('click', function () { if (api._open) close(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && api._open) close(); });
    global.addEventListener('resize', function () { if (api._open) place(); });
    global.addEventListener('scroll', function () { if (api._open) place(); }, true);

    var api = {
      value: null, el: btn, _open: false,
      setDates: function (list) {
        dates = (list || []).slice(); reindex();
        if (api._open) render();
      },
      setValue: function (v) {
        value = api.value = v; renderBtn();
        if (api._open) { var n = norm(v); goMonth(n.y, n.m); }
      },
      getDates: function () { return dates.slice(); },
    };
    if (opts.dates) api.setDates(opts.dates);
    if (opts.value) api.setValue(opts.value); else renderBtn();
    if (dates.length) { var f = norm(dates[0]); monthY = f.y; monthM = f.m; }  /* 弹层默认月 */
    return api;
  }
  global.AKDP = { create: create };
})(window);
