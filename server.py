# -*- coding: utf-8 -*-
"""
A股看板 · 统一服务（竞价 + 轮动 + 复盘 + 全球 + 量化 五板块，单端口）

用法:
    python server.py            # 默认 127.0.0.1:8000（可用环境变量 AK_PORT 改）
    python server.py --port 8080

结构（2026-09-27 方案 O-3b 拆分）：本文件只保留 GET/POST 路由表（唯一真相）、
do_GET/do_POST 分发、Handler 组合（各域方法在 server_handlers_*.py 的 mixin，
公共 HTTP 管线在 server_handlers_core.py）、health 巡检线程与 main()；
常量/锁/进程内缓存与独立 helper（health_payload/auction_payload 等）在
server_context.py——依赖方向 server.py → server_handlers_* → server_context，
绝不反向 import。

路由（板块排列顺序与顶栏导航一致：竞价 → 轮动 → 复盘 → 全球 → 量化）:
    静态:
        /auction, /auction/   实时竞价（web/auction/index.html）
        /                     日内轮动仪表盘（web/index.html，根路径入口不变）
        /recap, /recap/       盘后复盘看板（web/recap/index.html，同源内嵌）
        /global, /global/     全球总览（web/global/index.html）
        /quant, /quant/       量化平台工作台（web/quant/index.html，内置 quant/ 引擎）
        /lib/...              共享 JS/CSS/地图（ETag + 304；echarts/world.json 长缓存）
    实时竞价 API（只读 data/auction/ 采集产出；异动回算在 backend/auction/auc_alerts.py）:
        /api/auction          竞价面板（live/final/benchmark/状态/观察池/rounds_meta/时间线配置，ETag/304）
        /api/auction/series   当日全部轮次时序（单股竞价曲线）
        /api/auction/status   采集状态与观察池（轻量，页面补抓期间轮询用）
        POST /api/auction/watchlist     更新自选观察池（归一化为完整 thscode）
        POST /api/fetch-auction         后台补抓（文件锁防重入，日志 .status/logs/）
    轮动数据 API:
        /api/dates            轮动日期列表（YYYY-MM-DD）
        /api/day?date=        当日板块分时（同花顺一级行业 90 个，盘中逐分钟快照轮询累积；白名单校验，防路径穿越）
        /api/boards           板块清单（同花顺一级行业，src=ths，与竞价页/复盘页同一口径）
        /api/rotation-stats   轮动统计（读 data/rotation/panel/stats.json，派生层产出）
        /api/rotation-matrix  板块强度矩阵 + 日内形态（读 panel/matrix.json）
    复盘数据 API:
        /api/recap/dates      复盘快照日期（兼容 .json / .json.gz）
        /api/recap/modules    模块契约 registry（单一来源 backend/recap/modules.py）
        /data/YYYYMMDD.json   复盘快照（白名单：仅 8 位数字）
        GET/POST /api/recap/note?date=YYYYMMDD  复盘笔记（存 data/recap/notes/，localStorage 仅兜底）
         /api/sentiment       情绪指数序列（recap/panel/sentiment.csv 派生层产出）
         /api/board-members   板块近5日走势+池内个股（聚合逻辑在 backend/board_members.py）
         /api/industry-map    个股→同花顺一级行业映射（data/auction/industry_map.json 单一来源，全站共用）
    全球总览 API:
        /api/global           全球总览数据（读 data/global/global.json，fetch_global.py 产出）
        POST /api/fetch-global 后台重抓全球数据（文件锁防重入）
    量化平台 API（引擎已内置于 quant/，后端接线见 backend/quant/quant_api.py）:
        /api/quant            总览面板（collector 聚合回测流水/报告/研究/策略库，15s 缓存）
        /api/quant/meta       前端表单单一事实源（策略族参数/规则 DSL/选股条件/本地标的清单）
        POST /api/quant/backtest   同步回测（内置族/规则 spec，返回指标+净值曲线+成交表）
        GET/POST /api/quant/strategies[|/get|/delete]  策略库读写
        POST /api/quant/job   提交长任务 screener|signals|grid；GET /api/quant/job/<id> 轮询
    运维:
        /api/health           数据管家（新鲜度/失败明细/锁状态）
        POST /api/fetch / /api/fetch-rotation   后台抓取（文件锁防重入，日志 .status/logs/）

写接口安全（2026-09-04 加固，原为局域网零鉴权）:
    - 所有 POST 过写接口闸门：浏览器跨站 POST 的 Origin/Referer 与本服务 Host 不同源 → 403
      （拦 CSRF：恶意网页借用户浏览器打内网接口）；看板页面同源访问与手机 Tailscale 访问不受影响。
    - 非本机来源的无 Origin 客户端（curl/脚本）：配置环境变量 AK_WRITE_TOKEN 后必须携带
      X-AK-Token 头（或 ?token=）才能写；未配置时行为同旧版（放行），启动横幅会提示如何开启。

设计边界：
- 全站板块口径统一为同花顺一级行业指数（881xxx，90 个，2026-09-01 起）：轮动采集 backend/rotation/ths_collect.py、竞价板块强度 sector.json、复盘行业模块与钻取、个股行业归属 industry_map.json 全部同一目录同一名单；东财口径仅存归档 daily_legacy_eastmoney/ 与 legacy 脚本，不再接入任何链路与文案。
- 启动落点（先打开哪个板块）由 backend/landing.py 统一判定：工作日 09:10–09:30 落实时竞价，其余落日内轮动；server.py 与 start.py 共用，不在两处各写一套时间判断。
- HTTP 层不做业务计算：情绪指数/轮动统计/强度矩阵全部由 backend/derive.py 产出面板文件；异动回算在 backend/auction/auc_alerts.py、板块钻取在 backend/board_members.py、行情代理与单代码缓存在 backend/quote_service.py（2026-09-04 迁出），本文件只做路由/缓存/序列化。
- 快照读写统一走 backend/recap/snapio（自动兼容 gzip 归档）。
- 静态服务仅暴露 web/ 下的五个页面目录与 lib/（主站 + recap/global/quant/auction）；data 路由白名单校验，杜绝目录浏览与穿越。
"""
import argparse
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, HTTPServer

