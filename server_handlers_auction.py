# -*- coding: utf-8 -*-
"""Handler 竞价域方法（server.py 拆分 · 方案 O-3b，2026-09-27）。

动机：单文件 1660 行改一处要载入全部上下文。方法体自 server.py 原样搬移
（零行为变化）；共享状态一律 from server_context import ...，绝不 import server。
覆盖路由：/api/auction、/api/auction/series、/api/auction/status、
POST /api/auction/watchlist、POST /api/fetch-auction，
以及 sector 停在旧日期时的自愈补抓 _maybe_auto_sector_backfill。
"""
import os
import time

from server_context import (AUCTION_FETCH_SCRIPT, AUCTION_FETCH_CWD, LOG_DIR,
                            _auto_sector_ts, load_status, auction_payload,
                            auction_series_payload, auc_config, lockutil)


class HandlerAuction:
    # ---------------- GET handlers：竞价 ----------------

    def api_auction(self):
        self._maybe_auto_sector_backfill()   # sector 停在旧日期时的自愈补抓（闸门/节流见该函数）
        return self._etag_json(auction_payload())

    def api_auction_series(self):
        return self._etag_json(auction_series_payload())

    def api_auction_status(self):
        # 真正的轻量：不读 live/final 整包（108~300 只 items 可达几百 KB），
        # 页面补抓期间每 5s 轮询一次，走这里而不是 /api/auction。
        st = load_status("auction.json")
        return self._json({"status": st, "watchlist": auc_config.read_watchlist(),
                           "fetching": lockutil.held_info(auc_config.LOCK_PATH) is not None,
                           "rounds": len((auc_config.load_json("rounds_meta.json") or {}).get("rounds") or []),
                           "config": {"interval": auc_config.INTERVAL, "batch": auc_config.BATCH,
                                      "max_codes": auc_config.MAX_CODES, "hot_top": auc_config.HOT_TOP}})

    # ---------------- 自愈与 POST：竞价抓取 / 观察池 ----------------

    def _maybe_auto_sector_backfill(self):
        """板块竞价强度自愈（2026-09-04 事故）：竞价采集器盘中静默死亡会同时丢掉
        09:25:10 终态与挂在终态路径上的 sector 抓取 → sector.json 停在昨日 →
        前端退回「观察池行业聚合」偏样本口径冒充板块强度（当日实测：09:19:30 死亡，
        用户看到的「板块竞价强度」其实只是 70 只涨停池+热股的均值）。
        页面每 10s 轮询 /api/auction 时顺手检查：sector 非今日则后台补跑采集器
        --once（冻结终态 + 90 行业开盘缺口；open/prev 是日K属性，盘后补抓仍是今日真实值）。
        闸门按成本从低到高：内存 10 分钟节流 → 时间窗 09:26–23:00 → sector 已是今日 →
        采集锁空闲 → hithink 交易日历（子进程最贵，且必须 fail-closed：日历不可用宁可
        跳过——绝不能在非交易日把昨日冻结值写成今日 final）。"""
        now = time.time()
        if now - _auto_sector_ts[0] < 600:
            return
        hm = time.strftime("%H:%M")
        if not ("09:26" <= hm <= "23:00"):
            return
        try:
            sec = auc_config.load_json("sector.json") or {}
        except Exception:
            return
        if sec.get("date") == time.strftime("%Y%m%d"):
            return
        if lockutil.held_info(auc_config.LOCK_PATH):
            return   # 采集在跑（live 轮询或上一次补抓），不打断
        _auto_sector_ts[0] = now   # 无论后续成败，10 分钟内不再试探（含日历子进程的成本闸）
        try:
            import trade_cal  # noqa: PLC0415  交易日历判定单一来源（backend/trade_cal.py）
            if trade_cal.is_trade_today() is not True:
                return   # False=非交易日 / None=日历不可用：一律 fail-closed
        except Exception:
            return
        self._spawn_fetch(
            AUCTION_FETCH_SCRIPT, AUCTION_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-auction.log"), auc_config.LOCK_PATH,
            "auction", "auction", extra=["--once"], respond=False)

    def post_fetch_auction(self):
        done = self._spawn_fetch(
            AUCTION_FETCH_SCRIPT, AUCTION_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-auction.log"), auc_config.LOCK_PATH,
            "auction", "auction", extra=["--once"])
        return self._fetch_started() if not done else None

    def post_auction_watchlist(self):
        req = self._body() or {}
        text = req.get("text")
        if not isinstance(text, str) or len(text) > 20000:
            return self._json({"error": "text 需为字符串（≤20000 字符）"}, 400)
        _, invalid = auc_config.parse_watchlist(text)   # 先解析出被丢弃的 token，别静默少股
        codes = auc_config.write_watchlist(text)
        return self._json({"status": "saved", "codes": codes, "total": len(codes),
                           "invalid": invalid[:20], "invalid_total": len(invalid),
                           "note": "下次采集自动生效；盘中可点「立即补抓」尽快落地"})
