# -*- coding: utf-8 -*-
"""热力图窗口状态：把「实际覆盖 / 应有覆盖」折算成 ok / partial / unknown。

复盘 §10 的规则「ok 由 (expected_rows, actual_rows, exit) 计算，禁止手写」的
global 落地：0/N 窗口（如 0/717 个股窗口）不再写 `[ok] heatmap_cn`，而是
unknown；部分成功是 partial；只有 actual>=expected 且 actual>0 才是 ok。

零依赖纯函数：调用方传入 n_ok / n_total，脏数据（None、负数）一律按 0 夹紧，
不抛错——日志/落盘路径不能因计数异常中断。日志行形如
`heatmap_cn windows=0/717 status=unknown`，便于按字节检索与对账。

状态只进 payload 的 "windows" 字段，不进 errors（0/N 是当天数据覆盖问题，
不是抓取异常，见 fetch_global._run 的分块降级语义）。
"""

OK = "ok"
PARTIAL = "partial"
UNKNOWN = "unknown"


def _n(v):
    """计数夹紧：None/非数字/负数 → 0。"""
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n > 0 else 0


def classify(n_ok, n_total):
    """(实际行数, 应有行数) → ok / partial / unknown。

    - n_total<=0：没有应有样本（0/0），无从判断 → unknown
    - n_ok<=0：一只都没拿到 → unknown（绝不能再报 ok）
    - n_ok>=n_total：全覆盖 → ok
    - 其余：partial（有覆盖但不全）
    """
    n_ok, n_total = _n(n_ok), _n(n_total)
    if n_total <= 0 or n_ok <= 0:
        return UNKNOWN
    if n_ok >= n_total:
        return OK
    return PARTIAL


def summary(label, n_ok, n_total):
    """状态 + 计数打包，供 fetch_heat_* 返回 payload 的 "windows" 字段。"""
    return {"label": label, "status": classify(n_ok, n_total),
            "n_ok": _n(n_ok), "n_total": _n(n_total)}


def log_line(label, stat):
    """一行日志正文：'heatmap_cn windows=0/717 status=unknown'。"""
    st = stat or {}
    return "%s windows=%s/%s status=%s" % (
        label, _n(st.get("n_ok")), _n(st.get("n_total")),
        st.get("status") or UNKNOWN)