# 打包态经 launcher/gui 的 runpy 运行：server.py 所在目录（onefile 下是
# %LOCALAPPDATA% 持久目录）未必在 sys.path，先自锚定再 import 拆分模块。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from server_context import (_HEALTH_SCAN_INTERVAL, _hithink_probe,             # noqa: E402
                            _health_alerts, health_payload, notify,
                            rot_dates, recap_dates, auction_brief,
                            landing_describe, landing_path)
from server_handlers_core import HandlerCore          # noqa: E402  公共 HTTP 管线
from server_handlers_rotation import HandlerRotation  # noqa: E402  轮动域
from server_handlers_recap import HandlerRecap        # noqa: E402  复盘域
from server_handlers_auction import HandlerAuction    # noqa: E402  竞价域
from server_handlers_global import HandlerGlobal      # noqa: E402  全球域
from server_handlers_quant import HandlerQuant        # noqa: E402  量化域
from server_handlers_www import HandlerWww            # noqa: E402  页面与静态
from server_handlers_ops import HandlerOps            # noqa: E402  运维（health）

# ---------------- 路由表（2026-09-04：巨型 if 链 → (method, regex) → handler） ----------------

GET_ROUTES = [
    (r"^/api/dates$", "api_dates"),
    (r"^/api/health$", "api_health"),
    (r"^/api/sentiment$", "api_sentiment"),
    (r"^/api/board-members$", "api_board_members"),
    (r"^/api/rotation-stats$", "api_rotation_stats"),
    (r"^/api/rotation-matrix$", "api_rotation_matrix"),
    (r"^/api/day$", "api_day"),
    (r"^/api/boards$", "api_boards"),
    (r"^/api/quote$", "api_quote"),
    (r"^/api/industry-map$", "api_industry_map"),
    (r"^/api/recap/dates$", "api_recap_dates"),
    (r"^/api/recap/modules$", "api_recap_modules"),
    (r"^/api/recap/note$", "api_recap_note"),
    (r"^/data/(?P<name>\d{8}\.json)$", "api_snapshot"),
    (r"^/api/global$", "api_global"),
    (r"^/api/quant$", "api_quant"),
    (r"^/api/quant/meta$", "api_quant_meta"),
    (r"^/api/quant/strategies$", "api_quant_strategies"),
    (r"^/api/quant/jobs$", "api_quant_jobs"),
    (r"^/api/quant/job/(?P<jid>[^/]+)$", "api_quant_job_get"),
    (r"^/api/auction$", "api_auction"),
    (r"^/api/auction/series$", "api_auction_series"),
    (r"^/api/auction/status$", "api_auction_status"),
    (r"^/lib/(?P<rest>.+)$", "api_lib_static"),
    (r"^/quant-results/(?P<sub>.+)$", "api_quant_results"),
    (r"^/recap(?:/|/index\.html)?$", "page_recap"),
    (r"^/recap/app\.js$", "page_recap_app_js"),
    (r"^/global(?:/|/index\.html)?$", "page_global"),
    (r"^/auction(?:/|/index\.html)?$", "page_auction"),
    (r"^/quant(?:/|/index\.html)?$", "page_quant"),
]
GET_ROUTES = [(re.compile(p), fn) for p, fn in GET_ROUTES]

