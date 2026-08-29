#!/usr/bin/env python3
"""
Health Check — AI Fixer
========================
LLM-powered analysis engine for health check results.

Flow:
  1. Collect check results (errors, warnings, fix suggestions)
  2. Build a system prompt with analysis context
  3. Call LLM via AIEngine -> return structured repair plan
  4. Execute fixes (with admin confirmation)

Uses the project's AIEngine (agent_matrix/engine.py) which supports
providers available in China: DashScope, DeepSeek, SiliconFlow, etc.
Defaults to the 'cleaner_ai' config from system_config.
"""

import json
import os
import sys
from typing import Optional

# Ensure project path is accessible
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(BASE_DIR, '..', 'auth-center'))
sys.path.append(os.path.join(BASE_DIR, '..'))

try:
    from plugin_manager.logger import get_plugin_logger
    _logger = get_plugin_logger('health_check')
except ImportError:
    import logging
    _logger = logging.getLogger('health_check')

from .checkers import (
    FixSuggestion,
    FIX_ACTION_NOTIFY_ADMIN,
    WHITELIST_FIX_ACTIONS,
    ALL_FIX_ACTIONS,
)


# ─── LLM Configuration via AIEngine ───────────────────────────────────

def _build_aiengine_config() -> dict:
    """
    Build an AIEngine-compatible config dict from system_config.

    Reads cleaner_ai_* keys, falls back to:
      - provider-specific api_key (e.g. deepseek_api_key)
      - environment variable (e.g. DEEPSEEK_API_KEY)
    """
    config = {
        'provider': 'deepseek',
        'model_name': '',
        'base_url': '',
        'system_prompt': '',
    }

    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT key, value FROM system_config WHERE key IN "
                "('cleaner_ai_provider', 'cleaner_ai_model', "
                "'cleaner_ai_base_url', 'cleaner_ai_api_key')"
            ).fetchall()
            for r in rows:
                key = r['key']
                val = r['value']
                if key == 'cleaner_ai_provider' and val:
                    config['provider'] = val
                elif key == 'cleaner_ai_model' and val:
                    config['model_name'] = val
                elif key == 'cleaner_ai_base_url' and val:
                    config['base_url'] = val

        conn.close()
    except Exception as e:
        _logger.warning("Failed to read AIEngine config from DB: %s", e)

    # Fallback: if model_name is still empty, query AI Hub for active model
    if not config['model_name']:
        try:
            from models.database import get_active_model
            _, model_name, base_url = get_active_model(config['provider'])
            if model_name:
                config['model_name'] = model_name
            if base_url:
                config['base_url'] = config['base_url'] or base_url
        except Exception as e:
            _logger.warning("Failed to get active model from AI Hub: %s", e)

    return config


def _call_llm(system_prompt: str, user_prompt: str,
              temperature: float = 0.3) -> Optional[str]:
    """
    Call LLM via AIEngine (agent_matrix/engine.py).

    AIEngine handles API key resolution across all supported providers,
    including DashScope, DeepSeek, SiliconFlow (all accessible in China).
    """
    # AI 费用闸门：日预算熔断 + 速率限制，超限则拒绝本次调用
    try:
        from agent_matrix.engine import check_ai_budget
        allowed, reason = check_ai_budget(scene='health_ai_fixer')
        if not allowed:
            _logger.warning("[AIFixer] blocked by AI budget guard: %s", reason)
            return None
    except Exception as e:
        _logger.warning("[AIFixer] budget guard unavailable, proceeding: %s", e)

    engine_config = _build_aiengine_config()

    try:
        from agent_matrix.engine import UnifiedLLM
        engine = UnifiedLLM(engine_config)
    except ImportError:
        _logger.error("agent_matrix.engine.UnifiedLLM not available")
        return None
    except Exception as e:
        _logger.error("Failed to initialize UnifiedLLM: %s", e)
        return None

    if not engine.is_ready():
        _logger.error("UnifiedLLM not ready (missing API key)")
        return None

    try:
        resp = engine.chat(
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            temperature=temperature,
            max_tokens=8192,
            module='health_check',
        )
        return resp
    except ValueError as e:
        # 模型解析失败（cleaner_ai_model 指向已删除的模型 / AI Hub 无活跃模型）。
        # 用 AI Hub 的活跃模型（provider_model_id 方式）重试一次，避免静默返回空结果。
        _logger.warning("[AIFixer] model resolution failed, retrying with AI Hub active model: %s", e)
        try:
            from models.database import get_active_model
            pm_id, pm_model, pm_base_url = get_active_model(engine_config.get('provider', 'deepseek'))
            if pm_id and pm_model:
                retry_engine = UnifiedLLM({
                    'provider': engine_config.get('provider', 'deepseek'),
                    'model_name': pm_model,
                    'base_url': pm_base_url or engine_config.get('base_url', ''),
                    'provider_model_id': pm_id,
                    'system_prompt': '',
                })
                return retry_engine.chat(
                    messages=[
                        {'role': 'system', 'content': system_prompt},
                        {'role': 'user', 'content': user_prompt},
                    ],
                    provider_model_id=pm_id,
                    temperature=temperature,
                    max_tokens=8192,
                    module='health_check',
                )
        except Exception as e2:
            _logger.error("[AIFixer] retry with AI Hub active model failed: %s", e2)
        return None
    except Exception as e:
        _logger.error("LLM call via UnifiedLLM failed: %s", e)
        return None


