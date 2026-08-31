# -*- coding: utf-8 -*-
"""统一 logging（2026-09-04 收敛，替代散落的 print）。

采集/抓取脚本历史上用 print + [ok]/[fail] 手拼前缀，靠 .bat 重定向落盘：
无时间戳、无级别、错误无法按级聚合检索。get_logger(name) 提供：

- 输出格式 `HH:MM:SS LEVEL 消息`（时间戳补齐，消息内容不变，旧日志习惯不受影响）；
- 默认输出到 stdout：.bat 的 `>> 日志` 重定向照旧生效，不另落第二份文件；
- 幂等：同一 logger 重复获取不叠加 handler（smoke 会 import 这些模块）；
- 不动 sys.stdout / 不 reconfigure 编码（auc_collector 的既有教训：导入即改
  宿主输出编码会把 smoke 日志搅成 GBK/UTF-8 混排）。
"""
import logging
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
