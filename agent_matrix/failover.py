#!/usr/bin/env python3
"""
Agent Matrix — 兜底引擎（failover.py）
======================================
在所有配置的主模型全部失效时，自动切换兜底模型，保证基础会话/系统诊断/错误反馈可用。

组件：
  CircuitBreaker    进程内熔断器（连续失败 N 次开闸 → 冷却 → half-open 试探）
  ModelHealthStore  健康状态持久化（ai_model_health / ai_model_failover_events）
  FallbackEngine    兜底候选加载 + call_with_failover / stream_with_failover 编排

设计约束：
  - 零第三方依赖（标准库 + 项目既有 get_db）
  - DB 异常一律静默降级为进程内状态，监控绝不影响主链路
  - 全部延迟导入，避免与 engine.py 循环依赖
"""
import json
import logging
import threading
import time as _time

logger = logging.getLogger('failover')

# ── 熔断默认参数（可经 system_config 覆盖：ai_fb_threshold / ai_fb_cooldown）──
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 300
DEFAULT_BACKOFF_FACTOR = 2
DEFAULT_MAX_COOLDOWN = 3600
# 兜底候选默认不内置硬编码——生产环境无 key 的兜底只会制造噪音与延迟（D-1），
# 需通过 system_config ai_fallback_models 显式配置（load_fallback_models 兼容）。
BUILTIN_FALLBACKS = []


def _cfg_key(provider, model_name):
    return f'{provider}::{model_name}'


def _sys_key(key):
    """读 system_config（静默失败）"""
    try:
        from models import get_db
        with get_db() as conn:
            row = conn.execute('SELECT value FROM system_config WHERE key=%s', (key,)).fetchone()
        return (row['value'] or '') if row else ''
    except Exception as e:
        logger.warning(f'[failover] read system_config {key} failed: {e}')
        return ''


def _now_str(ts):
    if not ts:
        return None
    return _time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime(ts))


def _send_alert(message):
    """发送兜底引擎告警（延迟导入 + 异常静默，绝不影响主链路）"""
    try:
        from plugins.health_check.alerter import send_notification
        send_notification('internal', message, {})
    except Exception as e:
        logger.warning(f'[failover] send_notification failed: {e}')


class CircuitBreaker:
    """进程内熔断器：key = 'provider::model'"""

    def __init__(self, failure_threshold=None, cooldown_seconds=None):
        self._threshold = failure_threshold or DEFAULT_FAILURE_THRESHOLD
        self._cooldown = cooldown_seconds or DEFAULT_COOLDOWN_SECONDS
        self._states = {}
        self._lock = threading.Lock()

    def _get(self, key):
        st = self._states.get(key)
        if st is None:
            st = {'failures': 0, 'open': False, 'cooldown_until': 0.0, 'open_count': 0}
            self._states[key] = st
        return st

    def is_available(self, key):
        with self._lock:
            st = self._get(key)
            return not st['open'] or _time.time() >= st['cooldown_until']

    def record_success(self, key):
        with self._lock:
            st = self._get(key)
            st['failures'] = 0
            st['open'] = False
            st['cooldown_until'] = 0.0

    def record_failure(self, key):
        with self._lock:
            st = self._get(key)
            st['failures'] += 1
            if st['failures'] >= self._threshold:
                st['open'] = True
                backoff = self._cooldown * (DEFAULT_BACKOFF_FACTOR ** st['open_count'])
                st['cooldown_until'] = _time.time() + min(backoff, DEFAULT_MAX_COOLDOWN)
                st['open_count'] += 1
                return True
            return False

    def get_status(self, key):
        with self._lock:
            st = self._get(key)
            return {'failures': st['failures'], 'open': st['open'],
                    'cooldown_until': st['cooldown_until'], 'open_count': st['open_count']}