# ─── Prompt Templates ────────────────────────────────────────────────────

FIXER_SYSTEM_PROMPT = """You are a senior site reliability engineer for the VeroRun system.

Your job is to analyze health check results and produce a structured repair plan.
You will receive JSON containing health check results with various issues.

Analyze each issue and return a JSON object with the following structure:

{
  "summary": "Brief summary of all issues found",
  "items": [
    {
      "check_key": "The check item key (e.g. internal_links, media_integrity)",
      "issue": "Description of the specific problem",
      "root_cause": "Analysis of what caused this issue",
      "action": "One of: update_url / mark_disabled / run_sql / notify_admin",
      "params": {
        // Action-specific parameters (see below)
      },
      "priority": "high/medium/low",
      "reason": "Why this fix is recommended"
    }
  ]
}

Action parameter formats:
- update_url:  {"table": "table_name", "record_id": 123, "field": "url_column", "new_value": "https://new-url.com"}
- mark_disabled: {"table": "table_name", "record_id": 123}
- run_sql: {"sql": "UPDATE table SET field='value' WHERE id=%s", "params": [123]}
Note: run_sql is restricted to non-destructive statements (SELECT / UPDATE / DELETE).
      UPDATE/DELETE MUST include a WHERE clause. Never suggest DROP/ALTER/TRUNCATE/
      CREATE/GRANT/REVOKE or any other destructive statement — they will be rejected.
Note: The database is PostgreSQL — always use %s placeholder (NOT ?) for parameters.
- notify_admin: {"message": "Alert message", "level": "warning/critical"}

IMPORTANT: The input JSON includes a "status" field ("passed"/"warning"/"error").
If status is "warning" or "error", there ARE real issues that need fixes.
Do NOT ignore warning/error status — analyze the detail data and suggest appropriate actions.

Rules:
1. Only suggest fixes for clearly identified problems
2. For broken links (404/410), suggest mark_disabled or update_url if you know the correct URL
3. For redirect chains, suggest update_url to the final destination
4. For server resources (disk/memory), suggest run_sql to clean old logs/cache, or notify_admin
5. For database issues, suggest run_sql with appropriate repair queries
6. If unsure, mark as notify_admin
7. BE CONSERVATIVE — do not suggest destructive actions without strong evidence
"""

