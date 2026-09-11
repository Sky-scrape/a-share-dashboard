# -*- coding: utf-8 -*-
"""投机分析计算引擎 —— 盘后复盘新板块（2026-09-02）。

把用户的投机方法论文档（文案.doc，摘要见 docs/speculation-notes.md）
映射为可计算数据，输出快照模块 `speculation`：

  themes    题材结构·核心识别：涨停池按涨停原因首段聚类；核心 = 连板最高
            （平手时：封板时间最早 → 封单最大）；后排数衡量持续性
            （文案：后排越多的核心持续性越好；独苗题材高度有限）
  ladder    连板梯队（按板高分组，供「连板天梯」视角）
  deviation 偏离值雷达：本地 DuckDB 全市场扫描（v_daily_qfq 十年日线）。
            口径（文案「严重异常波动」章节）：偏离值 = 个股区间涨幅 − 指数区间涨幅；
            普通异动 = 3 日累计偏离 ±20%（10cm）/ ±30%（20cm）；
            严重异动 = 10 日偏离 ≥100% 或 30 日偏离 ≥200%（10cm/20cm 同）。
            基准指数：沪市→上证指数 000001.SH，深市→深证成指 399001.SZ
            （index.history 全段缓存于 .ht_cache，不重复抓）。
  fate      高位承接 / A杀监控：昨日连板≥2 个股今日结局
            （连板晋级/炸板/断板收涨/断板走弱/断板大跌/跌停），
            非池内个股今日涨跌幅来自 DuckDB；按板高分组统计承接率
            （文案：中位股、补涨股最容易出现 A杀）
  cycle     情绪周期定位：启动-发酵-高潮-震荡-退潮 的规则化机械推断
            （涨停数/最高连板/晋级率 近3日 vs 前3日），只给依据不下结论
  anomalies 异动/监管事件：special anomaly-list（today-only，历史日期诚实置空）
  rules     静态口径速查：监管阶梯（抓捕＞封账户＞停牌＞限制买入＞公告）、
            异动阈值、核心交易要点（摘自用户文案）
  pool      明日交易备选池（C7 主板概念口径）：四档环境配额 + 涨停组/低吸组打分
            + 负面清单 + 昨日一字排除；C6 起环境分档含隔夜美股闸门（纳指/标普
            隔夜 ≤-2.0% 强制 defensive），C7 起入选门槛 70 + 市场量能闸门
            （缩量不低吸；expB/expC 单变量实验采纳，expA 否决；T+2 持有证伪）；
            资金管理层 S1（防守/冰点档半仓）为建议层；
            每日验证自我优化闭环——
            validate_prev_pool 用 T 日真实走势验证 T-1 的 picks（买点/风险位/归因，
            冻结执行层 _MF_BUY），record_validation 按日留痕 pool_track.json，
            update_optimizer 从 45 日滚动窗口统计有界调整（因子分桶惩罚 +
            入选门槛微调，状态为验证记录的纯函数，幂等可重放）→ pool_opt.json；
            结构规则/环境配额/执行层永不自动改动，样本不足不动作

数据源：本地快照（调用方传 bundles）+ 本地 DuckDB + index.history + anomaly-list。
HTTP 层不做业务计算：providers.speculation() 在每日抓取时调用本引擎；
本文件也可独立回填既有快照：

    python backend/recap/speculate.py --date 20260902    # 单日计算并合并进快照
    python backend/recap/speculate.py --recent 30        # 回填最近 30 份快照
    python backend/recap/speculate.py --missing          # 回填所有缺失快照
    python backend/recap/speculate.py --date D --force-rewrite   # 口径守卫逃生舱

回填纪律（2026-09-10 代码化）：改池口径不回填 data/recap 历史快照——retrofill
读旧快照备选池的口径版本标记（pool.note 首段，如 "C7"/"C_Final"/"M_Final"）
与当前 RULES_VERSION 不一致时拒绝重算（--force-rewrite 显式逃生）；即使口径
一致，回填路径也以 record_metrics=False 重算，不写 pool_track.json /
pool_opt.json（回填不得污染 45 日优化器样本），快照内 validation/optimizer
段落照常生成。us_close_task 的 `--date T`（当天终版）走 record_metrics=True。

实现布局（2026-09-10 门面拆分，对外零改动）：静态口径/基础工具在 spec_rules，
序列缓存（指数/行业/情绪 + 进程内 memo）在 spec_series，DuckDB 层在 spec_duckdb，
快照构建器（themes/ladder/deviation/fate/cycle/anomalies）在 spec_builders，
备选池在 spec_pool，验证/优化器闭环在 spec_validate。依赖单向：
rules ← series ← duckdb ← builders/validate ← pool ← 本门面。本文件保留
compute 主编排、快照读写/retrofill 与 CLI 入口，并把旧模块级名字全部
re-export（`from backend.recap.speculate import 旧名字` / speculate.属性 照旧）。
"""
import argparse
import copy
import gzip
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ht          # noqa: E402  兼容旧模块属性（实现在 spec_* 子模块使用）
import snapio      # noqa: E402
import fsutil      # noqa: E402  原子写盘单一来源（backend/fsutil.py）
import thscodes    # noqa: E402  代码转换单一来源（backend/thscodes.py）
import concept_map  # noqa: E402  个股→概念归属（备选池概念维度单一来源）
import us_market   # noqa: E402  隔夜美股因子单一来源（backend/us_market.py）
from modules import cget  # noqa: E402
import industry_common  # noqa: E402  # backend/ 个股→一级行业共享映射读取器
import index_hist_cache  # noqa: E402  指数/行业指数日线增量缓存（boards 共用）
import execution_layer  # noqa: E402  执行层常量单一来源（backend/execution_layer.py）

