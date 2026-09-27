# -*- coding: utf-8 -*-
"""Handler 轮动域方法（server.py 拆分 · 方案 O-3b，2026-09-27）。

动机：单文件 1660 行改一处要载入全部上下文。方法体自 server.py 原样搬移
（零行为变化）；共享状态一律 from server_context import ...，绝不 import server。
覆盖路由：/api/dates、/api/day、/api/boards、/api/quote、/api/industry-map、
/api/rotation-stats、/api/rotation-matrix、/api/board-members、
POST /api/fetch-rotation，以及轮动断流自愈 _maybe_auto_rotation_revive。
"""
import os
import time

from server_context import (ROOT, ROT_DAILY, ROT_BOARDS, ROT_FETCH_SCRIPT,
                            ROT_FETCH_CWD, LOCK_ROT, LOG_DIR, _DATE_RE,
                            _auto_rot_ts, rot_dates, rotation_stall,
                            rotation_stats_payload, rotation_matrix_payload,
                            _cached_json_bytes, industry_map_payload,
                            auc_config, quote_service, board_members, lockutil)


class HandlerRotation:
    # ---------------- GET handlers：轮动 ----------------

    def api_dates(self):
        return self._json({"dates": rot_dates()})

    def api_board_members(self):
        name = self._query().get("name", [""])[0].strip()
        if not name or len(name) > 40:
            return self._json({"error": "板块名非法"}, 400)
        return self._json(board_members.payload(ROOT, name))

    def api_rotation_stats(self):
        self._maybe_auto_rotation_revive()   # 盘中断流自愈（闸门/节流见该函数）
        return self._json(rotation_stats_payload())

    def api_rotation_matrix(self):
        self._maybe_auto_rotation_revive()
        return self._json(rotation_matrix_payload())

    def api_day(self):
        dates = rot_dates()
        if not dates:
            return self._json({"error": "暂无轮动数据，盘中请先运行 backend/rotation/ths_collect.py"}, 404)
        date = self._query().get("date", [dates[-1]])[0]
        if not _DATE_RE.fullmatch(date):   # 白名单，防路径穿越
            return self._json({"error": "日期格式非法"}, 400)
        # ETag/304 + mtime 缓存：分时文件未变时零读盘零解析（bytes 层缓存见 _cached_json_bytes），
        # 盘中 15s 轮询与复盘页 5 连拉在数据未更新时都是 304 空回
        cached = _cached_json_bytes(os.path.join(ROT_DAILY, f"{date}.json"))
        if cached is None:
            return self._json({"error": f"没有 {date} 的数据"}, 404)
        return self._send_etag_body(cached[0], cached[1], cached[2])

    def api_boards(self):
        boards = self._read_json(ROT_BOARDS)
        if boards is None:
            return self._json({"error": "暂无板块清单"}, 404)
        return self._json(boards)

    def api_quote(self):
        # 个股实时行情代理（同花顺 hithink 快照 + 单代码 5s TTL 服务端缓存），
        # 支持 600519 / 000001 / sh600519 / 北交所8xxxxx
        raw = self._query().get("codes", [""])[0]
        codes = []
        for c in raw.split(","):
            c = c.strip()
            if c and auc_config.to_thscode(c):
                codes.append(c)
        codes = list(dict.fromkeys(codes))  # 去重，保持顺序
        if not codes:
            return self._json({"error": "代码格式非法"}, 400)
        if len(codes) > 500:
            return self._json({"error": "最多 500 只"}, 400)
        return self._json(quote_service.quote_payload(codes, ROOT))

    def api_industry_map(self):
        # 个股→同花顺一级行业（全站单一来源；复盘页行业列/focus 链接用）
        return self._etag_json(industry_map_payload())

    # ---------------- 自愈与 POST：轮动抓取 ----------------

    def _maybe_auto_rotation_revive(self):
        """轮动断流自愈（2026-09-05）：盘中采集进程死亡会让当段分钟永久缺失
        （同花顺口径没有分钟级指数接口，不可回补），rotation_stall() 只报警不补救。
        页面轮询 /api/rotation-* 时顺手检查：stalled 且采集锁心跳也断 >6 分钟
        （活循环每轮 utime 心跳，断 6 分钟=持有者必死）→ 摘死锁、后台拉起常驻
        循环（循环模式自动补采至 15:00 收盘定格后自退出）。闸门按成本从低到高：
        10 分钟节流 → 交易时段 09:36–11:30 与 13:05–14:55（午休 11:30–13:00 心跳
        本来就停，摘锁会误伤活进程；13:05 后留 5 分钟给恢复采样的头几个点位；
        之后尾部由定格任务+定格并入兜底）→
        stall 确认 → 锁心跳确认死 → 交易日历 fail-closed（子进程最贵，日历不可用
        宁可跳过——绝不在非交易日拉起采集）。机器关机与上游长时间故障仍属物理缺口。"""
        now = time.time()
        if now - _auto_rot_ts[0] < 600:
            return
        hm = time.strftime("%H:%M")
        if not ("09:36" <= hm <= "11:30" or "13:05" <= hm <= "14:55"):
            return
        st = rotation_stall()
        if not (st and st.get("stalled")):
            return
        try:
            lock_age = time.time() - os.path.getmtime(LOCK_ROT)
        except OSError:
            lock_age = None
        if lock_age is not None and lock_age < 360:
            return   # 锁心跳 <6 分钟：持有者大概率活着（可能刚被别的路径拉起），不动
        _auto_rot_ts[0] = now   # 无论后续成败，10 分钟内不再试探（含日历子进程的成本闸）
        try:
            import trade_cal  # noqa: PLC0415  交易日历判定单一来源（backend/trade_cal.py）
            if trade_cal.is_trade_today() is not True:
                return   # False=非交易日 / None=日历不可用：一律 fail-closed
        except Exception:
            return
        lockutil.release(LOCK_ROT, force=True)   # 摘死锁（活持有者不可能出现：心跳断 6 分钟必死）
        self._spawn_fetch(
            ROT_FETCH_SCRIPT, ROT_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-rotation.log"), LOCK_ROT,
            "rotation", "rotation", extra=None, respond=False)

    def post_fetch_rotation(self):
        done = self._spawn_fetch(
            ROT_FETCH_SCRIPT, ROT_FETCH_CWD,
            os.path.join(LOG_DIR, "fetch-rotation.log"), LOCK_ROT,
            "rotation", "rotation", extra=["--once"])
        return self._fetch_started() if not done else None