GLOBAL_SYSTEM_PROMPT = """You are a senior site reliability engineer for the VeroRun system.

Your job is to analyze the LATEST full health check run (all checkers at once)
and produce a GLOBAL root-cause analysis across all items.

You will receive JSON containing:
- "run_id": the health check run id
- "run_type": quick/standard/manual
- "results": an array of check results, each with:
    check_key, status ("passed"/"warning"/"error"), message, detail

Analyze the WHOLE set and return a JSON object with:

{
  "summary": "One-paragraph overall health summary and the top priority issues",
  "health_score": 0-100 (estimated from passed/total),
  "critical_issues": ["list of check_keys that are error"],
  "root_causes": [
    {
      "check_key": "the check item key (e.g. database, redis)",
      "issue": "Description of the specific problem",
      "root_cause": "Analysis of what caused this issue (e.g. redis not installed on OS)",
      "action": "One of: update_url / mark_disabled / run_sql / notify_admin",
      "params": {},
      "priority": "high/medium/low",
      "reason": "Why this fix is recommended"
    }
  ]
}

Action parameter formats:
- update_url:  {"table": "table_name", "record_id": 123, "field": "url_column", "new_value": "https://new-url.com"}
- mark_disabled: {"table": "table_name", "record_id": 123}
- run_sql: {"sql": "UPDATE table SET field='value' WHERE id=%s", "params": [123]}
Note: run_sql is restricted to non-destructive statements (SELECT / UPDATE / DELETE).
      UPDATE/DELETE MUST include a WHERE clause. Never suggest DROP/ALTER/TRUNCATE/
      CREATE/GRANT/REVOKE or any other destructive statement — they will be rejected.
Note: The database is PostgreSQL — always use %s placeholder (NOT ?) for parameters.
- notify_admin: {"message": "Alert message", "level": "warning/critical"}

IMPORTANT:
- Only status "warning"/"error" items indicate real issues. Analyze their detail data
  and suggest appropriate actions. Do NOT invent issues for "passed" items.
- For redis: if detail/error message indicates connection refused or client unavailable,
  the likely root cause is redis-server not installed on the OS — suggest notify_admin
  with an install instruction, or run_sql is NOT applicable.
- For database: if detail contains "connections"/"max_connections", assess connection
  pressure and suggest notify_admin if usage exceeds 80%.
- BE CONSERVATIVE — do not suggest destructive actions without strong evidence.
"""


# ─── AIFixer Class ───────────────────────────────────────────────────────

