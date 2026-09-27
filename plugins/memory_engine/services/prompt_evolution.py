#!/usr/bin/env python3
"""Prompt evolution: aggregate metrics, emit admin-approved suggestions,
and close/open evolution rounds at the end of the daily aggregation cycle."""

import hashlib
import logging

logger = logging.getLogger('memory_engine.prompt_evolution')


class PromptEvolutionService:
    """Daily aggregation job body: refresh success_rate / avg_rating per version.

    At the end of each daily run, archives the current open round (closes it)
    and opens a new round for each agent with pending evolution data.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}

    def run_daily(self):
        """APScheduler job: aggregate task outcomes into prompt_metrics,
        then finalize evolution rounds.

        Gated by `prompt_evolution_enabled`（plugin.json 既有声明，此前代码从未
        读取——本改动使该开关真实生效；关闭时不做聚合与 round 归档）。
        """
        if not self._config.get('prompt_evolution_enabled', False):
            return
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            # 1. Snapshot per-agent prompt versions from the read-only main DB.
            from agent_matrix.models import get_db
            with get_db() as mdb:
                rows = mdb.execute(
                    "SELECT identifier, system_prompt, updated_at FROM public.agents"
                ).fetchall()
            for r in rows:
                digest = hashlib.sha256(
                    str(r['system_prompt']).encode('utf-8')
                ).hexdigest()
                # 真实版本号：该 agent 既有 hash 数 +1（prompt 变更即产生新版本行）
                seq = conn.execute(
                    "SELECT COUNT(*) AS n FROM prompt_metrics WHERE agent_id = ?",
                    (r['identifier'],),
                ).fetchone()
                conn.execute(
                    "INSERT INTO prompt_metrics"
                    " (agent_id, prompt_hash, prompt_version)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT (agent_id, prompt_hash) DO NOTHING",
                    (r['identifier'], digest, 'v%d' % (int(seq['n'] or 0) + 1)),
                )
            # 2. Recompute rolling metrics from reflexion_logs (last 7 days).
            conn.execute(
                "UPDATE prompt_metrics pm"
                " SET sample_count = s.n,"
                " success_count = s.ok,"
                " success_rate = CASE WHEN s.n > 0 THEN s.ok::real / s.n ELSE 0 END,"
                " avg_rating   = COALESCE(s.rt, 0),"
                " updated_at   = now()"
                " FROM ("
                " SELECT agent_id,"
                " COUNT(*) AS n,"
                " COUNT(*) FILTER (WHERE success) AS ok,"
                " AVG(rating) FILTER (WHERE rating > 0) AS rt"
                " FROM reflexion_logs"
                " WHERE created_at > now() - interval '7 days'"
                " GROUP BY agent_id"
                " ) s"
                " WHERE pm.agent_id = s.agent_id",
            )
            # 3. Round archival (Appendix C.2): close open rounds and open new ones.
            self._archive_rounds(conn)
            conn.commit()
        except Exception as e:
            logger.error('prompt evolution aggregation failed: %s', e)
            conn.rollback()
        finally:
            conn.close()

    def _archive_rounds(self, conn):
        """Close any open evolution_rounds for each agent and open new ones.

        One round = one aggregation day. No new scheduler job needed.
        """
        # Determine distinct agents with an open round (should be at most one per agent).
        open_agents = conn.execute(
            "SELECT DISTINCT agent_id FROM evolution_rounds WHERE status = 'open'"
        ).fetchall()
        for ag in open_agents:
            agent_id = ag['agent_id']
            # Close the current open round.
            conn.execute(
                "UPDATE evolution_rounds"
                " SET status = 'closed',"
                " window_end = now(),"
                " mem_count = (SELECT COUNT(*) FROM memories"
                "  WHERE agent_id = ? AND created_at >= evolution_rounds.window_start"
                "  AND created_at < now()),"
                " ref_count = (SELECT COUNT(*) FROM reflexion_logs"
                "  WHERE agent_id = ? AND created_at >= evolution_rounds.window_start"
                "  AND created_at < now()),"
                " prompt_to = COALESCE("
                "  (SELECT prompt_version FROM prompt_metrics"
                "   WHERE agent_id = ? ORDER BY updated_at DESC LIMIT 1), prompt_to)"
                " WHERE agent_id = ? AND status = 'open'",
                (agent_id, agent_id, agent_id, agent_id),
            )
            # Open a new round.
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(round_seq), 0) AS mx FROM evolution_rounds"
                " WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            seq = (max_seq['mx'] or 0) + 1
            conn.execute(
                "INSERT INTO evolution_rounds"
                " (agent_id, round_seq, status, window_start)"
                " VALUES (?, ?, 'open', now())",
                (agent_id, seq),
            )
        # If no open rounds exist at all, seed one open round per agent
        # that has any activity in the last 7 days.
        if not open_agents:
            active = conn.execute(
                "SELECT DISTINCT agent_id FROM reflexion_logs"
                " WHERE created_at > now() - interval '7 days'"
            ).fetchall()
            for ag in active:
                agent_id = ag['agent_id']
                existing = conn.execute(
                    "SELECT 1 FROM evolution_rounds WHERE agent_id = ?", (agent_id,)
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    "INSERT INTO evolution_rounds"
                    " (agent_id, round_seq, status, window_start)"
                    " VALUES (?, 1, 'open', now())",
                    (agent_id,),
                )

    def list_suggestions(self, min_sample: int = 10) -> list:
        """版本对比较：每个 agent 最近两个版本做双比例 z 检验。

        仅当两版本样本均 >= min_sample 且 |z| > 1.96（95% 置信）时
        标记 significant。返回列表按 |z| 降序。
        """
        import math
        from ..models import get_memory_engine_db
        conn = get_memory_engine_db()
        try:
            agents = conn.execute(
                "SELECT DISTINCT agent_id FROM prompt_metrics"
            ).fetchall()
            out = []
            for a in agents:
                rows = conn.execute(
                    "SELECT agent_id, prompt_hash, prompt_version, sample_count,"
                    " success_count, success_rate, avg_rating, updated_at"
                    " FROM prompt_metrics WHERE agent_id = ?"
                    " ORDER BY updated_at DESC LIMIT 2",
                    (a['agent_id'],),
                ).fetchall()
                if len(rows) < 2:
                    continue
                new, old = rows[0], rows[1]
                n1, n2 = int(old['sample_count']), int(new['sample_count'])
                if min(n1, n2) < min_sample or n1 <= 0 or n2 <= 0:
                    continue
                x1, x2 = int(old['success_count']), int(new['success_count'])
                p1, p2 = x1 / n1, x2 / n2
                pooled = (x1 + x2) / (n1 + n2)
                se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
                z = (p2 - p1) / se if se > 0 else 0.0
                out.append({
                    'agent_id': new['agent_id'],
                    'old_version': old['prompt_version'], 'old_rate': round(p1, 4),
                    'new_version': new['prompt_version'], 'new_rate': round(p2, 4),
                    'delta': round(p2 - p1, 4), 'z': round(z, 3),
                    'significant': abs(z) > 1.96,
                    'samples': [n1, n2],
                })
            out.sort(key=lambda s: abs(s['z']), reverse=True)
            return out
        finally:
            conn.close()
