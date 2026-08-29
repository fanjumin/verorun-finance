"""Captcha Embedded Plugin — 核心逻辑子包

包含拼图生成（generator）、HMAC token 安全（security）、行为轨迹分析与风险
评分（behavior）、存储层（store，Redis + 内存回退）。数据持久化策略与 PG
schema 预留见同目录上级的 models.py。
"""
