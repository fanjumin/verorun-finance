#!/usr/bin/env python3
"""A/B 注入收益对照：分流哈希 + 结局记录 + 对照报告聚合。

K2 约束：注入点（before_prompt_resolve）拿不到 session_id/task_id，
故分流键 = user_id 确定性哈希（同用户恒定同臂，无闪烁）。
"""

import hashlib
import logging
import math

logger = logging.getLogger('memory_engine.abtest')

ARM_TREATMENT = 'treatment'
ARM_CONTROL = 'control'


def assign_arm(user_id: str, control_pct: int = 50) -> str:
    """确定性分流：sha256('abtest|user_id') % 100 < control_pct → control。"""
    if not user_id:
        return ARM_TREATMENT
    bucket = int(hashlib.sha256(f'abtest|{user_id}'.encode('utf-8')).hexdigest()[:8], 16) % 100
    return ARM_CONTROL if bucket < max(0, min(100, int(control_pct))) else ARM_TREATMENT


class AbTestService:
    """结局记录（事件侧）与对照报告聚合。"""

    def __init__(self, config: dict):
        self._config = config or {}

    def enabled(self) -> bool:
        return bool(self._config.get('abtest_enabled', False))

    # ── 结局记录：AGENT_TASK_COMPLETED 事件侧调用 ──────────────────

    def record_outcome(self, task: dict, result: dict, agent_id: str,
                       injected_len: int = 0):
        """每个已完成任务一行。异常静默（绝不影响主链路）。

        injected_len>0 即 treatment 臂实际注入（由注入侧缓存最近一次块长，
        本服务按同 arm 判定回填；见 prompt_injector 侧说明）。
        """
        if not self.enabled():
            return
        user_id = str((task.get('input_data') or {}).get('user_id')
                      or task.get('user_id') or '')
        if not user_id:
            return
        task_id = str(task.get('task_id') or '')
        arm = assign_arm(user_id, int(self._config.get('abtest_control_pct', 50)))
        injected = bool(arm == ARM_TREATMENT and injected_len > 0)
        tokens = self._tokens_for_task(task_id)
        try:
            from ..models import get_memory_engine_db
            conn = get_memory_engine_db()
            try:
                conn.execute(
                    "INSERT INTO ab_events"
                    " (task_id, user_id, agent_id, arm, injected, block_len,"
                    "  failed, confidence, retries, total_tokens)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (task_id, user_id) DO NOTHING",
                    (task_id, user_id, agent_id, arm, injected, int(injected_len),
                     bool(result.get('failed')),
                     float(result.get('confidence') or 0),
                     int(result.get('retries') or 0), tokens),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning('abtest record failed: %s', e)

    @staticmethod
    def _tokens_for_task(task_id: str) -> int:
        """按 task_id 只读回查 agent_token_logs（缺表/无行返回 0）。"""
        if not task_id:
            return 0
        try:
            from agent_matrix.models import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) AS t"
                    " FROM agent_token_logs WHERE task_id = %s",
                    (task_id,),
                ).fetchone()
            return int(row['t'] or 0)
        except Exception:
            return 0

    # ── 对照报告：management API 调用 ─────────────────────────────

    def report(self, days: int = 14, min_sample: int = 30) -> dict:
        """按臂聚合：任务成功率 / 平均置信度 / 平均 token / z 检验。"""
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            rows = conn.execute(
                "SELECT arm,"
                " COUNT(*) AS n,"
                " COUNT(*) FILTER (WHERE NOT failed) AS ok,"
                " AVG(confidence) AS conf,"
                " AVG(NULLIF(total_tokens, 0))::float AS tok"
                " FROM ab_events"
                " WHERE created_at > now() - make_interval(days => ?)"
                " GROUP BY arm",
                (int(days),),
            ).fetchall()
        finally:
            conn.close()
        arms = {r['arm']: dict(r) for r in rows}
        t, c = arms.get(ARM_TREATMENT), arms.get(ARM_CONTROL)
        out = {'window_days': days, 'treatment': t, 'control': c, 'verdict': 'insufficient'}
        if not t or not c:
            return out
        if min(t['n'], c['n']) < min_sample:
            out['verdict'] = 'insufficient'   # 样本不足，不下结论
            return out
        p1 = t['ok'] / t['n']
        p2 = c['ok'] / c['n']
        pooled = (t['ok'] + c['ok']) / (t['n'] + c['n'])
        se = math.sqrt(pooled * (1 - pooled) * (1 / t['n'] + 1 / c['n']))
        z = (p1 - p2) / se if se > 0 else 0.0
        out.update({'z': round(z, 3), 'delta': round(p1 - p2, 4)})
        if z > 1.96:
            out['verdict'] = 'memory_helps'      # 注入显著提升成功率
        elif z < -1.96:
            out['verdict'] = 'memory_hurts'      # 注入显著损害 → 建议关闭
        else:
            out['verdict'] = 'no_significant_difference'
        return out
