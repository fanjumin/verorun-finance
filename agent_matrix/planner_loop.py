#!/usr/bin/env python3
"""Agent Matrix -- 长程自主循环 / 动态规划器 (PlannerLoop)

把 orchestrator 的「单次分解 -> 并行下发 -> 汇总」升级为
plan -> execute -> observe -> replan 的迭代循环，带轮数与预算护栏。

设计约束（v1.1 §5；2026-09-23 定案）：
  1. 开关默认关闭。是否构造本类由调用方（orchestrator）分支决定；
     本模块不在 import 期产生任何副作用，也不自行拦截调用。
  2. 只复用 orchestrator 既有方法（decompose_task / dispatch_sub_tasks /
     _add_task_log），不自造分解与下发逻辑。
  3. 观察压缩自建轻量实现，刻意不复用 orchestrator 的历史摘要方法 ——
     后者会以 session_id 为键写会话摘要缓存，拿它压缩执行观察会污染
     真实会话摘要，属跨请求的数据串味。
  4. blocked（模块策略 / 配额拦截）不触发 replan：策略性拒绝靠重规划解决不了，
     只会成倍放大成本。
  5. 全异常 fail-open：内部任何异常都不外抛，收敛并返回已完成轮次的结果。
  6. 返回累计的 (decomposed, sub_results, stats)，保持与 orchestrator 既有
     汇总逻辑的等长配对契约（其汇总按 zip(decomposed, sub_results) 渲染）。
"""
import logging
import time

from agent_matrix.context_manager import estimate_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# 护栏常量
# ---------------------------------------------------------------
REPLAN_DEFAULTS = {
    'agent_replan_enabled': 0,
    'agent_replan_max_iterations': 3,
}
_CONFIG_KEYS = ('agent_replan_enabled', 'agent_replan_max_iterations')

MAX_ITERATIONS_CEILING = 10     # 配置误填保护：单次请求最多规划轮数
MAX_ITERATIONS_FLOOR = 1
ROUND_WALL_TIMEOUT_S = 600      # 轮级墙钟护栏：仅阻止进入下一轮
OBSERVE_MAX_TOKENS = 2000       # 单轮观察压缩上限（估算 token）
LOOP_LOG_TYPE = 'planner_loop'  # 任务日志类型标识，全链路可追溯

STOP_CONVERGED = 'converged'
STOP_MAX_ITERATIONS = 'max_iterations'
STOP_ROUND_TIMEOUT = 'round_timeout'
STOP_TOKEN_BUDGET = 'token_budget'
STOP_EMPTY_PLAN = 'empty_plan'
STOP_DECOMPOSE_ERROR = 'decompose_error'
STOP_DISPATCH_ERROR = 'dispatch_error'


# ---------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------
def get_replan_config() -> dict:
    """读取 system_config 中的迭代规划配置，缺失或异常回退默认值。

    读取与降级范式对齐 agent_matrix/context_manager.py 的 get_context_config：
    键不存在、值为空、类型非法、DB 不可达，一律退回默认值（默认即关闭）。
    """
    cfg = dict(REPLAN_DEFAULTS)
    try:
        from models import get_db as _main_get_db
        placeholders = ','.join(['%s'] * len(_CONFIG_KEYS))
        sql = ('SELECT key, value FROM system_config WHERE key IN (%s)'
               % placeholders)
        with _main_get_db() as conn:
            rows = conn.execute(sql, _CONFIG_KEYS).fetchall()
        for r in (rows or []):
            key = r['key'] if not isinstance(r, tuple) else r[0]
            val = r['value'] if not isinstance(r, tuple) else r[1]
            if key not in cfg or val is None or str(val).strip() == '':
                continue
            try:
                cfg[key] = int(val)
            except (ValueError, TypeError):
                logger.warning('[PlannerLoop] bad value for %s=%r, keep default', key, val)
    except Exception as e:
        logger.warning('[PlannerLoop] read config failed, use defaults: %s', e)
    return cfg


def is_replan_enabled() -> bool:
    """迭代规划开关；读取失败按关闭处理，与引入前行为一致。"""
    try:
        return int(get_replan_config().get('agent_replan_enabled') or 0) == 1
    except Exception:
        return False


