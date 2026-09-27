#!/usr/bin/env python3
"""Forgetting service — 记忆遗忘/整理闭环（衰减 + 软归档）。

设计要点（与 sedimentation.py 同范式）:
  run_daily() 由 APScheduler 每日 03:10 触发（沉淀 02:40 之后，见
  __init__.register_jobs）。run() 分两步: 先归档（终态）、后衰减（留存项）。

禁止物理删除:
  归档 = status 由 'active' 置为 'archived'。该状态值并非本服务新引入，仓内已有两处
  一致实现——
    - services/extractor.py  _enforce_owner_cap()  容量保护（超 max_memories_per_owner）
    - routes.py             DELETE /memories/<id>  管理员人工归档
  故本服务只是把「容量 / 人工」触发扩展为「时效 / 价值」触发，零新状态语义。
  归档后自动退出检索与沉淀候选（retriever 与 sedimentation 均硬过滤 status='active'）。

口径说明（非缺陷，勿误改）:
  1. 归档判龄用 COALESCE(last_hit_at, created_at)，而检索侧 recency 用
     COALESCE(last_hit_at, updated_at)（retriever.recency_expr）。二者目标不同:
     前者问「这条记忆多久没被用过了」（年龄），后者问「这条记忆多久没被碰过了」
     （活跃度）。取 created_at 的理由: 自写入起从未命中过的记忆最该被归档。
  2. 衰减 **不更新** updated_at —— 该列参与检索 recency 计算，改动会把陈旧记忆的
     时效分虚增；同时也会干扰 _enforce_owner_cap 的 updated_at 排序。
  3. importance 门槛当前为恒真条件: 全仓无任何写入方把 importance 抬到默认值 0.5
     以上（仅 routes 与图视图读取展示）。保留该条件作为防御性约束——高价值保护实际
     由「命中次数 > 1」与「未满归档判龄」承担。若将来出现 importance 写入方，本条件
     自动生效。
  4. content_hash 为 UNIQUE（migrations/v1.0.0_init.sql）→ 归档项不会被重复抽取，
     但也不会自动复活；如需复活须显式 UPDATE status='active'。
"""

import logging

logger = logging.getLogger('memory_engine.forgetting')

# ── 默认阈值 ────────────────────────────────────────────────
# 均可经 plugin.json 的 config 同名键覆盖；未声明该键时由此处默认值兜底
# （base.get_config_value 语义: 仅读 plugin.json config 段，缺失即取默认）。
DEFAULT_DAILY_BUDGET = 500      # 单轮每步最多处理行数
DEFAULT_ARCHIVE_DAYS = 90       # 归档判龄: 自写入/末次命中起 N 天未被使用
DEFAULT_DECAY_DAYS = 30         # 衰减判龄
DEFAULT_QUALITY_FLOOR = 0.3     # 衰减地板（与 retriever 的 quality_score >= 0.3 门槛对齐）
DEFAULT_DECAY_STEP = 0.05       # 每次衰减幅度
DEFAULT_IMPORTANCE_CEILING = 0.7   # 归档要求 importance 低于该值
DEFAULT_MAX_HIT_COUNT = 1       # 归档要求命中次数不超过该值（高价值保护）

# ── SQL ────────────────────────────────────────────────────
# 占位符 `?` 由 PgConnection._replace_placeholders 自动转 `%s`；
# make_interval(days => ?) 为本仓既有写法（见 iot_hub/services/alerting.py）。
_ARCHIVE_SQL = """
UPDATE memories
   SET status = 'archived'
 WHERE id IN (
       SELECT id FROM memories
        WHERE status = 'active'
          AND COALESCE(last_hit_at, created_at) < now() - make_interval(days => ?)
          AND importance < ?
          AND hit_count <= ?
          AND memory_type NOT IN ('lesson')
          AND source <> 'reflexion'
        ORDER BY COALESCE(last_hit_at, created_at) ASC
        LIMIT ?
 )
"""

_DECAY_SQL = """
UPDATE memories
   SET quality_score = GREATEST(?, quality_score - ?)
 WHERE id IN (
       SELECT id FROM memories
        WHERE status = 'active'
          AND COALESCE(last_hit_at, created_at) < now() - make_interval(days => ?)
        ORDER BY COALESCE(last_hit_at, created_at) ASC
        LIMIT ?
 )
"""


class ForgettingService:
    """每日/手动执行「软归档 + 质量衰减」，维持记忆池的时效性。"""

    def __init__(self, config: dict):
        self._config = config or {}
        self.last_stats = {'archived': 0, 'decayed': 0, 'skipped': False}

    # ── 入口 ──────────────────────────────────────────────
    def run_daily(self):
        """APScheduler 每日任务（03:10，晚于沉淀 02:40，避免同轮争用）。"""
        budget = int(self._config.get('forgetting_daily_budget', DEFAULT_DAILY_BUDGET))
        return self.run(batch_limit=budget)

    def run(self, batch_limit: int = DEFAULT_DAILY_BUDGET) -> int:
        """执行一轮归档 + 衰减（幂等）。返回本轮处理行数，异常时返回 0。"""
        self.last_stats = {'archived': 0, 'decayed': 0, 'skipped': False}
        if not self._config.get('enable_forgetting', True):
            self.last_stats['skipped'] = True
            return 0
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            # 顺序: 先归档（终态）再衰减（留存项）—— 避免对即将归档项做无效写入。
            # 归档谓词不含 quality_score，衰减不改变归档候选集，两步无交叉干扰，
            # 顺序只影响写入行数（性能），不影响最终结果集。
            archived = self._archive(conn, batch_limit)
            decayed = self._decay(conn, batch_limit)
            conn.commit()
            self.last_stats.update({'archived': archived, 'decayed': decayed})
            logger.info(
                'forgetting run done: archived=%d decayed=%d', archived, decayed)
            return archived + decayed
        except Exception as e:
            logger.error('forgetting run failed: %s', e)
            conn.rollback()
            return 0
        finally:
            conn.close()

    # ── 步骤 ──────────────────────────────────────────────
    def _archive(self, conn, limit: int) -> int:
        """软归档: 长期未使用且低命中、非自我演化产物的记忆。"""
        if limit <= 0:
            return 0
        days = int(self._config.get('forgetting_archive_days', DEFAULT_ARCHIVE_DAYS))
        ceiling = float(self._config.get(
            'forgetting_importance_ceiling', DEFAULT_IMPORTANCE_CEILING))
        max_hits = int(self._config.get(
            'forgetting_max_hit_count', DEFAULT_MAX_HIT_COUNT))
        cur = conn.execute(_ARCHIVE_SQL, (days, ceiling, max_hits, limit))
        return max(0, cur.rowcount or 0)

    def _decay(self, conn, limit: int) -> int:
        """质量衰减: 陈旧记忆的 quality_score 按步长下调，地板 0.3。"""
        if limit <= 0:
            return 0
        days = int(self._config.get('forgetting_decay_days', DEFAULT_DECAY_DAYS))
        floor = float(self._config.get(
            'forgetting_quality_floor', DEFAULT_QUALITY_FLOOR))
        step = float(self._config.get('forgetting_decay_step', DEFAULT_DECAY_STEP))
        cur = conn.execute(_DECAY_SQL, (floor, step, days, limit))
        return max(0, cur.rowcount or 0)
