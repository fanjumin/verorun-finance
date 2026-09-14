#!/usr/bin/env python3
"""Social Push Plugin — 发布队列（P4）

- enqueue()          入队（定时/立即）
- cancel_task()      取消 pending 任务
- list_tasks()       队列列表（分页/状态过滤）
- dispatch_due()     到期任务执行（scheduler 每分钟调用，PG advisory lock 防并发）
"""
import json
import logging
import time

logger = logging.getLogger(__name__)

# PG advisory lock 固定键（'social' 十六进制 0x736f6369616c）：多 worker 下保证单实例派发
QUEUE_LOCK_KEY = 0x736f6369616c

# 单次派发上限（防长任务阻塞调度）
DISPATCH_BATCH = 20

# 队列级最大重试次数（超过后任务转 failed，不再自动重试）
MAX_RETRIES = 3


def enqueue(channel: str, payload: dict, schedule_at=None, draft_id=None,
            admin_id=None) -> dict:
    """入队发布任务。schedule_at 为空=立即；返回 {id}。"""
    from ..models import get_sp_db
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    with get_sp_db() as conn:
        cur = conn.execute(
            """INSERT INTO publish_queue
               (channel, draft_id, payload_json, status, schedule_at, admin_id)
               VALUES (%s,%s,%s,'pending',%s,%s) RETURNING id""",
            (channel, draft_id, payload_json, schedule_at, admin_id))
        task_id = cur.fetchone()['id']
        conn.commit()
    return {'id': task_id}


def cancel_task(task_id: int) -> bool:
    """取消 pending 任务（已派发/已发布不可取消）。"""
    from ..models import get_sp_db
    with get_sp_db() as conn:
        cur = conn.execute(
            "UPDATE publish_queue SET status='cancelled', updated_at=NOW() "
            "WHERE id=%s AND status='pending'", (task_id,))
        conn.commit()
    return cur.rowcount > 0


def list_tasks(status: str = '', limit: int = 50, offset: int = 0) -> list:
    """队列列表。status 可选 pending/publishing/published/failed/cancelled。"""
    from ..models import get_sp_db
    sql = "SELECT * FROM publish_queue"
    params = []
    if status:
        sql += " WHERE status=%s"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
    params += [int(limit), int(offset)]
    with get_sp_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r['payload_json'] or '{}')
        except Exception:
            payload = {}
        out.append({
            'id': r['id'], 'channel': r['channel'], 'draft_id': r['draft_id'],
            'status': r['status'], 'payload': payload,
            'schedule_at': str(r['schedule_at']) if r.get('schedule_at') else '',
            'publish_at': str(r['publish_at']) if r.get('publish_at') else '',
            'retry_count': r['retry_count'], 'error_msg': r['error_msg'],
            'post_id': r['post_id'], 'post_url': r['post_url'],
            'created_at': str(r['created_at']) if r.get('created_at') else '',
        })
    return out


def dispatch_due():
    """扫描到期 pending 任务并执行发布（幂等，PG advisory lock 防并发）。

    返回派发任务数。发布结果写回 publish_queue 并落 social_push_logs（由
    _publish_via_gateway / _publish_via_provider 完成），失败任务留待重试。
    """
    lock_conn = None
    try:
        from ..models import get_sp_db
        lock_conn = get_sp_db()
        cur = lock_conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (QUEUE_LOCK_KEY,))
        acquired = cur.fetchone()[0]
        if not acquired:
            logger.info('[SocialPush] publish queue dispatch already running in another worker; skip')
            return 0
    except Exception as e:
        logger.warning('[SocialPush] advisory lock acquire failed (%s); dispatch without lock', e)

    dispatched = 0
    try:
        from ..models import get_sp_db
        with get_sp_db() as conn:
            rows = conn.execute(
                """SELECT * FROM publish_queue
                   WHERE status='pending' AND (schedule_at IS NULL OR schedule_at <= NOW())
                   ORDER BY id LIMIT %s""", (DISPATCH_BATCH,)).fetchall()
        for r in rows:
            try:
                task_id = r['id']
                channel = r['channel']
                payload = json.loads(r['payload_json'] or '{}')
                admin_id = r['admin_id']

                # 标记 publishing（防重复派发）
                with get_sp_db() as conn:
                    conn.execute(
                        "UPDATE publish_queue SET status='publishing', updated_at=NOW() "
                        "WHERE id=%s AND status='pending'", (task_id,))
                    conn.commit()

                # 执行发布：走网关渠道（统一入口），失败按错误分级重试
                from .retry import with_retry, is_retryable
                result = with_retry(_execute_channel, channel, payload, admin_id)
                if result.get('status') == 'published':
                    new_status = 'published'
                    err = ''
                elif is_retryable(result.get('error', ''), result.get('status_code', 0)):
                    # 可重试但已耗尽 → 重置为 pending 交由下轮扫描（retry_count 递增）
                    new_retry = (r['retry_count'] or 0) + 1
                    if new_retry > MAX_RETRIES:
                        new_status = 'failed'
                        err = (result.get('error', '') or '')[:500]
                    else:
                        new_status = 'pending'
                        err = ''
                    with get_sp_db() as conn:
                        conn.execute(
                            """UPDATE publish_queue SET status=%s, retry_count=%s,
                               publish_at=NOW(), error_msg=%s, updated_at=NOW()
                               WHERE id=%s""",
                            (new_status, new_retry, err, task_id))
                        conn.commit()
                    dispatched += 1
                    continue
                else:
                    new_status = 'failed'
                    err = (result.get('error', '') or '')[:500]

                with get_sp_db() as conn:
                    conn.execute(
                        """UPDATE publish_queue SET status=%s, publish_at=NOW(),
                           post_id=%s, post_url=%s, error_msg=%s, updated_at=NOW()
                           WHERE id=%s""",
                        (new_status, result.get('media_id', ''), result.get('url', ''),
                         err, task_id))
                    conn.commit()
                dispatched += 1
            except Exception as e:
                logger.exception('[SocialPush] dispatch task %s failed', r.get('id'))
                try:
                    with get_sp_db() as conn:
                        conn.execute(
                            "UPDATE publish_queue SET status='failed', error_msg=%s, updated_at=NOW() "
                            "WHERE id=%s", (str(e)[:500], r['id']))
                        conn.commit()
                except Exception:
                    pass
    except Exception as e:
        logger.exception('[SocialPush] dispatch_due scan failed: %s', e)
    finally:
        if lock_conn is not None:
            try:
                lock_conn.close()
            except Exception:
                pass
    return dispatched


def _execute_channel(channel: str, payload: dict, admin_id) -> dict:
    """按渠道类型执行发布。

    网关渠道（twitter/weibo/telegram/facebook/instagram/linkedin/reddit/toutiao）：
    统一走 gateway.publish()。
    长文渠道（wechat）暂保留原链路（由 routes._publish_to_platform 分发）。
    """
    from ..routes import _publish_to_platform
    return _publish_to_platform(
        platform=channel,
        title=payload.get('title', ''),
        body=payload.get('body', ''),
        body_html=payload.get('body_html', ''),
        summary=payload.get('summary', ''),
        author='',
        cover_image_url=payload.get('cover_image_url', ''),
        auto_publish=True,
        admin_id=admin_id,
    )