POST_ROUTES = [
    (r"^/api/fetch$", "post_fetch_recap"),
    (r"^/api/fetch-rotation$", "post_fetch_rotation"),
    (r"^/api/fetch-global$", "post_fetch_global"),
    (r"^/api/fetch-auction$", "post_fetch_auction"),
    (r"^/api/quant/backtest$", "post_quant_backtest"),
    (r"^/api/quant/strategies$", "post_quant_strategies_save"),
    (r"^/api/quant/strategies/get$", "post_quant_strategies_get"),
    (r"^/api/quant/strategies/delete$", "post_quant_strategies_delete"),
    (r"^/api/quant/job$", "post_quant_job"),
    (r"^/api/quant/compare$", "post_quant_compare"),
    (r"^/api/quant/rule/preview$", "post_quant_rule_preview"),
    (r"^/api/recap/note$", "post_recap_note"),
    (r"^/api/auction/watchlist$", "post_auction_watchlist"),
]
POST_ROUTES = [(re.compile(p), fn) for p, fn in POST_ROUTES]


class Handler(HandlerCore, HandlerRotation, HandlerRecap, HandlerAuction,
              HandlerGlobal, HandlerQuant, HandlerWww, HandlerOps,
              SimpleHTTPRequestHandler):
    """各域 mixin 组合（方法体零改搬移，见 server_handlers_*.py）；分发逻辑
    do_GET/do_POST 与路由表同在本文件，路由表是唯一真相。MRO：域 mixin →
    HandlerCore（公共管线）→ SimpleHTTPRequestHandler。"""

    # ---------------- 分发 ----------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith(("/api/", "/data/", "/quant-results/")) \
                and self._read_gate_ok() is None:
            return
        for pat, fn in GET_ROUTES:
            m = pat.fullmatch(path)
            if m:
                return getattr(self, fn)(**m.groupdict())
        if path.startswith("/api/") or path.startswith(("/data/", "/quant-results/")):
            # API 族未知路径统一结构化 404（与 POST 路由同口径），前端 fetch 拿到可解析的错误体
            return self._json({"error": "not found"}, 404)
        # 其余静态文件从主 web/ 提供
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        for pat, fn in POST_ROUTES:
            m = pat.fullmatch(path)
            if m:
                if self._write_gate_ok() is None:
                    return
                return getattr(self, fn)(**m.groupdict())
        return self._json({"error": "not found"}, 404)


class ThreadingServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _health_monitor_loop():
    """守护线程：首轮只建基线不推送（避免重启后旧告警重放轰炸），之后每轮
    把「当轮存在的告警 key」推给 notify（notify 侧还有 6h key 节流兜底）。"""
    first = True
    while True:
        time.sleep(_HEALTH_SCAN_INTERVAL)
        try:
            _hithink_probe()   # 探活按巡检节奏刷新（S-4）；health_payload 只读缓存
            alerts = _health_alerts(health_payload())
            if not first:
                for key, msg in alerts:
                    r = notify.send(title="看板数据链告警",
                                    text=msg + f"\n\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                                    key="health:" + key)
                    if r.get("sent"):
                        print(f"[health-monitor] 已推送 {key}", flush=True)
            first = False
        except Exception as e:  # noqa: BLE001 - 监控线程自身绝不退出
            sys.stderr.write(f"[health-monitor] {type(e).__name__}: {e}\n")


def main():
    ap = argparse.ArgumentParser(description="A股看板统一服务（竞价/轮动/复盘/全球/量化）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("AK_PORT") or 8000))
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("!! 绑定了非本机地址：接口将暴露给局域网。写接口已有跨源防护（Origin/Referer）,")
        print("!! 但局域网内的脚本直连仍无鉴权；如需收紧，设置环境变量 AK_WRITE_TOKEN=随机串，")
        print("!! 之后非本机写请求必须携带 X-AK-Token 头。")
        print("!! 若经内网穿透等远程暴露：建议同时设置 AK_READ_TOKEN=随机串，")
        print("!! 届时非本机访问 /api、/data 全部数据接口都须携带 X-AK-Token 头或 ?token=。")

    # health 巡检推送线程（0914 事故复盘：「打开看板才知道」→「手机先知道」）
    threading.Thread(target=_health_monitor_loop, daemon=True,
                     name="health-monitor").start()

    rd, cd = rot_dates(), recap_dates()
    landing = landing_path()
    print("=" * 56)
    print("A股看板 · 统一服务（竞价 + 轮动 + 复盘 + 全球 + 量化）")
    print(f"  竞价数据: {auction_brief()}")
    print(f"  轮动数据: {', '.join(rd[-3:]) if rd else '（无，先运行 backend/rotation/ths_collect.py）'}")
    print(f"  复盘数据: {', '.join(cd[:3]) if cd else '（无，先运行 backend/recap/fetch_daily.py）'}")
    print(f"  访问: http://{args.host}:{args.port}{landing}   （竞价: /auction ｜ 轮动: / ｜ 复盘: /recap ｜ 全球: /global ｜ 量化: /quant）")
    print(f"  落点: {landing_describe()}")
    print("  Ctrl+C 退出")
    print("=" * 56)
    with ThreadingServer((args.host, args.port), Handler) as httpd:
        if not args.no_open:
            import webbrowser
            webbrowser.open(f"http://{args.host}:{args.port}{landing}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")


if __name__ == "__main__":
    main()