class AIFixer:
    """
    LLM-powered fix analysis engine.

    Uses AIEngine from Agent Matrix to support all providers
    (DashScope, DeepSeek, SiliconFlow, OpenAI, etc.).

    Usage:
        fixer = AIFixer()
        plan = fixer.analyze(check_results)
        # Review plan, then:
        results = fixer.execute_fix(plan, conn)
    """

    def analyze(self, check_results: dict) -> dict:
        """
        Analyze health check results and return a repair plan.

        check_results should be a dict with at minimum:
            {'check_key': str, 'status': str, 'message': str, 'detail': dict}
        Global mode (analyze the full run at once):
            {'run_id': int, 'run_type': str, 'results': [check_result, ...]}
        """

        # ── 全局模式：多检查项一次分析 ──────────────────────────────────
        if 'results' in check_results and isinstance(check_results.get('results'), list):
            results = check_results['results']
            for item in results:
                if not isinstance(item, dict):
                    continue
                detail = item.get('detail')
                if isinstance(detail, dict):
                    # 精简大字段，避免 LLM 上下文溢出
                    slim = {}
                    for k, v in detail.items():
                        v_str = json.dumps(v, ensure_ascii=False)
                        if isinstance(v, list) and len(v_str) > 300:
                            slim[k] = f"[{len(v)} items, truncated]"
                        elif isinstance(v, dict):
                            slim[k] = v
                        elif len(v_str) > 400:
                            slim[k] = v_str[:400] + '... [truncated]'
                        else:
                            slim[k] = v
                    item['detail'] = slim
            user_prompt = json.dumps(check_results, ensure_ascii=False, indent=2)
            response_text = _call_llm(GLOBAL_SYSTEM_PROMPT, user_prompt)
            if not response_text:
                return {
                    'summary': 'LLM analysis failed',
                    'items': [],
                    '_error': 'LLM call failed or blocked (AI budget / model resolution / API error). Check plugin logs for details.',
                }

            import re
            fence = re.search(r'```(?:json)?\s*(.*?)\s*```', response_text, re.DOTALL)
            if fence:
                response_text = fence.group(1)
            try:
                plan = json.loads(response_text)
            except json.JSONDecodeError:
                match = re.search(r'\{(?:[^{}]|(?:\{[^{}]*\}))*\}', response_text, re.DOTALL)
                if match:
                    try:
                        plan = json.loads(match.group())
                    except json.JSONDecodeError:
                        plan = {'summary': 'Failed to parse LLM response', 'items': [],
                                '_error': 'LLM response was not valid JSON.'}
                else:
                    plan = {'summary': 'Failed to parse LLM response', 'items': [],
                            '_error': 'LLM response was not valid JSON.'}
            if 'root_causes' in plan:
                plan.setdefault('items', plan.get('root_causes', []))
            return plan

        # ── 单检查项模式（原逻辑） ──────────────────────────────────────
        # Truncate detail if too large to avoid LLM timeout/context overflow
        full_json = json.dumps(check_results, ensure_ascii=False, indent=2)
        if len(full_json) > 6000:
            detail = check_results.get('detail', {})
            if isinstance(detail, dict):
                # Keep detail structure but replace large values with truncated text
                truncated_detail = {}
                for k, v in detail.items():
                    v_str = json.dumps(v, ensure_ascii=False)
                    if isinstance(v, list):
                        truncated_detail[k] = f"[{len(v)} items, showing first 3]"
                        truncated_detail[k + '_sample'] = v[:3]
                    elif isinstance(v, dict):
                        truncated_detail[k] = v  # keep small dicts
                    elif len(v_str) > 500:
                        truncated_detail[k] = v_str[:500] + '... [truncated]'
                    else:
                        truncated_detail[k] = v
                check_results['detail'] = truncated_detail
                check_results['_detail_truncated'] = True
            user_prompt = json.dumps(check_results, ensure_ascii=False, indent=2)
        else:
            user_prompt = full_json

        response_text = _call_llm(FIXER_SYSTEM_PROMPT, user_prompt)
        if not response_text:
            return {
                'summary': 'LLM analysis failed',
                'items': [],
                '_error': 'LLM call failed or blocked (AI budget / model resolution / API error). Check plugin logs for details.',
            }

        import re
        # 剥离 markdown 代码围栏（```json ... ```），避免 json.loads 直接失败
        fence = re.search(r'```(?:json)?\s*(.*?)\s*```', response_text, re.DOTALL)
        if fence:
            response_text = fence.group(1)

        try:
            plan = json.loads(response_text)
        except json.JSONDecodeError:
            match = re.search(r'\{(?:[^{}]|(?:\{[^{}]*\}))*\}', response_text, re.DOTALL)
            if match:
                try:
                    plan = json.loads(match.group())
                except json.JSONDecodeError:
                    plan = {
                        'summary': 'Failed to parse LLM response',
                        'items': [],
                        '_error': 'LLM response was not valid JSON (may have been truncated by max_tokens).',
                    }
            else:
                plan = {
                    'summary': 'Failed to parse LLM response',
                    'items': [],
                    '_error': 'LLM response was not valid JSON (may have been truncated by max_tokens).',
                }

        # Normalize: if LLM returned a flat action object (no 'items' array),
        # wrap it into items
        if 'items' not in plan and 'action' in plan:
            plan = {'items': [plan], 'summary': plan.get('reason', '')}

        return plan

    def suggestions_from_plan(self, plan: dict) -> list:
        """Convert a LLM repair plan into FixSuggestion objects."""
        suggestions = []
        for item in plan.get('items', []):
            action = item.get('action', '')
            if action not in ALL_FIX_ACTIONS:
                continue
            suggestions.append(FixSuggestion(
                action=action,
                reason=item.get('reason', ''),
                params=item.get('params', {}),
                record_type=item.get('check_key', ''),
            ))
        return suggestions

    def execute_fix(self, conn, suggestions: list, auto_exec: bool = False,
                    admin_user: str = 'system', run_id: int = 0,
                    check_key: str = '') -> dict:
        """
        Execute a list of FixSuggestion objects.

        If auto_exec=True, only whitelist actions are executed automatically;
        all other actions are skipped.

        Each applied fix is recorded in fix_audit_log for rollback support.

        Returns stats dict: {applied: int, errors: list}
        """
        import json as _json
        applied = 0
        errors = []

        for sug in suggestions:
            # Auto-exec mode: only whitelist actions
            if auto_exec and sug.action not in WHITELIST_FIX_ACTIONS:
                _logger.info('Skipped (not in whitelist): %s', sug.action)
                continue

            # Build undo_params for rollback
            undo_params = self._build_undo_params(conn, sug)

            try:
                if sug.action == FIX_ACTION_NOTIFY_ADMIN:
                    if conn:
                        msg = sug.params.get('message', sug.reason)
                        level = sug.params.get('level', 'warning')
                        conn.execute(
                            "INSERT INTO alerts (type, message, severity, created_at) "
                            "VALUES ('auto_remediation', %s, %s, NOW())",
                            (msg, level)
                        )
                    applied += 1
                else:
                    ok = FixSuggestion.apply_fix(conn, sug)
                    if ok:
                        applied += 1
                    else:
                        errors.append(f"{sug.action}: could not be applied")
                        continue

                # Record audit log
                if conn:
                    conn.execute(
                        'INSERT INTO fix_audit_log '
                        '(run_id, check_key, action, params_json, undo_params_json, status, admin_user) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (run_id, check_key, sug.action,
                         _json.dumps(sug.params, ensure_ascii=False),
                         _json.dumps(undo_params, ensure_ascii=False),
                         'applied', admin_user)
                    )

            except Exception as e:
                errors.append(f"{sug.action}: {e}")

        return {
            'applied': applied,
            'total': len(suggestions),
            'errors': errors,
        }

    def _build_undo_params(self, conn, sug: FixSuggestion) -> dict:
        """Build the undo_params for a given FixSuggestion.
        
        This captures the current state before the fix is applied,
        so it can be reversed later.
        """
        import re
        params = sug.params
        undo = {}

        # 安全校验：LLM 输出的 table/field 必须只含合法标识符字符，防止 SQL 注入
        _SAFE_IDENTIFIER = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
        table = params.get('table', '')
        field = params.get('field', '')
        if table and not _SAFE_IDENTIFIER.match(table):
            return {'note': f'Invalid table name rejected: {table}'}
        if field and not _SAFE_IDENTIFIER.match(field):
            return {'note': f'Invalid field name rejected: {field}'}

        if sug.action == 'set_log_level' and conn:
            old = conn.execute(
                "SELECT value FROM system_config WHERE key='log_level'"
            ).fetchone()
            undo['old_level'] = old['value'] if old else 'info'

        elif sug.action == 'clean_temp':
            undo['note'] = 'Files deleted from disk; rollback is best-effort (restore from backup)'

        elif sug.action == 'restart_worker':
            undo['worker_name'] = params.get('worker_name', '')
            undo['note'] = 'Worker already restarted; no undo for process signals'

        elif sug.action == 'flush_cdn':
            undo['note'] = 'CDN cache already flushed; no undo'

        elif sug.action == 'update_url' and conn and 'table' in params and 'record_id' in params and 'field' in params:
            try:
                old_val = conn.execute(
                    f"SELECT {params['field']} FROM {params['table']} WHERE id=%s",
                    (params['record_id'],)
                ).fetchone()
                if old_val:
                    undo['old_value'] = old_val[0]
                    undo['table'] = params['table']
                    undo['record_id'] = params['record_id']
                    undo['field'] = params['field']
            except Exception:
                undo['note'] = f"Table {params['table']} not accessible in current schema"

        elif sug.action == 'mark_disabled' and conn and 'table' in params and 'record_id' in params:
            try:
                old_val = conn.execute(
                    f"SELECT is_enabled, is_active FROM {params['table']} WHERE id=%s",
                    (params['record_id'],)
                ).fetchone()
                if old_val:
                    undo['old_is_enabled'] = old_val[0]
                    undo['old_is_active'] = old_val[1]
                    undo['table'] = params['table']
                    undo['record_id'] = params['record_id']
            except Exception:
                undo['note'] = f"Table {params['table']} not accessible in current schema"

        elif sug.action == 'mark_deleted' and conn and 'table' in params and 'record_id' in params:
            try:
                old_val = conn.execute(
                    f"SELECT status FROM {params['table']} WHERE id=%s",
                    (params['record_id'],)
                ).fetchone()
                if old_val:
                    undo['old_status'] = old_val[0]
                    undo['table'] = params['table']
                    undo['record_id'] = params['record_id']
            except Exception:
                undo['note'] = f"Table {params['table']} not accessible in current schema"

        elif sug.action == 'clear_field' and conn and 'table' in params and 'record_id' in params and 'field' in params:
            try:
                old_val = conn.execute(
                    f"SELECT {params['field']} FROM {params['table']} WHERE id=%s",
                    (params['record_id'],)
                ).fetchone()
                if old_val:
                    undo['old_value'] = old_val[0]
                    undo['table'] = params['table']
                    undo['record_id'] = params['record_id']
                    undo['field'] = params['field']
            except Exception:
                undo['note'] = f"Table {params['table']} not accessible in current schema"

        return undo


# ─── Convenience function ────────────────────────────────────────────────

def analyze_and_fix(check_results: dict, conn) -> dict:
    """
    One-shot: analyze check results with LLM via AIEngine, then execute fixes.
    Returns full result dict.
    """
    fixer = AIFixer()
    plan = fixer.analyze(check_results)
    suggestions = fixer.suggestions_from_plan(plan)
    result = fixer.execute_fix(conn, suggestions)
    return {
        'plan': plan,
        'execution': result,
    }
