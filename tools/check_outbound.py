# -*- coding: utf-8 -*-
"""出站 HTTP 调用点守卫（CI 用，2026-09-27 方案 O-1）。

背景：netguard（SSRF 防线）与 http_retry（重试）都是单一来源，但守卫此前只接了
3 个文件，其余出站调用靠「URL 全字面量」约定兜第一层——约定挡不住未来赶工时
新加的动态拼 URL。出站 GET 现统一收口 backend/http_retry.guarded_get（内置
netguard 校验：https + 主机白名单 + 受限地址阻断 + 禁跟随重定向）。

本脚本扫描 backend/**/*.py：凡直接调用 requests.get/post/put/delete/head/request
的文件，必须满足其一，否则退出码 1（CI 红）：
  - import netguard  （自行组守卫语义的少数场景：推送、诊断、备源）
  - import http_retry（走 guarded_get / retry）
  - 命中下方 ALLOWLIST（须写明理由与豁免日期，新增前先想想能不能不豁免）
"""
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"

# 裸 requests 调用的显式豁免（相对 backend/ 的 posix 路径 → 理由）
ALLOWLIST = {
    # 守卫本体：实现层，requests 调用即被守卫的语义本身
    "netguard.py": "守卫本体（2026-09-27）",
    # LEGACY 东财旧口径：2026-09-01 退役冻结，仅供口径回溯，不再接任何链路
    # （README「目录结构」节有记录；其中 _clist 手写多主机循环是 http_retry 要消灭的反面教材）
    "rotation/config.py": "LEGACY 退役冻结（2026-09-27）",
    "rotation/fetch_day.py": "LEGACY 退役冻结（2026-09-27）",
}

_CALL = re.compile(r"requests\.(?:get|post|put|delete|head|request)\s*\(")


def main():
    violations = []
    scanned = 0
    for py in sorted(BACKEND.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _CALL.search(text):
            continue
        scanned += 1
        rel = py.relative_to(BACKEND).as_posix()
        if rel in ALLOWLIST:
            continue
        if re.search(r"^\s*(?:import netguard|from netguard import)", text, re.M):
            continue
        if re.search(r"^\s*(?:import http_retry|from http_retry import)", text, re.M):
            continue
        violations.append(rel)

    print(f"[check_outbound] backend 内裸 requests 调用文件：{scanned} 个（豁免除外）")
    if violations:
        print("[check_outbound] 以下文件直接调 requests 且未接守卫/白名单：")
        for v in violations:
            print(f"  - backend/{v}")
        print("出站请求请改走 http_retry.guarded_get（守卫+重试单一入口），")
        print("或在本脚本 ALLOWLIST 显式豁免并写明理由。")
        return 1
    print("[check_outbound] OK：全部出站调用点已收口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
