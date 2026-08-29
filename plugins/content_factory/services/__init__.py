#!/usr/bin/env python3
"""Content Factory Plugin — 采集管理器主入口"""
from i18n import _
import importlib, json
from typing import Optional
from plugins.content_factory.models import get_cf_db
from .base_collector import BaseCollector
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('content_factory')

COLLECTOR_MAP = {
    'rss': 'collectors.rss_collector.RSSCollector',
}


def get_collector(source_type: str, source_id: int, config: dict = None) -> Optional[BaseCollector]:
    """工厂 — 根据 source_type 返回采集器实例"""
    path = COLLECTOR_MAP.get(source_type)
    if not path:
        return None
    module_path, cls_name = path.rsplit('.', 1)
    try:
        mod = importlib.import_module(f'.{module_path}', __package__)
        cls = getattr(mod, cls_name)
        return cls(source_id, config or {})
    except Exception as e:
        logger.error(_("[CF] 加载采集器 {} 失败: {}").format(source_type, e))
        return None


def run_collection(source_id: int, source_type: str = None,
                   config: dict = None, **kwargs) -> dict:
    """执行一次采集"""
    with get_cf_db() as conn:
        src = conn.execute('SELECT * FROM content_sources WHERE id=?', (source_id,)).fetchone()
        if not src:
            return {'success': False, 'error': _('Source does not exist')}
        if not src['is_active']:
            return {'success': False, 'error': _('Source is disabled')}

        source_type = source_type or src['source_type']
        cfg = {}
        try:
            cfg = json.loads(src['config_json'] or '{}')
        except:
            pass
        cfg.update(config or {})
        if src['url'] and 'url' not in cfg:
            cfg['url'] = src['url']

        collector = get_collector(source_type, source_id, cfg)
        if not collector:
            return {'success': False, 'error': f'Unknown data type: {source_type}'}

        cur = conn.execute(
            """INSERT INTO content_tasks (source_id, task_type, trigger_type, status, started_at, created_by)
               VALUES (?, 'crawl', 'manual', 'running', NOW(), ?) RETURNING id""",
            (source_id, kwargs.get('admin_id', 1))
        )
        conn.commit()
        task_id = cur.fetchone()['id']

        try:
            results = collector.collect(**kwargs)
            inserted, skipped = collector.save_results(results, task_id=task_id)
            status = 'completed'
            log = f"Total {len(results)} items → Added {inserted}, Skipped {skipped}"
        except Exception as e:
            logger.exception(f"[CF] 采集失败 source_id={source_id}")
            status = 'failed'
            inserted = 0
            skipped = 0
            log = str(e)

        conn.execute(
            """UPDATE content_tasks SET status=?, finished_at=NOW(),
               total_items=?, done_items=?, log_text=? WHERE id=?""",
            (status, inserted + skipped, inserted, log, task_id)
        )
        if status == 'completed':
            conn.execute("UPDATE content_sources SET last_crawled_at=NOW() WHERE id=?", (source_id,))
        conn.commit()

        return {'success': status == 'completed', 'total': inserted + skipped,
                'inserted': inserted, 'skipped': skipped, 'task_id': task_id,
                'log': log, 'error': log if status == 'failed' else ''}