# ---------------------------------------------------------------- 门面 re-export
# 旧 speculate 模块级名字全部显式 re-export（含单测/外部引用的私有名），保证
# `from backend.recap.speculate import 任意旧名字` 与 speculate.属性 访问零改动。
from spec_rules import (  # noqa: E402
    INDEX_BENCH, NEAR_D10, NEAR_D30, NORMAL_D3, RULES, SCAN_TH,
    SEVERE_D10, SEVERE_D30,
    _date_iso, _first_theme, _round2, board_class, thscode_of)
from spec_series import (  # noqa: E402
    PANEL_SENTIMENT, _IND_LOOKUP_MEMO, _ROT_BOARDS, _SENT_ROWS_MEMO,
    _industry_catalog, _industry_lookup, _mtime_or_none, _series_pct,
    index_gain, index_series, industry_series, read_sentiment_rows)
from spec_duckdb import (  # noqa: E402
    CACHE_DIR, _db_export, _lu_volr, _market_amt_ratio, _prev_yizi_codes,
    day_pcts, ohlc_rets, scan_deviation, scan_trend)
from spec_builders import (  # noqa: E402
    _FATE_ORDER, _avg, _dev_status, build_anomalies, build_cycle,
    build_deviation, build_fate, build_ladder, build_themes)
from spec_pool import (  # noqa: E402
    _CONCEPT_LU_TIERS, _MF_ENV_CN, _MF_QUOTA, _MF_US_GATE, _MAIN_PREFIX,
    _NLU_AMT_RATIO_MIN, _NLU_SECTOR_RANK_MIN, _ROT_DAILY,
    _best_concept, _concept_gate, _concept_heat, _concept_score,
    _is_main_board, _lu_candidate, _mf_env, _nlu_candidate,
    _pool_pick_row, _rotation_strength, _us_gate_hit, _us_row,
    build_pool, pool_rules_tag, RULES_VERSION)
from spec_validate import (  # noqa: E402
    _MF_BUY, _OPT_BUCKET_MIN_N, _OPT_MINSCORE_MIN_N, _OPT_MIN_SCORE,
    _OPT_WINDOW_DAYS, _OBS_CONCEPT_COLD_AVG, _OBS_CONCEPT_COLD_N,
    _OBS_STRONG_CONCEPT_MIN, _OBS_STRONG_CONCEPT_N,
    _OOS_BASELINE, _OOS_CRIT_WIN_DROP, _OOS_REVIEW_EVERY,
    _OOS_VERDICT_TEXT, _OOS_WARN_MEAN, _OOS_WARN_WIN_DROP,
    POOL_OPT, POOL_TRACK,
    _c7_rows, _exec_pnl_realtime, _load_opt, _load_track,
    _observation_state, _oos_state, _oos_verdict, _optimizer_state,
    _pick_buckets, _save_track, _val_attribute, _val_exec_stats,
    _val_stats, record_validation, update_optimizer, validate_prev_pool)

