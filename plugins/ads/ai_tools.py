#!/usr/bin/env python3
"""Ad Management Plugin — AI 可调用工具封装 (v1.2.0)

为 Agent Matrix / chat_tool 提供广告管理能力的内部封装。
所有函数返回 dict {'success': bool, 'data': any, 'error': str|None}，
便于上层统一处理。
"""
import json, os, sys, re, logging

from i18n import _

# 确保能找到项目根目录下的模块
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

_logger = logging.getLogger(__name__)


def _sanitize_ad_code(ad_code: str) -> str:
    """清洗广告代码，剥离 script/事件属性，防止存储型 XSS。
    LLM 输出的 ad_code 可能包含恶意 HTML/JS，入库前必须净化。
    """
    if not ad_code:
        return ad_code
    # 剥离 <script> 标签及其内容
    ad_code = re.sub(r'<script[\s\S]*?</script>', '', ad_code, flags=re.IGNORECASE)
    # 剥离 on* 事件属性
    ad_code = re.sub(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', '', ad_code, flags=re.IGNORECASE)
    ad_code = re.sub(r'\s+on\w+\s*=\s*[^\s>]+', '', ad_code, flags=re.IGNORECASE)
    # 剥离 javascript: 伪协议
    ad_code = re.sub(r'(?i)javascript\s*:', '', ad_code)
    # 剥离 <iframe>
    ad_code = re.sub(r'<iframe[\s\S]*?</iframe>', '', ad_code, flags=re.IGNORECASE)
    _logger.info("[Ads] ad_code sanitized for storage")
    return ad_code


def _get_db():
    """获取 ads 插件数据库连接"""
    from plugins.ads.models import get_ads_db
    return get_ads_db()


def list_ads(site_key=None, position=None, active_only=False):
    """列出广告"""
    try:
        where = []
        params = []
        if site_key:
            where.append('site_key=%s')
            params.append(site_key)
        if position:
            where.append('position=%s')
            params.append(position)
        if active_only:
            where.append('is_active=1')
        where_sql = f"WHERE {' AND '.join(where)}" if where else ''
        with _get_db() as conn:
            rows = conn.execute(
                f'SELECT * FROM ad_placements {where_sql} ORDER BY sort_order, id',
                params
            ).fetchall()
        data = []
        for r in rows:
            d = dict(r)
            try:
                d['targeting_rules'] = json.loads(d.get('targeting_rules') or '{}')
            except (json.JSONDecodeError, TypeError):
                d['targeting_rules'] = {}
            data.append(d)
        return {'success': True, 'data': data}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_ad(ad_id):
    """获取单个广告详情"""
    try:
        with _get_db() as conn:
            row = conn.execute('SELECT * FROM ad_placements WHERE id=%s', (ad_id,)).fetchone()
        if not row:
            return {'success': False, 'error': _('Advertisement does not exist')}
        d = dict(row)
        try:
            d['targeting_rules'] = json.loads(d.get('targeting_rules') or '{}')
        except (json.JSONDecodeError, TypeError):
            d['targeting_rules'] = {}
        return {'success': True, 'data': d}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def create_ad(data):
    """创建广告（入库前自动清洗 ad_code 防 XSS）"""
    try:
        # 校验必填
        name = (data.get('name') or '').strip()
        if not name:
            return {'success': False, 'error': _('Advertisement name cannot be empty')}
        # P0-F12: ad_code 入库前清洗脚本/事件属性
        if 'ad_code' in data and data['ad_code']:
            data['ad_code'] = _sanitize_ad_code(data['ad_code'])
        from plugins.ads.models import create_ad_record
        ad_id = create_ad_record(data)
        return {'success': True, 'data': {'id': ad_id}}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def update_ad(ad_id, data):
    """更新广告（动态字段，列名白名单见 models._UPDATE_FIELDS，入库前自动清洗 ad_code）"""
    try:
        # P0-F12: ad_code 入库前清洗脚本/事件属性
        if 'ad_code' in data and data['ad_code']:
            data['ad_code'] = _sanitize_ad_code(data['ad_code'])
        from plugins.ads.models import update_ad_record, AdNotFound
        try:
            update_ad_record(ad_id, data)
        except AdNotFound:
            return {'success': False, 'error': _('Advertisement does not exist')}
        except ValueError:
            return {'success': False, 'error': _('No fields to update')}
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def delete_ad(ad_id):
    """删除广告（级联清理统计与点击明细）"""
    try:
        from plugins.ads.models import delete_ad_record
        delete_ad_record(ad_id)
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_stats(ad_id=None, site_key=None, days=7):
    """获取广告统计"""
    try:
        from plugins.ads.models import get_ad_stats
        return {'success': True, 'data': get_ad_stats(ad_id=ad_id, site_key=site_key, days=days)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def list_zones(site_key=None, active_only=False):
    """列出广告区域"""
    try:
        from plugins.ads.models import list_zones as _list_zones
        return {'success': True, 'data': _list_zones(site_key=site_key or 'default', active_only=active_only)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def analyze_ads(days=7):
    """分析广告效果并返回文字洞察"""
    try:
        from plugins.ads.models import get_ad_stats
        stats = get_ad_stats(ad_id=None, days=days)
        ads_res = list_ads()
        ads = ads_res.get('data', []) if ads_res.get('success') else []

        total = stats.get('total', {})
        daily = stats.get('daily', [])

        lines = [
            _("=== Ad performance analysis (last {} days) ===").format(days),
            _("Total impressions: {}").format(total.get('impressions', 0)),
            _("Total clicks: {}").format(total.get('clicks', 0)),
            _("Overall CTR: {}%").format(total.get('ctr', 0)),
            "",
            _("Sorted by ad performance:")
        ]

        # 合并广告与累计数据（用当前累计 impressions/clicks）
        ranked = sorted(ads, key=lambda x: (x.get('clicks', 0), x.get('impressions', 0)), reverse=True)
        for i, a in enumerate(ranked[:10], 1):
            imp = a.get('impressions', 0)
            clk = a.get('clicks', 0)
            ctr = round(clk / imp * 100, 2) if imp else 0.0
            status = _('Running') if a.get('is_active') else _('Paused')
            lines.append(_("{}. [{}] {} | {} | impressions {} | clicks {} | CTR {}%").format(
                i, status, a.get('name'), a.get('position'), imp, clk, ctr))

        # 找出异常：高展示低点击
        low_ctr = [a for a in ads if a.get('impressions', 0) > 100 and (a.get('clicks', 0) / a.get('impressions', 0)) < 0.005]
        if low_ctr:
            lines.append("")
            lines.append(_("Ads needing optimization (impressions>100 and CTR<0.5%):"))
            for a in low_ctr:
                lines.append(_("- {} ({}): consider replacing the creative or adjusting targeting").format(a.get('name'), a.get('position')))

        # 趋势
        if daily:
            lines.append("")
            lines.append(_("Recent trends:"))
            for r in daily[-7:]:
                lines.append(_("- {}: impressions {}, clicks {}").format(r['stat_date'], r.get('impressions', 0), r.get('clicks', 0)))

        return {'success': True, 'data': '\n'.join(lines)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def generate_render_snippet(position='sidebar', page='*', site_key='default', zone_id=None):
    """生成广告位渲染代码片段"""
    try:
        zone_attr = f' data-ad-zone-id="{zone_id}"' if zone_id is not None else ''
        snippet = (
            f'{{% from "plugins/ads/templates/render_ads.html" import render_ads %}}\n'
            f'{{{{ render_ads(position="{position}", page="{page}", site_key="{site_key}"{zone_attr}) }}}}'
        )
        return {'success': True, 'data': snippet}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def toggle_ad(ad_id, is_active):
    """启用/禁用广告"""
    return update_ad(ad_id, {'is_active': 1 if is_active else 0})
