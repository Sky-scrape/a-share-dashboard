# -*- coding: utf-8 -*-
"""网络重试 · 单一来源（2026-09-04 收敛，2026-09-10 加退避与可重试判定）。

providers._retry（3 次/3s）与 fetch_global._retry（2 次/2s）是两套同构实现；
统一到这里，各文件保留同名薄转发（签名不同就传参），调用点零改动。

2026-09-10 起：
- 指数退避（delay × backoff^i，封顶 cap）+ 抖动（±jitter 比例），避免上游限流
  时整点齐步重试；
- 只对网络类失败重试：超时 / 连接类 / 响应 JSON 解析失败 / 5xx / 429，以及
  RuntimeError（ht CLI 封装的上游失败也是 RuntimeError，属网络/上游类）；
  编程错误（TypeError/KeyError 等）首次即抛，不拿重试掩盖 bug；
- 旧调用点不传新参数时行为兼容：重试次数/基础间隔语义不变，退避在多轮重试时
  才与旧的固定间隔有差异。
"""
import random
import time

try:                       # requests 是全项目既有依赖；缺失时退化为内置连接类判定
    import requests
    _REQ = requests.exceptions
    _excs = [_REQ.Timeout, _REQ.ConnectionError, _REQ.ChunkedEncodingError]
    _jd = getattr(_REQ, "JSONDecodeError", None)   # requests>=2.27 才有
    if _jd is not None:
        _excs.append(_jd)   # 上游回非 JSON（限流页/截断），网络类而非编程错误
    _NETWORK_EXCS = tuple(_excs)
except ImportError:        # pragma: no cover
    _NETWORK_EXCS = (ConnectionError, TimeoutError)


def is_retryable(e):
    """异常是否值得重试：超时/连接类/JSON 解析失败/5xx/429/RuntimeError → True。"""
    if isinstance(e, RuntimeError):   # ht CLI 封装（ht.py）的上游失败统一是 RuntimeError
        return True
    if isinstance(e, _NETWORK_EXCS):
        return True
    if _REQ is not None and isinstance(e, _REQ.HTTPError):
        code = getattr(getattr(e, "response", None), "status_code", 0) or 0
        return code >= 500 or code == 429
    return False


def retry(fn, tries=2, delay=2.0, backoff=2.0, cap=30.0, jitter=0.25,
          retry_on=None, on_retry=None):
    """执行 fn，失败重试 tries 次（总尝试 tries+1 次），仍失败抛最后一个异常。

    - 只重试 is_retryable / 自定义 retry_on(e) 判定为 True 的异常，其余直接抛；
    - 第 i 次重试前等待 min(delay * backoff**i, cap) 秒，±jitter 比例抖动；
    - on_retry(i, e)（可选）：每次重试前回调（i 从 0 计），调用方可留日志。
    """
    judge = retry_on or is_retryable
    last = None
    for i in range(tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 可重试性由 judge 决定，编程错误直接抛
            if not judge(e):
                raise
            last = e
            if i < tries:
                wait = min(delay * (backoff ** i), cap)
                wait *= 1.0 + random.uniform(-jitter, jitter) if jitter else 1.0
                if on_retry is not None:
                    on_retry(i, e)
                time.sleep(max(wait, 0.0))
    raise last