class ModelHealthStore:
    """模型健康状态持久化（DB 异常静默降级）"""

    def record_success(self, provider, model_name, model_id=0):
        try:
            from models import get_db
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO ai_model_health
                        (model_id, provider_slug, model_name, status, consecutive_failures,
                         circuit_open, total_calls, last_success_at, last_error,
                         cooldown_until, updated_at)
                    VALUES (%s, %s, %s, 'healthy', 0, FALSE, 1, NOW(), '', NULL, NOW())
                    ON CONFLICT (provider_slug, model_name) DO UPDATE SET
                        model_id = EXCLUDED.model_id,
                        status = 'healthy',
                        consecutive_failures = 0,
                        circuit_open = FALSE,
                        total_calls = ai_model_health.total_calls + 1,
                        last_success_at = NOW(),
                        last_error = '',
                        cooldown_until = NULL,
                        updated_at = NOW()
                """, (model_id, provider, model_name))
                conn.commit()
        except Exception as e:
            logger.warning(f'[failover] record_success failed: {e}')

    def record_failure(self, provider, model_name, model_id=0, error='',
                       breaker_open=False, cooldown_until=None):
        try:
            from models import get_db
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO ai_model_health
                        (model_id, provider_slug, model_name, status, consecutive_failures,
                         circuit_open, total_calls, total_failures, last_failure_at,
                         last_error, cooldown_until, updated_at)
                    VALUES (%s, %s, %s, %s, 1, %s, 1, 1, NOW(), %s, %s, NOW())
                    ON CONFLICT (provider_slug, model_name) DO UPDATE SET
                        model_id = EXCLUDED.model_id,
                        status = EXCLUDED.status,
                        consecutive_failures = ai_model_health.consecutive_failures + 1,
                        circuit_open = EXCLUDED.circuit_open,
                        total_calls = ai_model_health.total_calls + 1,
                        total_failures = ai_model_health.total_failures + 1,
                        last_failure_at = NOW(),
                        last_error = EXCLUDED.last_error,
                        cooldown_until = COALESCE(EXCLUDED.cooldown_until,
                                                  ai_model_health.cooldown_until),
                        updated_at = NOW()
                """, (model_id, provider, model_name,
                      'cooldown' if breaker_open else 'degraded',
                      breaker_open, (error or '')[:500], cooldown_until))
                conn.commit()
        except Exception as e:
            logger.warning(f'[failover] record_failure failed: {e}')

    def log_failover_event(self, from_cfg, to_cfg, reason='health', error='', request_id=''):
        try:
            from models import get_db
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO ai_model_failover_events
                        (from_model_id, from_provider, from_model,
                         to_model_id, to_provider, to_model, reason, error_text, request_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (from_cfg.get('model_id', 0) or 0, from_cfg.get('provider', '') or '',
                      from_cfg.get('model', '') or '', to_cfg.get('model_id', 0) or 0,
                      to_cfg.get('provider', '') or '', to_cfg.get('model', '') or '',
                      reason, (error or '')[:500], request_id or ''))
                conn.commit()
        except Exception as e:
            logger.warning(f'[failover] log_failover_event failed: {e}')

    def get_health(self, provider=None, model_name=None, limit=100):
        try:
            from models import get_db
            sql = ("SELECT id, model_id, provider_slug, model_name, status, "
                   "consecutive_failures, circuit_open, total_calls, total_failures, "
                   "last_success_at, last_failure_at, last_error, cooldown_until, updated_at "
                   "FROM ai_model_health")
            params, where = [], []
            if provider:
                where.append('provider_slug=%s'); params.append(provider)
            if model_name:
                where.append('model_name=%s'); params.append(model_name)
            if where:
                sql += ' WHERE ' + ' AND '.join(where)
            sql += ' ORDER BY updated_at DESC LIMIT %s'
            params.append(limit)
            with get_db() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f'[failover] get_health failed: {e}')
            return []

    def get_recent_events(self, limit=50):
        try:
            from models import get_db
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT id, from_model_id, from_provider, from_model, "
                    "to_model_id, to_provider, to_model, reason, error_text, request_id, created_at "
                    "FROM ai_model_failover_events ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f'[failover] get_recent_events failed: {e}')
            return []


