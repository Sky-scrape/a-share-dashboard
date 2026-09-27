# -*- coding: utf-8 -*-
"""统一 logging（2026-09-04 收敛，替代散落的 print）。

采集/抓取脚本历史上用 print + [ok]/[fail] 手拼前缀，靠 .bat 重定向落盘：
无时间戳、无级别、错误无法按级聚合检索。get_logger(name) 提供：

- 输出格式 `HH:MM:SS LEVEL 消息`（时间戳补齐，消息内容不变，旧日志习惯不受影响）；
- 默认输出到 stdout：.bat 的 `>> 日志` 重定向照旧生效，不另落第二份文件；
- 幂等：同一 logger 重复获取不叠加 handler（smoke 会 import 这些模块）；
- 不动 sys.stdout / 不 reconfigure 编码（auc_collector 的既有教训：导入即改
  宿主输出编码会把 smoke 日志搅成 GBK/UTF-8 混排）。

2026-09-27 起（方案 O-2）：新增 get_file_logger —— stdout + 追加文件的复合 logger，
收拢 notify._log / watchdog._log 两份同构手写实现（时间戳格式、目录创建、异常
吞掉三处细节此前各写一遍）。适用场景：被多个进程共用的聚合日志（notify.log /
watchdog.log），单进程脚本仍用 get_logger + .bat 重定向即可。
"""
import logging
import os
import sys

_CONFIGURED = set()


def get_logger(name, level=logging.INFO):
    lg = logging.getLogger(name)
    if name not in _CONFIGURED:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                         "%H:%M:%S"))
        lg.addHandler(h)
        lg.setLevel(level)
        lg.propagate = False
        _CONFIGURED.add(name)
    return lg


def get_file_logger(name, file_path, level=logging.INFO):
    """stdout（统一 HH:MM:SS 格式）+ 追加文件（完整日期，便于跨日翻档）的复合 logger。

    文件侧异常静默吞掉（目录建不出/文件锁住时日志退化为仅 stdout，不拖垮主链路，
    与 notify/watchdog 旧实现同一口径）。幂等：文件 handler 只挂一次。"""
    lg = get_logger(name, level)
    if not any(isinstance(h, logging.FileHandler) for h in lg.handlers):
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            fh = logging.FileHandler(file_path, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
            lg.addHandler(fh)
        except OSError:
            pass
    return lg
