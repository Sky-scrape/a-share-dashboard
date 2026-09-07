# -*- coding: utf-8 -*-
"""冒烟测试：起一个临时端口的 server，断言 API 契约与前端结构。

用法:
    python tests/smoke.py            # 自动选空闲端口
    python tests/smoke.py --port 8099

断言覆盖（对应 2026-08-30 设计重构 + 量化平台板块内置）:
- /api/quant/meta|backtest|strategies|job|rule/preview|compare|jobs：量化引擎接线 API 契约（引擎在 quant/）
- /quant-results/*：自包含报告只读直链与路径穿越防护
- /api/quant: 总览面板（collector 聚合 quant/ 产物）顶层结构/platform 字段/流水行字段
- 量化页: id 唯一、四表/散点/入口在、tokens 共享、四页导航均有量化入口
- /api/health: 结构齐、rotation/recap 字段在、状态文件不再错位（last_date 非 8-25 死值需真实日期格式）
- /api/sentiment: series 非空、含 weights_version、每日行含 index/label
- /api/rotation-stats: 与旧形状兼容（dates/speed/persistent/newcomers/leaders_5d）
- /api/rotation-matrix: dates/boards/patterns 齐
- /api/recap/dates + /data/*.json: 最新快照 13 模块齐（registry 一致性）
- /api/recap/modules: registry 与快照模块名一致
- 笔记 API: POST -> GET 回读一致（测试日期用 19700101，跑完删掉）
- /api/quote: 结构或错误降级（不强依赖网络）
- 前端结构: 两页 id 唯一；recap 页 4 个叙事组存在、每个 render* 在 RENDER_PLAN 里注册、
  新鲜度胶囊槽位在；rotation 页布局预设按钮与研究面板在；auction 页状态条/热榜/分布/曲线弹窗在、
  数据日诚实标注与补抓状态轮询在；竞价窗口归属（逐轮 vs 补抓快照）与交易日历 8 位口径在；
  顶栏板块顺序（竞价→轮动→复盘→全球→量化）与路由入口齐；
  启动落点（工作日 09:10–09:30 先开竞价）走 backend/landing 单一口径
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend", "recap"))
from modules import MODULES  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(base, path, retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(base + path, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 4xx/5xx 是服务端的确定性回答：原样返回结构化结果（无数据环境下
            # /api/boards 等返回 404，断言 detail 会急切取值，不能把 body 吞成字符串）
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, body
        except Exception as e:
            if i == retries - 1:
                return None, str(e)
            time.sleep(2)


def post(base, path, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    port = free_port()
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    base = f"http://127.0.0.1:{port}"
    if not os.path.isdir(os.path.join(ROOT, "data", "rotation", "daily")):
        print("[note] 未检测到采集数据（data/ 不入库，克隆后属正常）：页面与 API 契约可测，"
              "数据依赖型断言会 FAIL——先完成一次各板块抓取即可全绿。")
    print(f"== smoke: server on {base} ==")
    srv = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"), "--port", str(port), "--no-open"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        # 等待就绪
        for _ in range(30):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        else:
            check("server 启动", False, "30 次重试无响应")
            return

        st, h = get(base, "/api/health")
        check("/api/health 200", st == 200, str(h))
        check("health.rotation 字段齐", all(k in (h.get("rotation") or {}) for k in
              ("last_date", "fetched_at", "age_hours", "failed", "task", "stall", "fetching")), str(list(h.get("rotation") or {})))
        check("health.recap 字段齐", all(k in (h.get("recap") or {}) for k in
              ("last_date", "fetched_at", "errors", "task")), str(list(h.get("recap") or {})))
        rot_task = (h.get("rotation") or {}).get("task") or {}
        check("轮动状态文件已修复错位（有 last_run）", bool(rot_task.get("last_run")), str(rot_task)[:80])
        # 轮动采集防缺失三件套（2026-09-05）：节拍对齐整分钟 / 定格点并入 / 断流自愈
        _tcs = open(os.path.join(ROOT, "backend", "rotation", "ths_collect.py"), encoding="utf-8").read()
        _srv2 = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
        check("轮动采样节拍对齐整分钟（固定 sleep 漂移跳分钟防回退）",
              "def _next_tick" in _tcs and "_next_tick(datetime.datetime.now(), args.interval)" in _tcs)
        check("轮动定格点（11:30/15:00）并入 daily 时间轴（尾部缺 15:00 防回退）",
              'for stamp in ("11:30", "15:00"):' in _tcs and "used = sorted(used + [frz[-1]]" in _tcs)
        check("轮动整轮空重试（上游抖动不留永久分钟洞）",
              "for attempt in (1, 2):" in _tcs and "time.sleep(5)" in _tcs)
        check("rotation 断流自愈：stall+锁心跳断 6 分钟摘死锁拉起常驻循环（交易时段+日历 fail-closed）",
              "_maybe_auto_rotation_revive" in _srv2 and "lock_age < 360" in _srv2
              and '("09:36" <= hm <= "14:55")' in _srv2
              and _srv2.count("self._maybe_auto_rotation_revive()") >= 2)
        check("health.global 字段齐", all(k in (h.get("global") or {}) for k in
              ("last_date", "fetched_at", "age_hours", "errors", "fetching")), str(list(h.get("global") or {})))

        st, gl = get(base, "/api/global")
        check("/api/global 200", st == 200, str(gl)[:80])
        if st == 200:
            idxs = (gl or {}).get("indices") or []
            check("global indices ≥10 且字段齐", len(idxs) >= 10 and all(
                  all(k in idxs[0] for k in ("id", "geo", "tz", "pct", "hist", "last_date")) for _ in [0]),
                  f"n={len(idxs)}")
            check("global index hist 足够画走势", len(idxs) and len(idxs[0].get("hist") or []) > 100)
            check("global index 多数含日线 OHLC（K线）", bool(idxs) and sum(
                1 for k in idxs if len(k.get("kline") or []) > 200 and len(k["kline"][0]) == 5) >= max(1, len(idxs) // 2))
            r = (gl or {}).get("radar") or {}
            th = ((r.get("us") or {}).get("themes") or []) + ((r.get("cn") or {}).get("themes") or [])
            check("global radar 双市场主题齐",
                  len((r.get("us") or {}).get("themes") or []) >= 8 and
                  len((r.get("cn") or {}).get("themes") or []) >= 8, str(list(r.keys()))[:80])
            check("global radar 主题含 RS/MOM/轨迹", bool(th) and all(
                  all(k in th[0] for k in ("today", "mom", "rs", "trail", "members"))
                  and len(th[0]["trail"]) >= 4 for _ in [0]), str(th[0] if th else "")[:120])
            check("global radar 轨迹点含日期与坐标", bool(th) and all(
                  all(k in p for k in ("d", "rs", "mom")) for p in th[0]["trail"][:2]))
            check("global heatmap 美股/中国齐", bool(((gl or {}).get("heatmap_us") or {}).get("sectors")) and
                  bool(((gl or {}).get("heatmap_cn") or {}).get("sectors")))
            _hcs = (((gl or {}).get("heatmap_cn") or {}).get("sectors")) or []
            check("global heatmap 含 5/20 日窗口字段",
                  _hcs and all(k in _hcs[0] for k in ("pct5", "pct20")) and
                  any(s.get("pct5") is not None for s in _hcs))

        st, s1 = get(base, "/api/sentiment")
        series = (s1 or {}).get("series") or []
        check("/api/sentiment 非空", st == 200 and len(series) > 50, f"len={len(series)}")
        check("sentiment 带 weights_version", s1.get("weights_version") is not None)
        last = series[-1] if series else {}
        check("sentiment 行字段齐", all(k in last for k in
              ("date", "index", "label", "zt", "dt", "max_lb", "promo_rate", "up_ratio")), str(last)[:120])

        st, rs = get(base, "/api/rotation-stats")
        check("/api/rotation-stats 旧形状兼容", st == 200 and all(k in rs for k in
              ("dates", "speed", "top10", "persistent", "newcomers", "leaders_5d")), str(list(rs or {}))[:100])
        # 逐日锚定统计（复盘页按快照日期取数，防“换日期看到同一份”回退）
        _bd = (rs or {}).get("by_date") or {}
        check("rotation-stats 含逐日锚定 by_date", st == 200 and len(_bd) >= 2,
              f"by_date={len(_bd)}")
        if len(_bd) >= 2:
            _ks = sorted(_bd)[-2:]
            check("rotation-stats by_date 逐日不同（新晋/领涨锚定当日）",
                  _bd[_ks[0]].get("newcomers") != _bd[_ks[1]].get("newcomers")
                  or _bd[_ks[0]].get("leaders_5d") != _bd[_ks[1]].get("leaders_5d"),
                  f"{ _ks}")
        # 轮动统计口径重定义（2026-09-04）：旧「当日 Top10 Jaccard」速度被 0.01~0.09 点的
        # 第10/11名边界噪声钉死在 0.89~1.0；旧「连续≥3日在榜」持续强势在极速轮动市常年为空
        # （主线走碎步+爆发路径，连续在榜看不见）。新口径：
        #   速度 = 相邻两日「近5日累计 Top10」主线集合的 Jaccard 距离；
        #   持续强势 = 近10日窗口 Top10 在榜 ≥4 日 且 累计 ≥5%。
        dp = open(os.path.join(ROOT, "backend", "derive.py"), encoding="utf-8").read()
        check("轮动速度口径=近5日累计Top10主线集合的日间差异（防 Top10 逐日 Jaccard 回潮）",
              "SPEED_MAINLINE_N = 10" in dp and "SPEED_MAINLINE_WINDOW = 5" in dp
              and "_mainline_set(pcts_all[i - SPEED_MAINLINE_WINDOW:i])" in dp)
        check("持续强势口径=近10日在榜≥4日且累计≥5%（防「连续≥3日」回潮）",
              "PERSIST_WINDOW = 10" in dp and "PERSIST_TOP_DAYS = 4" in dp
              and "PERSIST_CUM_MIN = 5.0" in dp
              and "_persistent_list(pcts_all[-PERSIST_WINDOW:])" in dp)

        st, mx = get(base, "/api/rotation-matrix")
        check("/api/rotation-matrix 齐", st == 200 and all(k in mx for k in
              ("dates", "boards", "patterns", "coverage")), str(list(mx or {}))[:100])
        # 盘中密集日或日K回填日都可产生 boards；回填日的形态字段必须置 null 不虚构
        _dense = any((c.get("minutes") or 0) >= 30 for c in (mx or {}).get("coverage") or [])
        if _dense:
            check("matrix boards 非空 + 有 cells", bool((mx or {}).get("boards")) and
                  bool((mx["boards"][0] or {}).get("cells")), "")
        elif (mx or {}).get("boards"):
            check("日K回填日 cells 的 tail30 必为 null（不拿收盘-开盘冒充尾盘30分）",
                  all(r.get("tail30") is None for b in mx["boards"] for r in b["cells"].values()))
        else:
            check("matrix 无任何可用历史日时空 boards 合法", True)
        # 2026-09-04 修复防回退：回填日（仅开/收两点）盘中见顶时刻不可知，不得把 max(开,收)
        # 冒充成「15:00 尾盘见顶」——旧口径 44 个回填日的强势板全堆进 14:30-15:00 桶（实测 665 次），
        # 「日内形态」直方图被单柱压扁、其余桶全不可读。窗口内 ≤2026-08-31 的日期均为日K回填日。
        _bf_peak_bad = [(b.get("name"), d, r.get("peak_t"))
                        for b in (mx or {}).get("boards") or []
                        for d, r in (b.get("cells") or {}).items()
                        if d <= "2026-08-31" and r.get("peak_t")]
        check("回填日 cells 的 peak_t 必为空（两点口径见顶时刻不可知，不冒充尾盘见顶）",
              not _bf_peak_bad, str(_bf_peak_bad[:3]))

        # ---- 全站板块口径统一（2026-09-01，同花顺一级行业 90）----
        st, bds = get(base, "/api/boards")
        check("/api/boards 为同花顺口径", st == 200 and bds.get("src") == "ths"
              and len(bds.get("industry") or []) >= 80
              and all(str(b.get("code", "")).startswith("881") for b in bds.get("industry") or []),
              f"src={bds.get('src')} n={len(bds.get('industry') or [])}")
        rot_html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
        rot_py = open(os.path.join(ROOT, "backend", "derive.py"), encoding="utf-8").read()
        st, dts = get(base, "/api/dates")
        check("日K回填后日期列表覆盖 7 月", st == 200 and any(d <= "2026-07-10" for d in (dts or {}).get("dates") or []),
              f"n={len((dts or {}).get('dates') or [])}")
        if (dts or {}).get("dates"):
            st, bf = get(base, "/api/day?date=2026-07-15")
            check("回填日文件 src=ths-daily + daily_only + 两点",
                  st == 200 and bf.get("src") == "ths-daily" and bf.get("daily_only") is True
                  and bf.get("times") == ["开盘", "收盘"], str(bf.get("src")))
        check("前端对回填日有专属绘制（只点不线/量价降级/对比拦截）",
              "DATA.daily_only" in rot_html and "日K回填日" in rot_html)
        # 热力图防回退：treemap 更新走 heatOption() notMerge 全量替换（merge 更新会逐代
        # 堆叠瓦片元素→顶层陈旧瓦片吞点击；lazyUpdate 在 5.5.0 treemap 上布局卡死）+
        # 点空白自动收起聚焦/钻取（热力图/榜单/曲线为聚焦语义来源面板，不触发收起）
        _uh = rot_html.split("function updateHeat")[1].split("let PALETTE")[0]
        check("热力图 treemap 更新 notMerge 全量替换（防瓦片堆叠/坏布局防回退）",
              "heatChart.setOption(opt, true);" in _uh and "false, true)" not in _uh
              and "heatChart.__heatInit" in rot_html
              and "function heatOption(" in rot_html)
        check("热力图点击信息点空白自动收起（聚焦+钻取；来源面板排除）",
              "document.addEventListener('click'" in rot_html
              and "t.closest('#heatmap, #curves, #rankPanel, #drillPanel')" in rot_html)
        # 交互语义统一（2026-09-05）：点击=只开钻取（近5日走势+池），不再联动曲线聚焦
        # （聚焦会把 Top3 别的板块曲线挂到页面上）；悬停=涨跌幅/成交额/市值
        _clk = rot_html.split("heatChart.on('click'")[1].split("});")[0]
        check("热力图点击语义统一：点击=只开钻取，再点同一板块收起（不联动聚焦）",
              "openDrill(d.name);" in _clk and "drillName === d.name" in _clk
              and "focused" not in _clk)
        check("热力图悬停 tooltip 含涨跌幅/成交额/市值（ths 指数无市值时如实显示 '-'）",
              "涨跌幅 ${fmtPct(pct)}" in rot_html and "d.mcap > 0" in rot_html
              and rot_html.count("市值 ${(d.mcap > 0)") == 1)

        if bds.get("industry"):
            nm = urllib.parse.quote(bds["industry"][0]["name"])
            st, bm = get(base, "/api/board-members?name=" + nm)
            check("钻取同为同花顺口径→必 exact 或快照缺失 none", st == 200 and bm.get("match") in ("exact", "none"),
                  f"{nm} match={bm.get('match')}")
        st, im = get(base, "/api/industry-map")
        check("/api/industry-map 一级行业单源", st == 200 and im.get("src") == "ths" and im.get("stocks", 0) > 4000,
              f"stocks={im.get('stocks')}")
        rec_html = open(os.path.join(ROOT, "web", "recap", "index.html"), encoding="utf-8").read()
        check("前端无东财板块口径文案残留", "东财实时" not in rot_html and "东财板块" not in rot_html
              and "东财分时" not in rec_html and "近似匹配" not in rec_html)
        check("点数不足时有诚实空态而非静默空白", "chartNote" in rot_html and "无法成线" in rot_html
              and "暂无完整的盘中采集日" in rot_html)
        check("隔夜外围条带有自动重拉+陈旧警示（不再只启动渲染一次）",
              "setInterval(renderOvernight" in rot_html and "visibilitychange" in rot_html
              and "数据陈旧" in rot_html)
        check("曲线空态恢复走 notMerge 重建（ECharts clear 后 merge 丢配置）",
              "curveBaseOption" in rot_html and "setOption(curveBaseOption(), true)" in rot_html)
        check("多日轮动竞赛图首帧 notMerge+后续 merge，类目按涨幅升序（底=最跌 顶=最涨，09-03 SVG DOM 实测类目轴 index 0 在屏幕底）",
              "raceBuilt" in rot_html and "a.value - b.value" in rot_html
              and "realtimeSort:true" not in rot_html
              and "id:'racebar'" in rot_html)
        check("轮动动画为涨TOP5/跌TOP5中轴蝶形（涨右跌左名居中央）",
              "涨TOP5 / 跌TOP5" in rot_html and "_day_bottom" in rot_py)
        check("涨跌幅曲线：取消聚焦后曲线真正消失（replaceMerge 替换 series；默认 merge 会按 id 保留旧曲线）",
              "replaceMerge:['series']" in rot_html
              and "focused===code" in rot_html           # 榜单行再点同一板块 = 取消聚焦
              and "drillName === d.name" in rot_html)    # 热力图再点同一板块 = 收起钻取
        # bat 必须 CRLF：cmd.exe 对 LF-only 批处理解析错乱（括号块直接崩），
        # 2026-09-03 实测：rotation 采集 bat 变 LF 后计划任务全天秒退 255、今日上午整段漏采
        import glob as _glob
        _bad_bats = []
        for _bf in _glob.glob(os.path.join(ROOT, "backend", "**", "*.bat"), recursive=True):
            _bb = open(_bf, "rb").read()
            if _bb and _bb.count(b"\n") != _bb.count(b"\r\n"):
                _bad_bats.append(_bf)
        check("所有 backend bat 均为 CRLF 行尾（LF-only 会让 cmd 解析错乱、任务秒退 255）", not _bad_bats)
        check("轮动采集器每轮采集后刷新派生面板（盘中矩阵不再冻结到收盘）",
              "run_derive" in open(os.path.join(ROOT, "backend", "rotation", "ths_collect.py"), encoding="utf-8").read())
        _tc_src = open(os.path.join(ROOT, "backend", "rotation", "ths_collect.py"), encoding="utf-8").read()
        check("轮动数据只到 15:00（用户指定；收盘后不再落 15:01/15:02 冗余点）",
              "PM_OPEN, PM_CLOSE = 13 * 60, 15 * 60" in _tc_src
              and "one_round(in_window=(hm == PM_CLOSE))" in _tc_src)
        check("首页 autoTick 跨日自动跳到最新日（浏览历史时不强拉） + 派生面板≈2分钟重拉",
              "histView" in rot_html and "refreshPanelsTick" in rot_html)
        _race2 = ((get(base, "/api/rotation-stats")[1] or {}).get("race")) or []
        check("stats.race 每日含 bottom 跌幅侧（非旧版只涨 TOP10）",
              bool(_race2) and all("bottom" in d for d in _race2))
        dates_rot = (get(base, "/api/dates")[1] or {}).get("dates") or []
        st, day = get(base, "/api/day?date=" + dates_rot[-1]) if dates_rot else (0, {})
        if day.get("boards"):
            check("/api/day 轮询采集日 src=ths + 诚实 coverage", day.get("src") == "ths"
                  and isinstance((day.get("coverage") or {}).get("complete"), bool),
                  f"date={day.get('date')} src={day.get('src')} cov={str(day.get('coverage'))[:80]}")

        st, cd = get(base, "/api/recap/dates")
        dates = (cd or {}).get("dates") or []
        check("/api/recap/dates 非空", st == 200 and len(dates) > 10, f"len={len(dates)}")

        st, mods = get(base, "/api/recap/modules")
        reg = [m["key"] for m in (mods or {}).get("modules") or []]
        check("registry 与 providers 清单一致", reg == MODULES, f"{reg[:4]}...")

        if dates:
            st, snap = get(base, "/data/" + dates[0] + ".json")
            snap_mods = list((snap or {}).get("modules") or {})
            missing = [m for m in MODULES if m not in snap_mods]
            check(f"最新快照 {dates[0]} 模块齐", st == 200 and not missing, f"缺 {missing}")

        # 笔记 API 回读
        st0, old = get(base, "/api/recap/note?date=19700101")
        st1, _ = post(base, "/api/recap/note?date=19700101", {"content": "smoke-测试-α"})
        st2, back = get(base, "/api/recap/note?date=19700101")
        check("笔记 POST->GET 一致", st1 == 200 and (back or {}).get("content") == "smoke-测试-α", str(back)[:80])
        # 清理
        note_fp = os.path.join(ROOT, "data", "recap", "notes", "19700101.md")
        if old is not None and not old.get("content") and os.path.isfile(note_fp):
            os.remove(note_fp)

        st, bad = None, None
        try:
            get(base, "/api/recap/note?date=../../etc")
        except Exception:
            pass  # 400 也接受（urllib 对 4xx 抛异常）
        req = urllib.request.Request(base + "/api/recap/note?date=..%2F..")
        try:
            urllib.request.urlopen(req, timeout=10)
            st = 200
        except urllib.error.HTTPError as e:
            st = e.code
        except Exception as e:
            st = str(e)
        check("笔记日期白名单拒绝穿越", st in (400, 404), str(st))

        # 前端结构检查
        rp = open(os.path.join(ROOT, "web", "recap", "index.html"), encoding="utf-8").read()
        ids = re.findall(r'\bid="([^"]+)"', rp)
        dup = {i for i in ids if ids.count(i) > 1}
        check("recap 页 id 唯一", not dup, str(dup))
        check("recap 页 6 叙事组", all(f'id="g-{g}"' in rp for g in ("temp", "main", "money", "risk", "spec", "pool")))
        check("recap 折叠 CSS 生效规则", ".gsec.folded .gbody" in rp)
        check("recap 锚点带 data-g", rp.count("data-g=") >= 6)
        check("recap 页渲染错误隔离", "渲染异常" in rp and "try {" in rp)
        check("recap render 全注册", all(f"[render{n}," in rp for n in
              ("Overview", "Indices", "Global", "Stats", "Dist", "Etf", "Boards", "Concepts",
               "Ladder", "Zt", "Dt", "Zb", "Popular", "Hot", "Lhb", "Regulatory", "Note",
               "SpecCycle", "SpecThemes", "SpecDeviation", "SpecFate", "SpecEvents", "SpecRules")))
        # 投机分析板块：六容器在、排序委托在、渲染器定义在（防结构回退）
        check("recap 投机分析六容器", all(f'id="{i}"' in rp for i in
              ("specCycleBox", "specThemesBox", "specDevBox", "specFateBox", "specEventsBox", "specRulesBox")))
        check("recap 投机分析渲染器定义", all(f"function render{n}(" in rp for n in
              ("SpecCycle", "SpecThemes", "SpecDeviation", "SpecFate", "SpecEvents", "SpecRules")))
        # ⑥ 明日备选池：容器/渲染器/注册/后端引擎（防结构回退）
        check("recap 明日备选池容器+渲染器", 'id="poolBox"' in rp and "function renderPool(" in rp
              and "[renderPool, \"poolBox\"]" in rp)
        sp_src2 = open(os.path.join(ROOT, "backend", "recap", "speculate.py"), encoding="utf-8").read()
        check("备选池后端引擎 build_pool", "def build_pool(" in sp_src2 and 'data["pool"]' in sp_src2
              and "_MF_QUOTA" in sp_src2 and "scan_trend(" in sp_src2)
        check("备选池 M_Final 主板口径+双组+负面清单", '"涨停组"' in sp_src2 and '"非涨停组"' in sp_src2
              and "score < ms_lu" in sp_src2 and "score < ms_nlu" in sp_src2
              and "nonzt" in sp_src2 and '"类型"' in sp_src2
              and "_MAIN_PREFIX" in sp_src2 and "_is_main_board(" in sp_src2
              and '"aggressive": (2, 0)' in sp_src2          # 进攻环境禁低吸（M_Final 配额）
              and "0.86 <= ratio60 <= 0.95" in sp_src2       # 回调低吸甜区（M_Final 位置口径）
              and "_prev_yizi_codes(" in sp_src2             # 昨日一字板排除
              and "C_Final 主板概念口径" in sp_src2          # C 线收敛版本标记
              and "s_theme + s_concept + s_ladder" in sp_src2  # C_Final 涨停组概念 15 分权重
              and "_concept_gate(" in sp_src2                # 概念独狼排除（C_Final 新增）
              and "_NLU_SECTOR_RANK_MIN" in sp_src2)         # 行业分位下限（C_Final）
        # ⑥ 选股逻辑升级：真实板块口径（轮动日线活跃度门槛+sector_mix）+ 涨停组量比档位
        check("备选池真实板块口径", "_rotation_strength(" in sp_src2 and "_lu_volr(" in sp_src2
              and "ist[\"rank\"]" in sp_src2 and "ist[\"lu_cnt\"] >= 1" in sp_src2
              and "0.55 * (ist[\"rank\"] or 0)" in sp_src2   # sector_mix 真实连续打分
              and "ind_stats is not None and ist is None" in sp_src2  # 无行业归属不选（引擎同口径）
              and "sector_meta" in sp_src2                   # 板块口径随快照留痕
              and "last_good" in sp_src2)                    # 空文件/半截文件跳过回退（17:05↔17:10 竞态）
        # ⑥ 概念维度（C_Final 一阶因子）：概念内涨停家数阶梯（<=2家桶两轮证据 -1.37% vs >=3家 +0.92%）
        check("备选池概念维度", "import concept_map" in sp_src2
              and "def _best_concept(" in sp_src2 and "def _concept_score(" in sp_src2
              and "_CONCEPT_LU_TIERS" in sp_src2             # 概念家数阶梯单一来源
              and 's_recog = _concept_score(c_lu if c_name else None, c_pct) if c_name' in sp_src2
              and "concepts_top" in sp_src2
              and '"所属概念"' in sp_src2 and '"所属概念"' in rp   # 输出行+前端展示
              and "GENERIC" in open(os.path.join(ROOT, "backend", "recap", "concept_map.py"),
                                    encoding="utf-8").read())  # 机械概念过滤（融资融券/沪深股通…）
        pv0 = open(os.path.join(ROOT, "backend", "recap", "providers.py"), encoding="utf-8").read()
        check("备选池概念数据链", '"concepts": _rows(concepts())' in pv0
              and '"concepts": rows("concepts")' in sp_src2  # 当日抓取与回填都带概念快照
              and "concept_map.json" in open(os.path.join(ROOT, "backend", "recap", "concept_map.py"),
                                             encoding="utf-8").read())
        # ⑥ 自我优化闭环：每日验证昨日备选池 → 滚动窗口统计 → 有界调参（结构规则冻结）
        check("备选池自我优化闭环", "def validate_prev_pool(" in sp_src2
              and "def update_optimizer(" in sp_src2 and "_optimizer_state(" in sp_src2
              and "pool_track.json" in sp_src2 and "pool_opt.json" in sp_src2
              and "_MF_BUY" in sp_src2                       # 执行层口径冻结不随优化漂移
              and 'data.get("pool_validation")' in sp_src2 and 'data.get("pool_optimizer")' in sp_src2
              and "昨日备选池验证" in rp)                     # 前端验证卡片
        # ⑥ 样本外追踪 + 到期复审 + 观察项到期（2026-09-04）：观察不能只挂不办
        check("备选池样本外追踪与到期复审", "def _oos_state(" in sp_src2
              and "def _oos_verdict(" in sp_src2 and "_OOS_REVIEW_EVERY" in sp_src2
              and "_OOS_BASELINE" in sp_src2                  # 基线=C3 调参区间口径
              and '"oos_reviews"' in sp_src2                  # 复审留痕 pool_opt.json
              and "样本外追踪" in rp                           # 前端渲染
              and "到期复审" in rp)
        check("备选池观察项到期动作", "def _observation_state(" in sp_src2
              and "_OBS_CONCEPT_COLD_N" in sp_src2 and "_OBS_STRONG_CONCEPT_N" in sp_src2
              and "观察项到期" in rp)
        # ⑥ 验证器概念维度：strong_concept + 概念持续性不足归因（引擎 C 线同口径）
        check("备选池验证 strong_concept", '"strong_concept"' in sp_src2
              and '"概念持续性不足"' in sp_src2 and "concept_pct" in sp_src2
              and '"strong_concept_rate"' in sp_src2)
        # 策略资金化：C_Final 资金曲线回测 + 竞价缺口前瞻回验（strategy-iter scripts）
        _bt_src = os.path.join(ROOT, "strategy-iter", "scripts", "backtest_capital.py")
        _gp_src = os.path.join(ROOT, "strategy-iter", "scripts", "gap_ahead_study.py")
        _c3_dir = os.path.join(ROOT, "strategy-iter", "runs", "concept_round3_C3")
        check("C_Final 资金曲线回测脚本与产出",
              os.path.isfile(_bt_src)
              and 'mode="realtime"' in open(_bt_src, encoding="utf-8").read()
              and os.path.isfile(os.path.join(_c3_dir, "capital_report.md"))
              and os.path.isfile(os.path.join(_c3_dir, "capital_curve.csv")))
        check("竞价缺口前瞻回验脚本与产出",
              os.path.isfile(_gp_src)
              and os.path.isfile(os.path.join(_c3_dir, "gap_ahead_report.md")))
        # ⑥ 板块对比补齐：行业指数日线缓存（881xxx index.history）+ 强于板块 + 板块持续性不足归因
        check("备选池验证板块对比", "def industry_series(" in sp_src2 and "_series_pct(" in sp_src2
              and "import industry_common" in sp_src2
              and 'startswith("881")' in sp_src2             # 881 目录口径（boards.json 单一来源）
              and "strong_sector" in sp_src2 and "板块持续性不足" in sp_src2
              and "checked_through" in sp_src2)              # 非交易日不重复请求
        # 异动/监管事件改为滚动查看（sp-scroll 容器，防回退到固定 25 条不滚动）
        check("recap 异动事件滚动容器", ".sp-scroll" in rp and "sp-scroll" in rp
              and 'class=\"sp-scroll\"' in rp)
        # 轮动统计按复盘日期锚定且随日期重渲染（防“今天和之前都一样”回退）
        check("recap 轮动统计按日锚定(by_date+SNAP.date)", "by_date" in rp and "SNAP.date" in rp
              and "renderRotStats" in rp)
        check("recap 换日期重渲染轮动统计", rp.count("renderRotStats()") >= 2)
        # 板块去重融合：轮动统计的「5日领涨板块」列与板块强度榜·近N日累计同口径重复
        #（且 leaders_5d 只累计每日 Top10，两处数字对不上），已从轮动统计中删除
        check("recap 轮动统计不再重复 5 日领涨列（口径归板块强度榜）",
              "5日领涨板块" not in rp and "板块强度榜" in rp)
        # 轮动速度/持续强势展示新口径（2026-09-04 重定义，详见 derive.py）
        check("复盘页展示新口径（在榜X/10日·累计），旧「连续≥3日」文案不回潮",
              "在榜" in rp and "近10日在榜≥4日" in rp
              and "连续≥3日" not in rp and "100%=完全换血" not in rp)
        # 投机分析后端契约：口径常量（防阈值/基准被改动）、providers 接线、registry 收录
        sp_src = open(os.path.join(ROOT, "backend", "recap", "speculate.py"), encoding="utf-8").read()
        check("投机偏离阈值口径冻结", "SEVERE_D10 = 100.0" in sp_src and "SEVERE_D30 = 200.0" in sp_src
              and '"10cm": 20.0' in sp_src and '"20cm": 30.0' in sp_src)
        check("投机基准指数口径", '"000001.SH"' in sp_src and '"399001.SZ"' in sp_src)
        check("投机 DuckDB 全市场扫描", "v_daily_qfq" in sp_src and "LAG(close,30)" in sp_src)
        pv_src = open(os.path.join(ROOT, "backend", "recap", "providers.py"), encoding="utf-8").read()
        check("providers 接线投机分析", "def speculation()" in pv_src and "speculate.compute" in pv_src)
        check("registry 收录投机分析", "speculation" in MODULES)
        check("recap 新鲜度胶囊槽", 'id="freshPillSlot"' in rp)
        check("recap 笔记走服务器 API", "/api/recap/note" in rp)
        check("recap 引用共享 tokens", "lib/tokens.css" in rp)

        rot = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
        check("rotation 布局预设按钮", rot.count("laybtn") >= 4)
        check("rotation 研究面板", 'id="matrixPanel"' in rot and "/api/rotation-matrix" in rot)
        check("rotation 覆盖度标注", "coverageNote" in rot)
        check("rotation 新鲜度胶囊槽", 'id="freshPillSlot"' in rot)
        _fsrc = open(os.path.join(ROOT, "web", "lib", "freshness.js"), encoding="utf-8").read()
        check("新鲜度胶囊展示盘中停滞告警（2026-09-02 静默失败事故防回退）",
              "r.stall" in _fsrc and "盘中断采" in _fsrc)
        # esc 单一来源（2026-09-04 收敛）：global/quant 旧副本不转义单引号，而龙虎榜/热股
        # 上游文本直接进 innerHTML——那是实际注入面；此后只许 lib/util.js 一处定义
        _usrc = open(os.path.join(ROOT, "web", "lib", "util.js"), encoding="utf-8").read()
        _esc_holders = [rot] + [
            open(os.path.join(ROOT, *pp), encoding="utf-8").read() for pp in (
                ("web", "recap", "index.html"), ("web", "global", "index.html"),
                ("web", "quant", "index.html"), ("web", "auction", "index.html"))]
        _esc_libs = [
            open(os.path.join(ROOT, "web", "lib", ff), encoding="utf-8").read()
            for ff in ("appbar.js", "freshness.js")]
        check("esc 单一来源：仅 util.js 定义（转义单引号），五页与公共组件零副本",
              "function esc(" in _usrc and "&#39;" in _usrc
              and all(("function esc" not in s and "const esc" not in s and "esc1" not in s)
                      for s in _esc_holders + _esc_libs)
              and all("util.js" in s for s in _esc_holders))
        _tok = open(os.path.join(ROOT, "web", "lib", "tokens.css"), encoding="utf-8").read()
        check("移动端顶栏/面板头换行适配在（2026-09-03 手机适配防回退）",
              "header.appbar { flex-wrap: wrap" in _tok and ".panel .head b" in _tok)
        check("rotation 引用共享 tokens", "lib/tokens.css" in rot)

        gp = open(os.path.join(ROOT, "web", "global", "index.html"), encoding="utf-8").read()
        gids = re.findall(r'\bid="([^"]+)"', gp)
        gdup = {i for i in gids if gids.count(i) > 1}
        check("global 页 id 唯一", not gdup, str(gdup))
        check("global 五城时钟", gp.count("America/New_York") >= 1 and gp.count("Asia/Hong_Kong") >= 1
              and "CITIES" in gp and gp.count("tz:") >= 5)
        check("global 地图+走势面板", 'id="mapChart"' in gp and 'id="trendChart"' in gp and "registerMap" in gp)
        check("global 轮动雷达为象限散点", 'id="radarChart"' in gp and "markArea" in gp and
              "type: 'scatter'" in gp and "ROTATION RADAR" in gp)
        check("global 雷达榜单与切换", 'id="radarList"' in gp and 'id="radarMkt"' in gp)
        check("global 热力面板", 'id="heatChart"' in gp and "type: 'treemap'" in gp)
        check("global 热力周期切换与涨跌停色阶", 'id="heatWinSeg"' in gp and "HEAT_CLAMP" in gp and
              'id="heatBar"' in gp and 'id="heatCrumbs"' in gp and "heatJump" in gp)
        check("global 滚动条槽固定+走势坐标轴", "scrollbar-gutter: stable" in gp and "containLabel" in gp)
        check("global 走势自选日期区间控件", 'id="trQuick"' in gp and 'id="trStart"' in gp and
              'id="trEnd"' in gp and "trendHist" in gp)
        check("global 走势K线形态切换", 'id="trKind"' in gp and "candlestick" in gp and
              "trendKl" in gp and "maArr" in gp)
        check("global 日内分时形态", 'data-k="min"' in gp and "renderTrendMin" in gp and
              "fetch_intraday" in open(os.path.join(ROOT, "backend", "global", "fetch_global.py"), encoding="utf-8").read())
        check("global 重抓按钮走 API", "/api/fetch-global" in gp and "/api/global" in gp)
        # 降级保护：单块失败沿用上一份并标 stale（防盘前抓取冲掉完好热力图）
        _fg = open(os.path.join(ROOT, "backend", "global", "fetch_global.py"), encoding="utf-8").read()
        check("global 热力图失败沿用上一份(stale)", '_keep("heatmap_cn")' in _fg and '_keep("heatmap_us")' in _fg
              and "stale=True" in _fg)
        check("global 引用共享 tokens", "/lib/tokens.css" in gp)
        # 顶栏导航由 lib/appbar.js 动态渲染（2026-08-31 重构）：静态 HTML 不再有按钮 id，
        # 改为断言各页引用组件且组件 PAGES 含对应入口
        ab_src = open(os.path.join(ROOT, "web", "lib", "appbar.js"), encoding="utf-8").read()
        # 顶栏板块顺序是全站单一来源（appbar.js PAGES）：2026-09-01 起竞价排第一，防默默回退
        porder = [p for _, p in re.findall(r'\{ id: "(\w+)", path: "([^"]+)"', ab_src)]
        check("顶栏板块顺序＝竞价→轮动→复盘→全球→量化",
              porder == ["/auction", "/", "/recap", "/global", "/quant"], str(porder))
        check("五页导航均引用 appbar（入口均在 PAGES）", "navGlobal" in ab_src and "appbar.js" in rot
              and "appbar.js" in rp and "appbar.js" in gp)
        # 启动落点：竞价窗口（工作日 09:10–09:30）先开竞价，其余先开轮动；两个入口共用一份口径
        sys.path.insert(0, os.path.join(ROOT, "backend"))
        import landing as _ld  # noqa: E402
        _wk = [0, 1, 2, 3, 4]           # Python tm_wday：0=周一 … 4=周五（5/6 为周末）
        check("启动落点：工作日竞价窗口内→/auction，窗口外与周末→/",
              all(_ld.landing_path(time.struct_time((2026, 9, d, 9, 20, 0, w, 1, -1))) == "/auction"
                  for d, w in enumerate(_wk)) and
              _ld.landing_path(time.struct_time((2026, 9, 1, 9, 9, 0, 1, 1, -1))) == "/" and
              _ld.landing_path(time.struct_time((2026, 9, 1, 9, 30, 0, 1, 1, -1))) == "/" and
              _ld.landing_path(time.struct_time((2026, 9, 1, 14, 0, 0, 1, 1, -1))) == "/" and
              _ld.landing_path(time.struct_time((2026, 9, 5, 9, 20, 0, 5, 1, -1))) == "/" and
              _ld.landing_path(time.struct_time((2026, 9, 6, 9, 20, 0, 6, 1, -1))) == "/")
        _sp = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
        _kp = open(os.path.join(ROOT, "start.py"), encoding="utf-8").read()
        check("server 与 start 均仅引 landing（落点不各写一套）",
              _sp.count("landing_path") >= 2 and "from landing import landing_path" in _sp
              and "from landing import landing_path" in _kp
              and "tm_wday" not in _kp and _kp.count("def landing_path") == 0)
        check("start.py 不重复弹浏览器（子进程 --no-open）", '"--no-open"' in _kp)
        check("start.py 监护自动重启在（2026-09-03 服务静默死亡事故防回退）",
              "proc.poll()" in _kp and "自动重启" in _kp and '"-u"' in _kp)
        check("世界地图 GeoJSON 存在", os.path.isfile(os.path.join(ROOT, "web", "lib", "map", "world.json")))

        # ---- 量化平台板块 ----
        st, qn = get(base, "/api/quant")
        check("/api/quant 200", st == 200, str(qn)[:80])
        check("quant 顶层结构齐", st == 200 and all(k in qn for k in
              ("generated_at", "platform", "strategies", "backtests", "reports", "research", "signals", "ledger")),
              str(list(qn or {}))[:100])
        pf = (qn or {}).get("platform") or {}
        check("quant platform 字段齐", all(k in pf for k in
              ("root", "exists", "name", "docs")) and "workshop_up" not in pf, str(pf)[:100])
        check("概览聚焦分析控件在", 'id="ovFocusSel"' in open(os.path.join(ROOT, "web", "quant",
              "index.html"), encoding="utf-8").read() and 'row["store"]' in open(os.path.join(ROOT, "backend",
              "quant", "quant_api.py"), encoding="utf-8").read())
        if pf.get("exists"):
            # 空流水（克隆后未跑过回测）时跳过行字段检查：[-1] 会 IndexError
            check("quant 回测流水行字段齐", (not qn["backtests"]) or all(k in qn["backtests"][-1] for k in
                  ("time", "title", "strategy", "symbols", "window", "cum_ret", "sharpe", "trades")),
                  str(qn["backtests"][-1:])[:120])
            check("quant 报告行含指标", (not qn["reports"]) or all(k in qn["reports"][-1] for k in
                  ("name", "window", "cum_ret", "max_dd", "benchmark", "html")), str(qn["reports"][-1:])[:120])
        qp = open(os.path.join(ROOT, "web", "quant", "index.html"), encoding="utf-8").read()
        qids = re.findall(r'\bid="([^"]+)"', qp)
        qdup = {i for i in qids if qids.count(i) > 1}
        check("quant 页 id 唯一", not qdup, str(qdup))
        check("quant 页四表/散点/入口在", 'id="btTable"' in qp and 'id="repTable"' in qp
              and 'id="scChart"' in qp and 'id="entryBody"' in qp and 'id="sideList"' in qp)
        check("quant 页 Streamlit 已彻底移除", '/api/quant/workshop' not in qp and '工坊' not in qp and 'streamlit' not in qp.lower())
        check("quant 页引用共享 tokens", "/lib/tokens.css" in qp)
        check("五页导航均有量化入口", "navQuant" in ab_src and "appbar.js" in rot
              and "appbar.js" in rp and "appbar.js" in gp and "appbar.js" in qp)
        auc_html = open(os.path.join(ROOT, "web", "auction", "index.html"), encoding="utf-8").read()
        check("五页导航均有竞价入口", "navAuction" in ab_src and 'active: "/auction"' in auc_html)
        # 竞价量比昨必须读全量存在的 auction_yesterday_ratio_pct（volume_ratio 仅首轮残留偶发，
        # 误用会让强势候选永远 0 命中 —— 2026-09-02 实查修复，防回潮）
        check("竞价量比昨口径用 yesterday_ratio 链路（vrMult），不再读常年缺失的 volume_ratio",
              "function vrMult" in auc_html and "auction_yesterday_ratio_pct" in auc_html
              and "x.auction_volume_ratio" not in auc_html and "r.auction_volume_ratio" not in auc_html)
        # 板块竞价强度（2026-09-04 事故防回退）：采集器盘中死亡 → sector.json 停旧日期 →
        # 前端回退观察池聚合，绝不能冒充「整个板块的强度」——标题显性降级 + 恢复指引；
        # server 端自愈补抓（交易日 fail-closed + 节流 + 采集锁防重入）兜底恢复全成分口径
        check("板块竞价强度回退口径显性标注（观察池偏样本 ≠ 全板块）+ 恢复指引在",
              "板块竞价强度 · 观察池偏样本" in auc_html and "非全板块口径" in auc_html)
        check("server sector 过期自愈补抓（交易日 fail-closed + 节流 + 采集锁防重入）",
              "_maybe_auto_sector_backfill" in _sp and "is_trade_today" in _sp
              and "respond=False" in _sp and "09:26" in _sp)
        try:
            code = urllib.request.urlopen(base + "/quant", timeout=5).status
        except Exception as e:
            code = str(e)
        check("/quant 路由 200", code == 200, str(code))

        # ---- 实时竞价板块（只读 data/auction/，采集器独立进程）----
        try:
            code = urllib.request.urlopen(base + "/auction", timeout=5).status
        except Exception as e:
            code = str(e)
        check("/auction 路由 200", code == 200, str(code))
        st, auc = get(base, "/api/auction")
        check("/api/auction 200 且结构齐", st == 200 and all(k in (auc or {}) for k in
              ("live", "final", "benchmark", "status", "watchlist", "watchlist_text",
               "watchmap", "config", "fetching", "alerts")),
              str(list((auc or {}).keys()))[:100])
        _al = (auc or {}).get("alerts") or {}
        check("/api/auction alerts 全天回算结构（date+items，条目字段齐）",
              isinstance(_al.get("items"), list) and
              all(all(k in a for k in ("t", "code", "name", "msg")) for a in _al["items"]),
              str(_al)[:80])
        _pe = (auc or {}).get("pool_exec") or {}
        check("/api/auction pool_exec 执行卡结构（rows/counts/note 或诚实降级）",
              isinstance(_pe.get("rows"), list) and "note" in _pe
              and (not _pe.get("ok") or all(all(k in r for k in
                   ("code", "name", "grp", "window", "verdict", "kind")) for r in _pe["rows"])),
              str(_pe)[:100])
        st, ser = get(base, "/api/auction/series")
        check("/api/auction/series 结构", st == 200 and "date" in ser and "rounds" in ser,
              str(ser)[:60])
        st, ast = get(base, "/api/auction/status")
        check("/api/auction/status 轻量结构", st == 200 and all(k in ast for k in
              ("status", "watchlist", "fetching", "config")), str(list((ast or {}).keys())))
        st, hth = get(base, "/api/health")
        check("/api/health 含 auction 段", st == 200 and "auction" in (hth or {}),
              str(list((hth or {}).keys()))[:100])
        ap = open(os.path.join(ROOT, "web", "auction", "index.html"), encoding="utf-8").read()
        aids = re.findall(r'\bid="([^"]+)"', ap)
        adup = {i for i in aids if aids.count(i) > 1}
        check("auction 页 id 唯一", not adup, str(adup))
        check("auction 页核心面板在", all(x in ap for x in
              ('id="phaseBar"', 'id="rankTable"', 'id="distChart"', 'id="sectorChart"',
               'id="curveMask"', 'id="poolMask"', 'id="alertList"')))
        check("auction 页走 API", all(x in ap for x in
              ("/api/auction", "/api/auction/series", "/api/auction/watchlist", "/api/fetch-auction")))
        check("auction 页 10s 轮询+304", "POLL_MS = 10000" in ap and "If-None-Match" in ap)
        check("auction 页数据日诚实标注（隔日/周末/缺终态不当成今日）",
              "function dataFreshness" in ap and "今日竞价未采集" in ap and "非今日 " in ap
              and "今日缺终态" in ap)
        check("auction 页补抓走 status 轮询（非固定解禁）",
              "/api/auction/status" in ap and "function watchAuctionFetch" in ap)
        check("auction 页渲染错误隔离", "function safeRender" in ap and "safeRender(renderFinal" in ap)
        check("auction 页观察池回填不丢注释", "watchlist_text" in ap)
        check("auction 页异动提醒不再纯内存态（后端回算+实时合并去重）",
              "renderAlertList" in ap and "histAlerts" in ap and "d.alerts" in ap)
        # 昨日备选池·执行卡（2026-09-04）：把「纪律执行是收益的一半」工具化
        check("auction 页昨日备选池执行卡",
              'id="pexTable"' in ap and 'id="pexBody"' in ap and "function renderPoolExec(" in ap
              and "safeRender(renderPoolExec)" in ap)
        pex_src = open(os.path.join(ROOT, "backend", "auction", "pool_exec.py"),
                       encoding="utf-8").read()
        check("执行卡后端：冻结执行层单一来源+污染定盘轮门卫+前瞻信号",
              "import execution_layer" in pex_src and "execution_layer.MF_BUY[grp]" in pex_src
              and 't > "09:26:00"' in pex_src                       # 午后补抓 final 是现价快照
              and "AHEAD_OPEN_DOWN_WARN" in pex_src                 # 情绪前瞻阈值（gap 回验）
              and "snapio.list_dates" in pex_src)                   # 上一交易日快照单一入口
        check("执行层常量单一来源 execution_layer.py",
              os.path.isfile(os.path.join(ROOT, "backend", "execution_layer.py"))
              and "execution_layer.MF_BUY" in open(os.path.join(ROOT, "backend", "recap",
                                                                 "speculate.py"),
                                                   encoding="utf-8").read())
        check("auction 页强弱转换起点不锁第一轮（首轮 not_ready 不再把面板转空）",
              "startMin" in ap and "rounds[0].items" not in
              ap.split("function renderFlip")[1].split('$("flipSeg")')[0])
        asrc = open(os.path.join(ROOT, "backend", "auction", "auc_collector.py"), encoding="utf-8").read()
        check("auction 采集器：手动补抓也写 series/终态未就绪标注/跳过也写状态",
              "def append_series" in asrc and "append_series(date8, dict(rnd, label=\"manual\"))" in asrc
              and "provisional" in asrc and "def _skip_status" in asrc)
        # 竞价窗口口径：窗口外的补抓拿的是 09:25 冻结值，当逐轮画就是假曲线
        check("采集器给每轮写 in_window 并保留 round_in_window 回推",
              '"in_window": _in_window(t_end)' in asrc and "def round_in_window" in asrc
              and "def _in_window" in asrc)
        sys.path.insert(0, os.path.join(ROOT, "backend", "auction"))
        import auc_collector as auc_c  # noqa: E402
        check("round_in_window：窗口内/外/显式标记/无 ts",
              auc_c.round_in_window({"ts": "2026-09-01 09:15:00"}) is True
              and auc_c.round_in_window({"ts": "2026-09-01 09:25:10"}) is True
              and auc_c.round_in_window({"ts": "2026-09-01 12:40:46"}) is False
              and auc_c.round_in_window({"ts": "2026-09-01 09:15:00", "in_window": False}) is False
              and auc_c.round_in_window({"label": "manual"}) is False)
        _store = {}
        _jl, _sv = auc_c.C.load_json, auc_c.C.save_json
        auc_c.C.load_json = lambda n: _store.get(n)
        auc_c.C.save_json = lambda n, o: _store.__setitem__(n, o)
        try:
            for _ts, _w in (("09:15:00", True), ("09:15:30", True),
                            ("10:50:08", False), ("10:58:54", False), ("11:07:16", False)):
                auc_c.append_series("20260901", {"ts": "2026-09-01 " + _ts, "in_window": _w, "items": [{"x": 1}],
                                                 "stage": "live", "label": _ts, "count": 2, "codes_total": 2,
                                                 "errors": [], "phase": "call_auction", "data_status": "incomplete"})
            _kept = [(r["ts"][11:], r["in_window"]) for r in _store["series.json"]["rounds"]]
            _rm = _store.get("rounds_meta.json") or {}
        finally:
            auc_c.C.load_json, auc_c.C.save_json = _jl, _sv
        check("补抓快照只留最新一条、逐轮全保留（不堆出平的假曲线）",
              _kept == [("09:15:00", True), ("09:15:30", True), ("11:07:16", False)], str(_kept))
        # 体检面板要画覆盖条：rounds_meta 必须与 series 同序同过滤，且不带 items
        # （否则页面为几个时间戳要去读几百 KB 的 series.json）
        check("rounds_meta 与 series 同序同过滤、不含 items 但带覆盖所需字段",
              _rm.get("date") == "20260901"
              and [r["ts"][11:] for r in _rm.get("rounds") or []] == [t for t, _w in _kept]
              and [r["in_window"] for r in _rm.get("rounds") or []] == [w for _t, w in _kept]
              and all("items" not in r for r in _rm.get("rounds") or [])
              and all({"count", "errors", "stage"} <= set(r) for r in _rm.get("rounds") or []),
              str(_rm)[:200])
        # 观察池来源标注：涨停接力面板靠它区分「上一交易日涨停股」与池内其它
        _wm_store = {}
        _sv4 = auc_c.C.save_json
        auc_c.C.save_json = lambda n, o: _wm_store.__setitem__(n, o)
        try:
            _codes, _poolmeta = auc_c.C.build_watchlist()
        finally:
            auc_c.C.save_json = _sv4
        _wmap = _wm_store.get("watchmap.json") or {}
        _srcs = [str(v.get("s") or "") for v in _wmap.values()]
        check("watchmap 带来源 s（U自选/L涨停池/H热股）且 meta 计数与标注一致",
              bool(_codes) and bool(_srcs) and all(re.fullmatch(r"[ULH]+", s) for s in _srcs)
              and _poolmeta.get("src_limit") == sum(1 for s in _srcs if "L" in s)
              and _poolmeta.get("src_hot") == sum(1 for s in _srcs if "H" in s),
              f"n={len(_codes)} src样={_srcs[:4]} meta={_poolmeta}")
        # 板块竞价强度换全成分口径：一级行业指数开盘缺口；观察池聚合只能做回退。
        _sec_store = {}
        _sv5 = auc_c.C.save_json
        _ht_mod = auc_c.ht

        class _Ht2:
            @staticmethod
            def catalog(tag, cache_dir=None, **k):
                return [{"thscode": "881101.TI", "name": "种植业"},
                        {"thscode": "881102.TI", "name": "游戏"},
                        {"thscode": "884001.TI", "name": "二级不该进来"}]
            @staticmethod
            def index_snapshot_batches(codes, **k):
                assert all(c.startswith("881") for c in codes), codes  # 只拉一级
                return {"881101.TI": {"open_price": 101.0, "prev_price": 100.0, "last_price": 105.0},
                        "881102.TI": {"open_price": 99.0, "prev_price": 100.0}}
        try:
            auc_c.C.save_json = lambda n, o: _sec_store.__setitem__(n, o)
            auc_c.ht = _Ht2
            auc_c.fetch_sector("20260901")
        finally:
            auc_c.C.save_json, auc_c.ht = _sv5, _ht_mod
        _secj = _sec_store.get("sector.json") or {}
        _srows = {r["n"]: r for r in _secj.get("rows") or []}
        check("fetch_sector：只取一级(881)、开盘缺口=(open-prev)/prev、缺 last_price 不报错",
              _secj.get("date") == "20260901" and set(_srows) == {"种植业", "游戏"}
              and _srows["种植业"]["open_pct"] == 1.0 and _srows["游戏"]["open_pct"] == -1.0
              and _srows["种植业"]["last_pct"] == 5.0 and _srows["游戏"]["last_pct"] is None,
              str(_secj)[:180])
        check("sector 抓取挂在两处终态路径（timeline final + 手动 --once）且失败不阻断",
              asrc.count("\n    fetch_sector(date8)") == 2
              and "except Exception" in asrc.split("def _sector_rows")[1].split("def ")[0])
        # 2026-09-02 事故：09:25:10 定盘时刻指数快照 open 未就绪，无重试导致
        # sector.json 整天停在上一交易日。重试逻辑不得回退。
        check("sector 定盘时刻 open 未就绪有退避重试（防静默陈旧）",
              "SECTOR_RETRY_WAITS" in asrc
              and "time.sleep" in asrc.split("def fetch_sector")[1].split("def ")[0])
        check("server payload 暴露 sector 字段", '"sector": auc_config.load_json("sector.json")' in _sp)
        # 消灭「未分类」两层供给：收盘重建 watchmap 回填 + payload 展示层兑底
        _wmf = {"600001.SH": {"i": "", "n": "缺行业", "s": "H"},
                "600002.SH": {"i": "银行", "n": "已有", "s": "L"}}
        _sv_imap = auc_c.C.industry_map
        auc_c.C.industry_map = lambda: {"600001.SH": "半导体", "600003.SH": "软件开发"}
        try:
            _filled = auc_c.C.watchmap_fill_industry(dict(_wmf), ["600001.SH", "600002.SH", "600003.SH", "600099.SH", None])
        finally:
            auc_c.C.industry_map = _sv_imap
        check("展示层行业兑底：空行业回填、已有不覆盖、池外老代码新建、映射也没有的不造假",
              _filled["600001.SH"]["i"] == "半导体" and _filled["600002.SH"]["i"] == "银行"
              and _filled["600003.SH"]["i"] == "软件开发" and _filled["600003.SH"]["s"] == ""
              and "600099.SH" not in _filled, str(_filled)[:150])
        _csrc = open(os.path.join(ROOT, "backend", "auction", "auc_config.py"), encoding="utf-8").read()
        check("build_watchlist 接行业全量映射回填（不再 import auc_industry 避循环，同模块直接调）",
              "imap = industry_map()" in _csrc and "import auc_industry" not in _csrc)
        _isrc = open(os.path.join(ROOT, "backend", "auction", "auc_industry.py"), encoding="utf-8").read()
        check("industry 重建挂在两处终态路径且 maybe_refresh 失败不阻断",
              asrc.count("AUI.maybe_refresh()") == 2
              and "except Exception" in _isrc.split("def maybe_refresh")[1].split("\ndef ")[0])
        check("重建防呆：目录缩水拒盖、映射偏小不落盘（成分接口若改截断不会静默造假）",
              "疑似上游异常，放弃重建" in _isrc and "不落盘" in _isrc)
        # 交易日历口径：上游 date 是 8 位（"20260901"）。早期用 "%Y-%m-%d" 比，
        # 两边永远不相等 → 每个交易日都被判成非交易日直接跳过，采集整条功能默默失效。
        _real_ht = auc_c.ht

        class _Cal:
            def __init__(self, item):
                self._i = item

            def ht(self, *a, **k):
                return {"item": self._i}

        _t8 = time.strftime("%Y%m%d")
        try:
            auc_c.ht = _Cal([{"date": "20260831"}, {"date": _t8}])
            _c1 = auc_c.is_trade_today()
            auc_c.ht = _Cal([{"date": "20260102"}, {"date": "20260105"}])
            _c2 = auc_c.is_trade_today()
            auc_c.ht = _Cal([{"date": _t8[:4] + "-" + _t8[4:6] + "-" + _t8[6:]}])
            _c3 = auc_c.is_trade_today()
            auc_c.ht = _Cal([{"trade_day": _t8}])          # schema 变了：宁可多跑，不可静默跳过
            _c4 = auc_c.is_trade_today()
        finally:
            auc_c.ht = _real_ht
        check("交易日历按 8 位日期比对（不再因格式不同把交易日判成休市）",
              _c1 is True and _c2 is False and _c3 is True and _c4 is None,
              f"今天在内={_c1} 今天不在={_c2} 带横杠={_c3} schema变={_c4}")
        check("补跑不冒充 09:25:10 时点（late_final → label=manual + note）",
              '"manual" if late_final else "09:25:10"' in asrc
              and "dict(rnd, label=fin_label)" in asrc and "_notes" in asrc)
        check("曲线弹窗区分逐轮/补抓快照且按 ts 排序",
              "function roundInWindow" in ap and "var auc = all.filter(roundInWindow)" in ap
              and "all.sort(function" in ap and "竞价定盘（补抓快照）" in ap
              and "补抓 \" + slot" in ap)
        _oc = re.search(r'function openCurve\(code\) \{(.*?)\n\}', ap, re.S)
        # 2026-09-04：fetchSeries 增加 15s 节流（applyPayload/定时器双通道去重），
        # openCurve 改传 force=true 绕过节流——「打开弹窗必取数、不给过期曲线」的语义不变
        check("曲线弹窗不只在无缓存时取数（重开不给出过期曲线）",
              bool(_oc) and "fetchSeries(true)" in _oc.group(1) and "if (!state.series)" not in _oc.group(1),
              str(_oc.group(1).strip().splitlines()[-2:]) if _oc else "无 openCurve")
        check("曲线弹窗按阶段称呼价格（竞价进行中不叫定盘价）",
              'liveAuc ? "匹配价"' in ap and "竞价曲线（进行中）" in ap and "涨幅（暂定）" in ap)
        # 轴标签几何（用户要求：标签保持原形式，只用边框/容器留位解决遮挡）：
        # 旋转 30° 标签竖向要 ~39px，原 bottom:26 会把尾巴裁掉；ECharts 默认又会自动抽稀标签。
        _dist = ap.split("function renderDist")[1].split("/* ============ 板块聚合")[0]
        check("分布图保持原标签形式，靠 grid.bottom + 容器高度腾位（不裁字）",
              'labels.push(bins[i] + "%~" + bins[i + 1] + "%")' in _dist
              and "rotate: 30" in _dist and "bottom: 44" in _dist
              and "#distChart { height: 210px; }" in ap
              and "fmtBin" not in ap and "\\n~" not in _dist)
        # 2026-09-05 双主题：axisLabel 允许追加 color（主题色板），布局参数 margin/interval/hideOverlap 仍禁改
        check("分布图 axisLabel 保持原样（不加 margin/interval/hideOverlap 覆写）",
              "axisLabel: { fontSize: 10, rotate: 30, color: P().text3 }" in _dist
              and all(k not in _dist for k in ("margin:", "interval:", "hideOverlap")),
              _dist[1370:1470].replace("\n", " ⏎ "))
        _sec = ap.split("function renderSector")[1].split("/* ============ 自选卡片")[0]
        check("板块图靠加高容器避免行业名隔行隐藏（不改标签形式）",
              "#sectorChart { height: 246px; }" in ap and "fontSize: 11, interval: 0" in _sec
              and "splitNumber" not in _sec)
        check("板块强度首选全成分指数口径（门槛=页面数据日而非日历日，周末照常展示并标注数据日）、回退必标偏样本",
              "secDate === today8() || secDate === dataFreshness().date8" in ap
              and "全部成分股集合竞价撮合" in ap and '" · 数据日 " + mdOf(secDate)' in ap
              and "偏样本" in ap and 'id="sectorNote"' in ap)
        check("板块强度涨/跌方向可切：榜内按方向过滤（跌榜不混高开行业）、柱长取绝对值零轴与涨榜同侧",
              'id="sectorSideSeg"' in ap and "sgn * r.open_pct > 0" in ap
              and "sgn * r.pct > 0" in ap and "value: Math.abs(r.pct)" in ap
              and "top[p.dataIndex].pct.toFixed(2)" in ap)
        check("auction 面板头可换行+seg 右对齐（页面放大/窄右列时 宽中严 不再被送量化挤出截断）",
              "flex-wrap: wrap; gap: 4px 8px" in ap
              and ".panel .ph > .seg { margin-left: auto; }" in ap)
        check("强弱转换=竞价期间逐轮轨迹口径（起点→终点 Δ），不是盘后现价",
              'id="flipBody"' in ap and "var FLIP_DELTA = 0.5" in ap
              and "Math.abs(fl) < FLIP_DELTA" in ap
              and "safeRender(renderFlip)" in ap
              and "roundInWindow" in ap.split("function renderFlip")[1].split('$("flipSeg")')[0]
              and 'class="body rankScroll" style="padding:0 4px 4px">\n        <table class="tbl one-left" id="flipTable"' in ap)
        check("flip 不吃盘后冻结快照：窗口外轮次不参与；series 由 ETag 轮询驱动",
              "等待今日逐轮竞价数据" in ap and ap.count("fetchSeries(false)") >= 3
              and "现价取" not in ap.split("function renderFlip")[1].split('$("flipSeg")')[0])
        # 强弱转换筛选优化（2026-09-04）：此前只比首末两点且起点落在最不可信的 09:15
        # 可撤单期；终点还可能被午后手动补抓的现价快照污染（当日实测 65/70 只被污染）。
        _fsrc = ap.split("function renderFlip")[1].split('$("flipSeg")')[0]
        check("flip 起点锚定 09:20 不可撤单窗口，回退时备注明示",
              '"09:20:00"' in _fsrc and "startAnchor" in _fsrc and "未覆盖09:20后" in _fsrc)
        check("flip 终点定盘可信门：只信当日 09:26 前抓取的 final 轮（午后补抓=现价快照）",
              '09:26:00' in _fsrc and "fin.date === (state.series || {}).date" in _fsrc
              and "fin.fetched_at" in _fsrc and "（定盘）" in _fsrc)
        check("flip 方向/形态/水平/流动性四重确认（起点方向·全程极值·红绿盘·竞价额下限）",
              "fromSide" in _fsrc and "Math.max.apply(null, tl)" in _fsrc
              and "Math.min.apply(null, tl)" in _fsrc and "FLIP_AMT" in _fsrc
              and "flipNote" in ap)
        check("体检面板已移到右列最下方且无重复",
              ap.count("今日采集体检</h2>") == 1
              and ap.index('id="checkNote"') > ap.index('id="benchList"'))
        # 右栏四面板（强势候选/涨停接力/兑现/采集体检）：全部只读 payload 现成字段。
        # 去重融合（2026-09-04）：涨停接力不再带逐只 TOP 表（与强势候选重复排同一批股票，
        # 改为强势候选行内「接」徽标）；高开兑现独立面板并入 09:25 定盘（兑现里的
        # 「高开只数」行与定盘统计卡重复口径，一并删除）。
        check("候选/接力/兑现/体检结构齐（接力去TOP表·兑现已并入定盘）",
              all(x in ap for x in ("candBody", "candSeg", "relayStats", "realizeStats",
                                   "covBar", "checkStats", "btnCand2Quant", "relay-badge",
                                   "定盘 → 现价兑现"))
              and all("function render" + n in ap for n in ("Cand", "Relay", "Realize", "Check")))
        check("涨停接力 TOP 表已删、高开兑现独立面板已删（防重复榜单回潮）",
              "relayTable" not in ap and "relayBody" not in ap
              and "涨停池高开 TOP" not in ap
              and "高开兑现</h2>" not in ap
              and '["高开只数", hi.length' not in ap)
        # 强势候选筛选优化（2026-09-04）：旧规则承接只看「剩余>0」方向——剩/匹 1%（珠免）
        # 与剩/匹 994%（大众交通）同榜，且纯按高开排序让弱承接股压住强承接股。
        # 现在：承接比门槛 + 竞价额下限 + 强度分排序（高开+量比分+承接分，双封顶）
        check("强势候选三档规则带承接比与竞价额门槛（防「剩余>0即承接」回潮）",
              "imb: 0.05," in ap and "imb: 0.15," in ap and "imb: 0.30," in ap
              and "imbOf(x) >= rule.imb" in ap
              and "(x.auction_amount || 0) < rule.amt" in ap)
        check("强势候选按强度分排序且行内可见承接厚度",
              "function candScore" in ap and "candScore(b) - candScore(a)" in ap
              and "Math.min(vrMult(x) || 0, 10) * 0.5" in ap
              and "Math.min(imbOf(x), 5) * 2" in ap
              and ">承接</th>" in ap and "强度分" in ap)
        check("涨停价开盘「一」徽标按精确涨停价判定（高开近似会误伤 9.58% 非一字股）",
              "function limitPriceOf" in ap and "relay-badge lim" in ap
              and "x.auction_price >= lp - 1e-6" in ap)
        check("量比缺失不静默吞掉（强势候选 note 明示未评数）",
              "nNoVr" in ap and "量比缺" in ap)
        check("四面板已接入渲染管线（safeRender 隔离，单块报错不拖垮全页）",
              all(f"safeRender(render{n});" in ap for n in ("Cand", "Relay", "Realize", "Check")))
        # 竞价热榜：用户要求取消行数下拉、全量渲染，框高锁在 20 行那一档内部滚动
        check("热榜无行数下拉、全量渲染（slice 已删）",
              "rankLimit" not in ap and "items.slice(0, n)" not in ap
              and "共 \" + items.length" in ap)
        check("热榜框高锁 20 行档（实测行高25px×20+表头24px=524px）且表头吸顶",
              ".rankScroll { max-height: 524px; overflow-y: auto" in ap
              and "position: sticky; top: 0" in ap and "background: var(--bg-2)" in ap)
        check("热榜名称列后挂行业（watchmap 现成字段，缺标注未分类）",
              "esc((wm[r.thscode] || {}).i" in ap and "<th>名称 / 行业</th>" in ap)
        check("体检面板新增行业覆盖行且观察池只数带来源条目（兑底临时条目不充池子）",
              '行业覆盖' in ap and "nPool" in ap and "indHit + \" / \" + itemsC.length" in ap)
        check("逐轮覆盖一轮只顶一格（+step；写成 +step*2 会把真漏采盖掉）",
              "slot.sec + step;" in ap and "slot.sec + step * 2" not in ap)
        check("体检面板显示任务 note 且隔日时序不计入今日覆盖",
              "st.note" in ap and "不计入覆盖" in ap and "rm.date === today8()" in ap)
        check("候选三档阈值可切且兑现面板竞价中拒绝计算",
              "CAND_RULES" in ap and "09:30 后可算" in ap and 'indexOf("L")' in ap)
        check("auction 页引用共享 tokens/appbar", "/lib/tokens.css" in ap and "appbar.js" in ap)
        check("auction 页送入量化联动", "A_QUANT_POOL" in ap and "goQuant" in ap)
        check("auction 采集器脚本存在", os.path.isfile(os.path.join(ROOT, "backend",
              "auction", "auc_collector.py")) and os.path.isfile(os.path.join(ROOT, "backend",
              "auction", "auction_task.bat")))

        # 观察池写入契约：归一化 + 不静默丢错代码 + 不冲掉注释（跑完按原文恢复，不弄脏用户自选）
        sys.path.insert(0, os.path.join(ROOT, "backend", "auction"))
        import auc_config as auc_c  # noqa: E402
        check("代码映射覆盖北交所/B股/ETF（不会把 920xxx 归为沪B而丢出池）",
              all(auc_c.to_thscode(a) == b for a, b in (("600519", "600519.SH"), ("300750", "300750.SZ"),
                  ("920819", "920819.BJ"), ("830799", "830799.BJ"), ("430047", "430047.BJ"),
                  ("900901", "900901.SH"), ("200011", "200011.SZ"), ("510300", "510300.SH"),
                  ("159915", "159915.SZ"), ("sh600000", "600000.SH"), ("6005", None), ("abc", None))))
        wl_path = os.path.join(ROOT, "data", "auction", "watchlist.txt")
        wl_raw0 = open(wl_path, encoding="utf-8").read() if os.path.isfile(wl_path) else ""
        try:
            st, w = post(base, "/api/auction/watchlist",
                         {"text": "# 测试注释\n600519 茅台 920819\n"})
            check("观察池 POST 返回 codes/invalid", st == 200 and w.get("codes") == ["600519.SH", "920819.BJ"]
                  and w.get("invalid") == ["茅台"] and w.get("invalid_total") == 1, str(w)[:140])
            check("观察池保存不丢注释行",
                  "# 测试注释" in open(wl_path, encoding="utf-8").read())
        finally:
            post(base, "/api/auction/watchlist", {"text": wl_raw0})
        check("观察池已恢复原文",
              [x for x in open(wl_path, encoding="utf-8").read().splitlines() if not x.startswith("#")] ==
              [x for x in wl_raw0.splitlines() if not x.startswith("#")])

        # ---- 量化工作台（引擎内置）----
        st, qt_meta = get(base, "/api/quant/meta")
        check("/api/quant/meta 200 且结构齐", st == 200 and all(k in qt_meta for k in
              ("families", "rule", "screener", "local_symbols", "execution_opts", "data_freshness")), str(qt_meta)[:80])
        check("meta 数据新鲜度（latest+只数）", isinstance(qt_meta.get("data_freshness", {}).get("symbols"), int)
              and (qt_meta.get("data_freshness") or {}).get("latest") is not None or not (qt_meta.get("data_freshness") or {}).get("n"),
              str(qt_meta.get("data_freshness"))[:80])
        if st == 200 and "families" in qt_meta:
            check("meta 内置策略族 ≥3", len(qt_meta["families"]) >= 3)
            check("meta 规则 DSL 齐（fields/ops/indicators）", all(k in qt_meta["rule"] for k in
                  ("fields", "ops", "indicators")))
            check("meta 选股配方模板序列化正确（bools 是列表不是字符串）",
                  all(isinstance(t.get("bools"), list) for t in qt_meta["screener"]["templates"].values()),
                  str(list(qt_meta["screener"]["templates"].values())[:1])[:120])
        # 规则代码预览（引擎同源导出）
        st, pv = post(base, "/api/quant/rule/preview", {"spec": {
            "symbols": ["510300"], "entry": {"mode": "all", "conds": [
                {"left": {"t": "field", "f": "close"}, "op": "gt",
                 "right": {"t": "ind", "i": "sma", "p": {"n": 20}, "c": ""}}]},
            "exit": {"mode": "any", "conds": []}, "target_weight": 0.9}})
        check("rule/preview 同源代码导出", st == 200 and "class" in (pv or {}).get("code", "")
              and not (pv or {}).get("problems"), str(pv)[:120])
        # 策略库写入→读回→删除（含存档 payload 一致性）
        st, sv = post(base, "/api/quant/strategies", {"name": "smoke-quant-原生", "kind": "rule",
                       "payload": {"symbols": ["510300"], "target_weight": 0.9},
                       "note": "冒烟", "data_symbols": ["510300"]})
        check("quant 存档 POST", st == 200 and (sv or {}).get("status") == "saved", str(sv)[:80])
        st, ls = get(base, "/api/quant/strategies")
        hit = [x for x in (ls or {}).get("strategies", []) if x["name"] == "smoke-quant-原生"]
        check("quant 存档读回含 payload", bool(hit) and hit[0]["payload"].get("target_weight") == 0.9, str(hit)[:80])
        st, gv = post(base, "/api/quant/strategies/get", {"name": "smoke-quant-原生"})
        check("quant 存档 get", st == 200 and (gv or {}).get("kind") == "rule", str(gv)[:60])
        st, dl = post(base, "/api/quant/strategies/delete", {"name": "smoke-quant-原生"})
        check("quant 存档 delete", st == 200 and (dl or {}).get("status") == "deleted", str(dl)[:60])
        # 同步回测（有本地缓存才跑，否则降级跳过）
        daily510 = os.path.join(ROOT, "quant", "data", "cn_a", "daily", "510300.parquet")
        if os.path.isfile(daily510):
            st, bt = post(base, "/api/quant/backtest", {
                "strategy": {"kind": "builtin", "family": "dual_ma", "params": {"fast": 20, "slow": 60}},
                "codes": ["510300"], "benchmark": "000300", "start": "2024-01-01", "end": "2025-06-30",
                "cash": 1e6, "data_mode": "local", "log": False})
            check("quant 同步回测 200+曲线+指标", st == 200 and "metrics" in (bt or {})
                  and len((bt or {}).get("curve", {}).get("dates") or []) > 100
                  and bt["curve"]["drawdown"] and "累计收益率" in bt["metrics"], str(bt)[:100])
            check("回测含基准超额指标+结构化风控事件", st == 200
                  and "年化超额收益" in bt.get("metrics", {})
                  and all(isinstance(e, dict) for e in bt.get("risk_events", [])), str(bt.get("metrics", {}).get("年化超额收益")))
            # 对比端点：2 项成功 / 1 项拒绝
            st2, cmp2 = post(base, "/api/quant/compare", {"items": [
                {"label": "A", "req": {"strategy": {"kind": "builtin", "family": "dual_ma", "params": {"fast": 10, "slow": 60}},
                 "codes": ["510300"], "data_mode": "local", "start": "2024-01-01", "end": "2025-06-30"}},
                {"label": "B", "req": {"strategy": {"kind": "builtin", "family": "dual_ma", "params": {"fast": 20, "slow": 60}},
                 "codes": ["510300"], "data_mode": "local", "start": "2024-01-01", "end": "2025-06-30"}}]})
            check("compare 双项返回归一净值", st2 == 200 and len((cmp2 or {}).get("series", [])) == 2
                  and all(len(x["dates"]) <= 241 for x in cmp2["series"]), str(cmp2)[:90])
            try:
                code1 = post(base, "/api/quant/compare", {"items": [{"label": "x", "req": {}}]})[0]
            except urllib.error.HTTPError as e:
                code1 = e.code
            check("compare 单项拒绝 400", code1 == 400, str(code1))
        else:
            check("quant 本地行情缺失（降级跳过回测）", True)
        # 后台任务非法 kind → 400；非法 id → 400
        try:
            code = post(base, "/api/quant/job", {"kind": "nope", "params": {}})[0]
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            code = str(e)
        check("job 非法 kind 拒绝", code == 400, str(code))
        try:
            code = urllib.request.urlopen(base + "/api/quant/job/..%2F..", timeout=10).status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            code = str(e)
        check("job id 白名单拒绝穿越", code in (400, 404), str(code))
        # 任务历史 + wf/refresh 类型接线 + 报告直链安全
        st, jb = get(base, "/api/quant/jobs")
        check("/api/quant/jobs 结构", st == 200 and isinstance(jb.get("jobs"), list), str(jb)[:80])
        qapi = open(os.path.join(ROOT, "backend", "quant", "quant_api.py"), encoding="utf-8").read()
        rjob = open(os.path.join(ROOT, "backend", "quant", "run_job.py"), encoding="utf-8").read()
        check("job 类型白名单含 wf/refresh", '"wf"' in qapi and '"refresh"' in qapi
              and "run_wf" in rjob and "run_refresh" in rjob)
        try:
            rlen = urllib.request.urlopen(base + "/quant-results/dual_ma_report.html", timeout=10).status
        except Exception as e:
            rlen = str(e)
        check("报告直链 200", rlen == 200, str(rlen))
        try:
            code2 = urllib.request.urlopen(base + "/quant-results/..%2Fserver.py", timeout=10).status
        except urllib.error.HTTPError as e:
            code2 = e.code
        except Exception as e:
            code2 = str(e)
        check("报告直链拒绝穿越", code2 in (400, 404), str(code2))
        # 原生工作台结构：六页签 + 关键面板
        check("quant 工作台六页签", all(f'id="tabBtn{c}"' in qp for c in
              ("Ov", "Build", "Store", "Screen", "Signal", "Grid")))
        check("quant 工作台核心控件在", 'id="buildForm"' in qp and 'id="storeCards"' in qp
              and 'id="screenTable"' in qp and 'id="gridTable"' in qp and 'id="mdOut"' in qp
              and 'id="eqChart"' in qp)
        check("quant 新能力面板在（对比/热力/WF/新鲜度）", all(x in qp for x in
              ('id="cmpPanel"', 'id="cmpChart"', 'id="heatWrap"', 'id="gHeat"', 'id="wfPanel"',
               'id="wfChart"', 'id="freshState"', 'id="btnRefreshData"')))
        check("quant 工作台走引擎 API", all(x in qp for x in
              ("/api/quant/backtest", "/api/quant/meta", "/api/quant/strategies",
               "/api/quant/job", "/api/quant/rule/preview", "/api/quant/compare", "/api/quant/jobs")))
        check("复盘→量化联动（A_QUANT_POOL 管道）", "A_QUANT_POOL" in qp and "goQuant" in rp
              and "btnZt2Quant" in rp)

        # quote 代理：不强依赖网络（允许降级 error，但必须 JSON 且非 500 崩溃）
        try:
            with urllib.request.urlopen(base + "/api/quote?codes=600519", timeout=20) as r:
                q = json.loads(r.read().decode("utf-8"))
            check("/api/quote 结构", "list" in q or "error" in q, str(q)[:80])
        except Exception as e:
            check("/api/quote 结构", False, str(e)[:80])

    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()

    print()
    if FAILED:
        print(f"SMOKE FAILED: {len(FAILED)} 项")
        for f in FAILED:
            print("  -", f)
        sys.exit(1)
    print("SMOKE ALL PASS")


if __name__ == "__main__":
    main()