class FallbackEngine:
    """兜底候选加载 + 调用编排"""

    def __init__(self, breaker=None, health=None):
        self.breaker = breaker or CircuitBreaker()
        self.health = health or ModelHealthStore()

    def load_fallback_models(self):
        """system_config ai_fallback_models（JSON 列表）→ 解析失败/未配置时用内置兜底"""
        raw = _sys_key('ai_fallback_models')
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data:
                    return [d for d in data
                            if isinstance(d, dict) and d.get('provider') and d.get('model')]
            except Exception as e:
                logger.warning(f'[failover] ai_fallback_models parse failed: {e}')
        return list(BUILTIN_FALLBACKS)

    def _candidates(self, primary_cfg, fallback_cfgs):
        pk = _cfg_key(primary_cfg.get('provider', ''), primary_cfg.get('model', ''))
        out = [primary_cfg]
        for c in fallback_cfgs or []:
            ck = _cfg_key(c.get('provider', ''), c.get('model', ''))
            if ck and ck != pk:
                out.append(c)
        return out

    def call_with_failover(self, primary_cfg, fallback_cfgs, call_fn, request_id=''):
        """非流式兜底：call_fn(cfg) -> response。主模型失败按序尝试兜底，全部失败抛出带主模型上下文的异常。"""
        last_err = None
        primary_err = None
        for idx, cfg in enumerate(self._candidates(primary_cfg, fallback_cfgs)):
            key = _cfg_key(cfg.get('provider', ''), cfg.get('model', ''))
            if not self.breaker.is_available(key):
                continue
            try:
                resp = call_fn(cfg)
                self.breaker.record_success(key)
                self.health.record_success(cfg.get('provider', ''),
                                           cfg.get('model', ''), cfg.get('model_id', 0))
                if idx > 0:
                    self.health.log_failover_event(
                        primary_cfg, cfg, reason='health',
                        error=str(last_err) if last_err else '', request_id=request_id)
                return resp, cfg
            except Exception as e:
                last_err = e
                if idx == 0:
                    primary_err = e
                just_opened = self.breaker.record_failure(key)
                st = self.breaker.get_status(key)
                self.health.record_failure(cfg.get('provider', ''), cfg.get('model', ''),
                                           cfg.get('model_id', 0), error=str(e),
                                           breaker_open=st['open'],
                                           cooldown_until=_now_str(st['cooldown_until']))
                logger.warning(f'[failover] {key} failed: {e}')
                if just_opened:
                    _send_alert(
                        f'[AI 兜底引擎] 模型 {cfg.get("provider", "")}::{cfg.get("model", "")} '
                        f'连续失败触发熔断，已进入冷却（冷却至 {_now_str(st["cooldown_until"])}），'
                        f'将自动使用兜底模型')
        if last_err:
            primary_key = _cfg_key(primary_cfg.get('provider', ''), primary_cfg.get('model', ''))
            primary_msg = f'primary {primary_key} failed: {primary_err or last_err}'
            _send_alert(f'[AI 兜底引擎] 全部候选不可用 | {primary_msg}; fallbacks also failed, last: {last_err}')
            raise RuntimeError(f'{primary_msg}; fallbacks also failed, last: {last_err}') from last_err
        raise RuntimeError('All models unavailable (circuit open)')

    def stream_with_failover(self, primary_cfg, fallback_cfgs, stream_call_fn, request_id=''):
        """流式兜底：返回生成器。产出首个 chunk 前失败可切换；已产出后失败直接上抛（防重复）。"""
        def _iter():
            last_err = None
            primary_err = None
            for idx, cfg in enumerate(self._candidates(primary_cfg, fallback_cfgs)):
                key = _cfg_key(cfg.get('provider', ''), cfg.get('model', ''))
                if not self.breaker.is_available(key):
                    continue
                started = False
                try:
                    stream = stream_call_fn(cfg)
                    for chunk in stream:
                        if not started:
                            started = True
                            _iter.used_cfg = cfg
                            if idx > 0:
                                self.health.log_failover_event(
                                    primary_cfg, cfg, reason='health',
                                    error=str(last_err) if last_err else '',
                                    request_id=request_id)
                        yield chunk
                    self.breaker.record_success(key)
                    self.health.record_success(cfg.get('provider', ''),
                                               cfg.get('model', ''), cfg.get('model_id', 0))
                    return
                except Exception as e:
                    last_err = e
                    if idx == 0:
                        primary_err = e
                    if started:
                        raise
                    just_opened = self.breaker.record_failure(key)
                    st = self.breaker.get_status(key)
                    self.health.record_failure(cfg.get('provider', ''), cfg.get('model', ''),
                                               cfg.get('model_id', 0), error=str(e),
                                               breaker_open=st['open'],
                                               cooldown_until=_now_str(st['cooldown_until']))
                    logger.warning(f'[failover] stream {key} failed before first chunk: {e}')
                    if just_opened:
                        _send_alert(
                            f'[AI 兜底引擎] 模型 {cfg.get("provider", "")}::{cfg.get("model", "")} '
                            f'连续失败触发熔断，已进入冷却（冷却至 {_now_str(st["cooldown_until"])}）')
            if last_err:
                primary_key = _cfg_key(primary_cfg.get('provider', ''), primary_cfg.get('model', ''))
                primary_msg = f'primary {primary_key} failed: {primary_err or last_err}'
                _send_alert(f'[AI 兜底引擎] 全部候选不可用 | {primary_msg}; fallbacks also failed, last: {last_err}')
                raise RuntimeError(f'{primary_msg}; fallbacks also failed, last: {last_err}') from last_err
            raise RuntimeError('All models unavailable (circuit open)')
        return _iter()
