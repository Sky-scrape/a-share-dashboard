# -*- coding: utf-8 -*-
"""版本单一来源（2026-09-27 起）。

此前三套口径并存：git tag v1.0.0、dist 产物名 v1.2.0（打包时手工命名）、
README 更新日志自称 v1.0。exe 是分发形态，「拿到的是哪版、装没装上某次
修复」必须有唯一答案，本文件即答案。消费方：
  - /api/health 顶层 version 字段（server.py health_payload，新鲜度胶囊可校验）
  - 桌面窗口启动页与窗口标题（packaging/gui.py）
  - 启动器横幅（start.py）
  - 打包产物命名（打包exe.bat 读取后重命名单文件 exe）
发布规范：发版时 git tag vX.Y.Z 必须与本文件 __version__ 一致；变更记录写
根目录 CHANGELOG.md（README 只留最近一版 + 指向）。
"""

__version__ = "1.3.0"


def version_string():
    return "v" + __version__