VERSION = 1

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 组装

def compute(date8, bundles, prev_bundles=None, sent_rows=None, historical=None,
            record_metrics=True):
    """主入口：返回模块结果 {"status": "ok", "data": {...}}。

    bundles      当日 {zt, zb, dt, hot, lhb}（快照 schema 行，可为空列表）
    prev_bundles 前一交易日 {zt}（fate 用；可为 None）
    sent_rows    sentiment.csv 行（None 时自读）
    historical   是否历史日期（决定异动榜单是否可抓；None 时按是否今天判断）
    record_metrics 每日验证闭环副作用开关（默认 True，向后兼容）：False 时跳过
                 record_validation / update_optimizer 的写盘（pool_track.json /
                 pool_opt.json 不被回填污染），快照内 validation/optimizer 段落
                 照常生成。retrofill 路径传 False；providers 每日抓取与
                 us_close_task 当天终版（--date T）保持 True。
    """
    t0 = time.time()
    if historical is None:
        historical = date8 != time.strftime("%Y%m%d")
    bundles = bundles or {}
    zt = bundles.get("zt") or []
    zb = bundles.get("zb") or []

    data = {
        "version": VERSION,
        "calc": {"date": date8, "historical": bool(historical),
                 "elapsed_ms": None,
                 "口径": "偏离值=个股区间涨幅−指数涨幅；沪市基准上证指数，深市基准深证成指；"
                         "普通异动 3 日偏离 ±20%(10cm)/±30%(20cm)；"
                         "严重异动 10 日≥100% 或 30 日≥200%；北交所不在判定口径内"},
        "themes": build_themes(zt),
        "ladder": build_ladder(zt),
    }

    try:
        data["deviation"] = {"ok": True, **build_deviation(date8, zt, zb)}
    except Exception as e:  # noqa: BLE001 - 偏离值失败不连坐其他部分
        data["deviation"] = {"ok": False,
                             "error": f"{type(e).__name__}: {str(e)[:200]}"}

    try:
        data["fate"] = build_fate(date8, (prev_bundles or {}).get("zt") or [],
                                  bundles)
    except Exception as e:  # noqa: BLE001
        data["fate"] = {"rows": [], "stats": [],
                        "note": f"计算失败: {type(e).__name__}: {str(e)[:150]}"}

    try:
        data["cycle"] = build_cycle(
            date8, sent_rows if sent_rows is not None else read_sentiment_rows())
    except Exception as e:  # noqa: BLE001
        data["cycle"] = {"phase": "计算失败",
                         "reasons": [f"{type(e).__name__}: {str(e)[:150]}"],
                         "series": [], "indicators": {}}

    try:
        data["anomalies"] = build_anomalies(historical)
    except Exception as e:  # noqa: BLE001
        data["anomalies"] = {"available": False,
                             "note": f"抓取失败: {type(e).__name__}: {str(e)[:150]}"}

    try:
        # 每日验证与自我优化闭环：先验证昨日备选池（真实走势/买点/归因），
        # 再带着最新优化器状态（滚动窗口统计）选今日标的
        _sr = sent_rows if sent_rows is not None else read_sentiment_rows()
        # T+1 概念指数当日涨幅（strong_concept / 概念持续性不足归因用；
        # compute 收到的 bundles 即 T+1 当日快照，历史回填日由 bundles_from_snap 提供）
        _con_pct = {r.get("概念"): r.get("涨跌幅")
                    for r in (bundles.get("concepts") or []) if r.get("概念")}
        val = validate_prev_pool(date8, _sr, concept_pct=_con_pct)
        if val:
            data["pool_validation"] = val
            if record_metrics:
                record_validation(date8, val)   # 回填路径（False）不污染验证样本
    except Exception as e:  # noqa: BLE001 - 验证失败不连坐选股
        data["pool_validation"] = {"ok": False,
                                   "error": f"{type(e).__name__}: {str(e)[:150]}"}
    try:
        data["pool_optimizer"] = update_optimizer(date8, persist=record_metrics)
    except Exception as e:  # noqa: BLE001 - 优化器失败按默认口径选股
        data["pool_optimizer"] = {"version": RULES_VERSION,
                                  "error": f"{type(e).__name__}: {str(e)[:150]}"}

    _f_row, _us_ok, _us_why = us_market.row_freshness(_date_iso(date8))
    if _us_ok:
        try:
            data["pool"] = build_pool(date8, bundles, data)
        except Exception as e:  # noqa: BLE001 - 备选池失败不连坐
            data["pool"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}",
                            "market": None, "picks": [], "risk": None, "concl": None}
    else:
        # 终版池必须含隔夜场次（us_date==T）；隔夜未定期间只出 pending 占位，
        # 由 T+1 凌晨美股收盘链重算覆盖——提前露出的选股不是终版口径。
        data["pool"] = {"ok": False, "stage": "pending", "note": _us_why,
                        "validation": data.get("pool_validation"),
                        "optimizer": data.get("pool_optimizer"),
                        "market": None, "picks": [], "risk": None, "concl": None}

    data["rules"] = RULES
    data["calc"]["elapsed_ms"] = int((time.time() - t0) * 1000)
    return {"status": "ok", "data": data}


