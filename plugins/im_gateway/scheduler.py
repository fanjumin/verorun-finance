#!/usr/bin/env python3
"""统一网关 — 定时任务：token 过期扫描刷新（APScheduler via register_jobs，Phase 5）"""
import logging
from datetime import datetime, timedelta

from .models_accounts import list_accounts, save_account
from .oauth import get_oauth_provider, OAUTH_PLATFORMS

logger = logging.getLogger(__name__)

# PG advisory lock 固定键（'gateway' 十六进制）：多 gunicorn worker 下保证刷新单实例执行
GATEWAY_REFRESH_LOCK_KEY = 0x67617465776179


def refresh_expiring_tokens():
    """每日扫描：剩余有效期 < 7 天的 token 自动刷新

    先取 PG 事务级 advisory lock，抢锁失败的 worker 直接跳过，
    避免多 worker 并发刷新导致重复请求与互相覆盖。
    """
    lock_conn = None
    try:
        from .models import get_im_db
        lock_conn = get_im_db()
        cur = lock_conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (GATEWAY_REFRESH_LOCK_KEY,))
        acquired = cur.fetchone()[0]
        if not acquired:
            logger.info('[Gateway] token refresh already running in another worker; skip')
            return
    except Exception as e:
        logger.warning('[Gateway] advisory lock acquire failed (%s); continue without lock', e)

    try:
        for acct in list_accounts():
            expires = acct.get('token_expires_at')
            if not expires or acct['channel'] not in OAUTH_PLATFORMS:
                continue
            try:
                expire_dt = datetime.fromisoformat(expires)
            except ValueError:
                continue
            if expire_dt - datetime.now() > timedelta(days=7):
                continue
            cfg = acct['config']
            refresh = cfg.get('refresh_token')
            if not refresh:
                logger.warning('[Gateway] account %s/%s missing refresh_token',
                               acct['channel'], acct['account_key'])
                continue
            try:
                provider = get_oauth_provider(acct['channel'], _app_creds(acct['channel']))
                if provider is None:
                    logger.warning('[Gateway] no oauth provider for %s', acct['channel'])
                    continue
                new_token = provider.refresh_token(refresh)
                save_account(acct['channel'], acct['account_key'],
                             {**cfg, **new_token},
                             handle=acct.get('handle', ''),
                             token_expires_at=_compute_expiry(new_token))
                logger.info('[Gateway] refreshed token for %s/%s',
                            acct['channel'], acct['account_key'])
            except Exception as e:
                logger.error('[Gateway] refresh failed %s/%s: %s',
                             acct['channel'], acct['account_key'], e)
    finally:
        if lock_conn is not None:
            try:
                lock_conn.close()  # 归还池前回滚事务，自动释放 advisory lock
            except Exception:
                pass


def _compute_expiry(token: dict) -> str:
    if token.get('expires_in'):
        return (datetime.now() + timedelta(seconds=int(token['expires_in']))).isoformat()
    return ''


def _app_creds(channel: str) -> dict:
    from .models import get_im_db
    import json
    try:
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel=%s", (channel,)
            ).fetchone()
        return json.loads(row['config_json']) if row else {}
    except Exception:
        return {}


GATEWAY_JOBS = [
    {
        'id': 'gateway_token_refresh',
        'func': refresh_expiring_tokens,
        'trigger': 'cron',
        'hour': 4,
        'minute': 0,
    },
]