def get_max_iterations() -> int:
    """最大规划轮数；读失败取默认，越界值收敛到 [1, 10] 防成本失控。

    这里刻意不用 `取值 or 默认值` 的写法：显式配置 0 是合法输入（应被下界
    收敛为 1），而 `0 or 3` 会把它误判成"缺省"，静默改写用户配置。
    """
    raw = None
    try:
        raw = get_replan_config().get('agent_replan_max_iterations')
    except Exception:
        raw = None
    if raw is None:
        raw = REPLAN_DEFAULTS['agent_replan_max_iterations']
    try:
        n = int(raw)
    except (ValueError, TypeError):
        n = REPLAN_DEFAULTS['agent_replan_max_iterations']
    return max(MAX_ITERATIONS_FLOOR, min(MAX_ITERATIONS_CEILING, n))


def _loop_token_budget() -> int:
    """循环累计观察的 token 护栏；0 表示不启用该护栏。

    复用 WP-A 的单次请求预算作为观察累计上限：单轮观察已压到
    OBSERVE_MAX_TOKENS，正常轮数下远达不到上限，该护栏仅用于兜底。
    """
    try:
        from agent_matrix.context_manager import get_context_budget
        return int(get_context_budget() or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------
# 观察压缩（轻量、只读、不写任何缓存）
# ---------------------------------------------------------------
def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按估算 token 上限截断文本。

    短文本原样返回；超限时先按比例缩字符数，再循环复核（CJK 与 ASCII 混排时
    等比换算有偏差），仍失败则退化为按 token 数取字符数上限。
    """
    text = text or ''
    if max_tokens <= 0:
        return ''
    try:
        total = estimate_tokens(text)
        if total <= max_tokens:
            return text
        cut = max(1, int(len(text) * (max_tokens / float(total or 1))))
        out = text[:cut]
        for _ in range(8):
            if estimate_tokens(out) <= max_tokens:
                break
            out = out[:max(1, int(len(out) * 0.85))]
        return out
    except Exception:
        return text[:max_tokens]


# ---------------------------------------------------------------
# 迭代规划器
# ---------------------------------------------------------------
class PlannerLoop:
    """迭代规划器：plan -> execute -> observe -> replan。

    调用形态（批 5 将在 orchestrator 内接入）：

        if is_replan_enabled():
            decomposed, sub_results, stats = PlannerLoop(
                self, master_task_id, master_agent_id, master_config,
                session_id=session_id, user_id=user_id, mode=mode,
            ).run(instruction)
        else:
            ... 原单次分解 / 下发 / 汇总路径保持不变 ...

    收敛条件（任一命中即停）：
      - 本轮无 failed（blocked 不计入，见设计约束 4）
      - 达到 max_iterations
      - 观察累计 token 触及护栏
      - 单轮墙钟超过 ROUND_WALL_TIMEOUT_S
      - 分解返回空、或分解 / 下发抛异常（fail-open 收敛）
    """

    def __init__(self, orchestrator, master_task_id, master_agent_id, master_config,
                 session_id=None, user_id=0, mode='', max_iterations=None):
        self.orch = orchestrator
        self.master_task_id = master_task_id
        self.master_agent_id = master_agent_id
        self.master_config = master_config or {}
        self.session_id = session_id
        self.user_id = user_id
        self.mode = mode or ''
        if max_iterations is None:
            self.max_iterations = get_max_iterations()
        else:
            try:
                self.max_iterations = int(max_iterations)
            except (ValueError, TypeError):
                self.max_iterations = get_max_iterations()
        self.max_iterations = max(MAX_ITERATIONS_FLOOR,
                                  min(MAX_ITERATIONS_CEILING, self.max_iterations))

    # -- 对外入口 -------------------------------------------------
    def run(self, instruction):
        """执行迭代规划。返回 (decomposed, sub_results, stats)。

        decomposed / sub_results 为**各轮累计**且等长配对，调用方可直接沿用
        既有汇总逻辑渲染；stats 供调用方增强报告与审计。
        """
        started = time.time()
        all_decomposed = []
        all_results = []
        rounds = []
        stop_reason = None

        try:
            token_budget = _loop_token_budget()
        except Exception:
            token_budget = 0
        observe_tokens = 0
        replan_instruction = instruction

        for iteration in range(1, self.max_iterations + 1):
            round_started = time.time()

            # 轮级护栏：只在进入新一轮前判定，不打断正在执行的子任务
            #（子任务超时由 dispatch_sub_tasks 既有的 300s/任务兜底）。
            if iteration > 1 and (round_started - started) > ROUND_WALL_TIMEOUT_S:
                stop_reason = STOP_ROUND_TIMEOUT
                break

            try:
                decomposed = self.orch.decompose_task(replan_instruction, self.master_config)
            except Exception as e:
                self._log('error', 'iteration %d decompose failed: %s' % (iteration, e))
                stop_reason = STOP_DECOMPOSE_ERROR
                break

            if not decomposed:
                stop_reason = STOP_EMPTY_PLAN
                break

            try:
                results = self.orch.dispatch_sub_tasks(
                    decomposed, self.master_task_id, self.session_id,
                    original_instruction=instruction,
                    user_id=self.user_id, mode=self.mode,
                )
            except Exception as e:
                self._log('error', 'iteration %d dispatch failed: %s' % (iteration, e))
                stop_reason = STOP_DISPATCH_ERROR
                break

            # 结果条目规范化：非 dict 一律视为失败条目，保持与 decomposed 等长配对。
            # 下发结果可能来自插件 / MCP / 线程池异常分支，形态不完全可控，
            # 此处兜底而不外抛，符合 fail-open 契约。
            results = [
                r if isinstance(r, dict) else {
                    'sub_task_id': None, 'status': 'failed', 'title': '',
                    'error': 'malformed result: %r' % (r,),
                }
                for r in (results or [])
            ]
            all_decomposed.extend(decomposed)
            all_results.extend(results)

            failed = [r for r in results if r.get('status') == 'failed']
            blocked = [r for r in results if r.get('status') == 'blocked']
            completed = [r for r in results if r.get('status') == 'completed']

            rounds.append({
                'iteration': iteration,
                'subtasks': len(decomposed),
                'completed': len(completed),
                'failed': len(failed),
                'blocked': len(blocked),
                'duration_s': round(time.time() - round_started, 2),
            })

            self._log('info',
                      'iteration %d/%d: subtasks=%d completed=%d failed=%d blocked=%d'
                      % (iteration, self.max_iterations, len(decomposed),
                         len(completed), len(failed), len(blocked)))

            if not failed:
                stop_reason = STOP_CONVERGED
                break

            if iteration >= self.max_iterations:
                stop_reason = STOP_MAX_ITERATIONS
                break

            observation = self._build_observation(failed, results)
            observe_tokens += estimate_tokens(observation)
            if token_budget > 0 and observe_tokens >= token_budget:
                self._log('info', 'observation token budget exhausted (%d/%d), stop'
                          % (observe_tokens, token_budget))
                stop_reason = STOP_TOKEN_BUDGET
                break

            replan_instruction = self._build_replan_instruction(instruction, observation)

        if stop_reason is None:
            stop_reason = STOP_CONVERGED

        stats = {
            'iterations': len(rounds),
            'max_iterations': self.max_iterations,
            'rounds': rounds,
            'stop_reason': stop_reason,
            'observation_tokens': observe_tokens,
            'duration_s': round(time.time() - started, 2),
        }
        self._log('info', 'planner loop stopped: reason=%s iterations=%d duration=%ss'
                  % (stop_reason, len(rounds), stats['duration_s']))
        return all_decomposed, all_results, stats

    # -- 内部方法 -------------------------------------------------
    def _build_observation(self, failed, results):
        """构造轮间观察：失败项（标题 + 错误摘要）+ 已完成清单。

        只做文本截断，不调 LLM、不读写任何缓存。
        """
        lines = []
        for r in failed:
            err = str(r.get('error') or r.get('response') or '').strip()
            lines.append('- [failed] %s :: %s' % (r.get('title', ''), err[:300]))
        done = [str(r.get('title', '')) for r in results
                if r.get('status') == 'completed' and r.get('title')]
        if done:
            lines.append('- [done] ' + '; '.join(done))
        return _truncate_to_tokens('\n'.join(lines), OBSERVE_MAX_TOKENS)

    def _build_replan_instruction(self, instruction, observation):
        """把原指令 + 执行观察拼成下一轮分解输入。

        decompose_task 只接受一个 instruction 串并在内部套模板，故观察以
        <执行观察> 段落附在其后，并显式要求不重复规划已完成任务。
        """
        return (
            '%s\n\n<执行观察>\n%s\n</执行观察>\n'
            '以上是上一轮执行的实际结果。请只规划【剩余或需要修正】的任务，'
            '已完成的任务不要重复规划；若无需继续，输出空 tasks 数组。'
            % (instruction, observation or '(no detail)')
        )

    def _log(self, level, message):
        """写任务日志；失败静默（与 orchestrator._add_task_log 同款容错）。"""
        try:
            self.orch._add_task_log(
                self.master_task_id, self.master_agent_id, level, LOOP_LOG_TYPE, message
            )
        except Exception:
            pass
        try:
            logger.info('[PlannerLoop] %s', message)
        except Exception:
            pass