# ---------------------------------------------------------------- 回填 CLI

def bundles_from_snap(snap):
    mod = (snap or {}).get("modules") or {}

    def rows(name):
        m = mod.get(name) or {}
        return m.get("data") or [] if m.get("status") == "ok" else []

    return {"zt": rows("limit_up_pool"), "zb": rows("limit_break_pool"),
            "dt": rows("limit_down_pool"), "hot": rows("hot_stock"),
            "lhb": rows("lhb"), "concepts": rows("concepts")}


def _save_same_format(snap, date8):
    """写回快照：原来什么格式还什么格式（老快照是 .json.gz）。两种形态均原子写。"""
    p = snapio.snap_path(snapio.RECAP_DATA, date8)
    if p.endswith(".gz"):
        return fsutil.save_gzip_json_atomic(p, snap)
    return snapio.save(date8, snap)


def _has_speculation_fast(date8):
    """"是否已有 speculation 模块"的短路径判断（--missing 批量去重解析用）。

    快照 JSON 是 ensure_ascii=False 落盘的，模块键 `"speculation"` 以字面字节出现；
    实扫全量 170 份快照验证：字节标记存在 ⇔ modules.speculation 存在（内容为中文
    行情/文案数据，不会含该英文键）。标记不存在 → 必缺模块（免整份 JSON parse）；
    标记存在 → 视为已有（读不了/坏文件也按有处理，宁可漏补也不误重算）。
    """
    fp = snapio.snap_path(snapio.RECAP_DATA, date8)
    try:
        if fp.endswith(".gz"):
            with gzip.open(fp, "rb") as f:
                return b'"speculation"' in f.read()
        with open(fp, "rb") as f:
            return b'"speculation"' in f.read()
    except OSError:
        return True


