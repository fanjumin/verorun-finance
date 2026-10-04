#!/usr/bin/env python3
"""进化共享基座 —— memory_engine / cogevolution_substrate 共用（插件侧内部库）。

纯代码共享包（无 plugin.json / 路由 / 作业 / 建库 / config / 连接池），
收敛两插件"已等价"的工程兜底，使分叉踩过的坑修一次两处生效：
- P0：`pii`（PII 判定）、`sql`（user_profiles.meta 读取）；
- P1a：`text`（keywords / record_hash，memory_engine 口径）、
  `curator`（curator 角色行解析）、`vector`（PG vector 文本字面量）。
语义不同的实现（如 cogevolution_substrate 的 keywords / content_hash）不在本包收敛。
此处不急切 import 子模块，避免导入副作用。
"""

__version__ = '0.1.0'