def retrofill(date8, quiet=False, record_metrics=False, force_rewrite=False):
    """计算单日投机分析并合并进既有快照。返回 (是否计算, 说明)。

    口径守卫（2026-09-10 把「改池口径不回填历史」纪律代码化）：旧快照备选池的
    口径版本标记（pool.note 首段，见 pool_rules_tag）与当前 RULES_VERSION 不一致
    时拒绝重算——防止用当前版本代码整块覆盖历史 pool picks/validation；
    --force-rewrite 为显式逃生舱。回填路径默认 record_metrics=False（不写
    pool_track/pool_opt），当天终版重算由 CLI --date 显式传 True。
    """
    snap_doc = snapio.load(date8)
    if snap_doc is None:
        return False, "快照不存在"
    snap = copy.deepcopy(snap_doc)   # 快照读取带共享缓存：改写前必须脱手
    old_mod = ((snap.get("modules") or {}).get("speculation") or {}).get("data") or {}
    old_pool = old_mod.get("pool") or {}
    tag = pool_rules_tag(old_pool)
    if (tag is not None or old_pool.get("picks")) and tag != RULES_VERSION \
            and not force_rewrite:
        return False, (f"口径不一致（快照={tag or '无标记'}，当前={RULES_VERSION}），"
                       f"拒绝重算历史池；--force-rewrite 可覆盖")
    dates = snapio.list_dates()
    prev_dates = [d for d in dates if d < date8]
    prev_bundles = None
    if prev_dates:
        prev_bundles = bundles_from_snap(snapio.load(prev_dates[0]))
    res = compute(date8, bundles_from_snap(snap), prev_bundles,
                  record_metrics=record_metrics)
    # 隔夜美股 factors 缺该日行时 compute 只会出 pending 占位；历史真池不能被占位
    # 冲掉（快照冻结纪律；factors 目前覆盖全部快照日，此分支仅防 factors 历史被
    # 裁剪/重建后的误回填），保留旧池、其余模块照常刷新。
    if ((res["data"].get("pool") or {}).get("stage") == "pending"
            and old_pool.get("picks")):
        res["data"]["pool"] = old_pool
        if not quiet:
            print(f"  {date8}: 隔夜美股因子缺行，保留历史备选池不回填占位")
    # 回填不降级：旧快照里已有当日抓到的异动榜单时保留（anomaly-list 是 today-only
    # 接口，隔天重算只会得到「不可回补」，不能把已有真数据冲掉）
    old_an = old_mod.get("anomalies") or {}
    if old_an.get("available") and not (res["data"].get("anomalies") or {}).get("available"):
        res["data"]["anomalies"] = old_an
    snap.setdefault("modules", {})["speculation"] = res
    out = _save_same_format(snap, date8)
    if not quiet:
        dev = res["data"].get("deviation") or {}
        n_dev = len(dev.get("rows") or []) if dev.get("ok") else "err"
        cyc = (res["data"].get("cycle") or {}).get("phase")
        n_fate = len((res["data"].get("fate") or {}).get("rows") or [])
        n_theme = len((res['data']['themes'] or {}).get('groups') or [])
        tag_txt = "" if record_metrics else " · 指标不落账(record_metrics=False)"
        print(f"  {date8}: 题材 {n_theme} 组 · "
              f"偏离候选 {n_dev} · 承接 {n_fate} · 周期「{cyc}」 -> {os.path.basename(out)}"
              + tag_txt)
    return True, "ok"


def main():
    ap = argparse.ArgumentParser(description="投机分析回填（合并进既有快照）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--date", help="8 位日期，如 20260902")
    g.add_argument("--recent", type=int, help="最近 N 份快照")
    g.add_argument("--missing", action="store_true", help="所有缺 speculation 的快照")
    ap.add_argument("--force-rewrite", action="store_true",
                    help="口径守卫逃生舱：旧口径快照也强制用当前版本代码重算")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(HERE))   # backend/（lockutil）
    import lockutil
    lock = lockutil.acquire(os.path.join(os.path.dirname(os.path.dirname(HERE)),
                                           ".status", "fetch-recap.lock"))
    if lock is None:
        print("已有复盘抓取在运行（锁），本次跳过。")
        sys.exit(0)

    dates = snapio.list_dates()   # 倒序
    try:
        if args.date:
            # --date 单日 = 当天终版重算语义（us_close_task 的 T+1 凌晨链走这里）：
            # 验证/优化器照常落账（record_metrics=True，口径守卫仍在）
            targets = [args.date]
            record_metrics = True
        else:
            targets = []
            record_metrics = False   # 批量回填：快照照常生成，指标不落账
            if args.recent:
                targets = dates[:args.recent]
            elif args.missing:
                # 短路径判断：字节标记不在 → 必缺 speculation，免整份 JSON parse
                targets = [d for d in dates if not _has_speculation_fast(d)]
            else:
                ap.error("需要 --date / --recent / --missing 之一")
        print(f"== 投机分析回填 {len(targets)} 份快照"
              + ("（终版落账）" if record_metrics else "（指标不落账）")
              + ("· 强制重写" if args.force_rewrite else "") + " ==")
        ok = skipped_guard = 0
        for d in targets:
            try:
                done, msg = retrofill(d, record_metrics=record_metrics,
                                      force_rewrite=args.force_rewrite)
                ok += 1 if done else 0
                if not done:
                    guard = "口径不一致" in msg
                    skipped_guard += 1 if guard else 0
                    print(f"  {d}: 跳过（{msg}）")
            except Exception as e:  # noqa: BLE001 - 单日失败不中断批量
                print(f"  {d}: 失败 {type(e).__name__}: {str(e)[:150]}")
        print(f"== 完成 {ok}/{len(targets)} =="
              + (f"（口径守卫跳过 {skipped_guard} 天，改池口径不回填历史）"
                 if skipped_guard else ""))
    finally:
        lockutil.release(lock)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
