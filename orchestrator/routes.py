#!/usr/bin/env python3
# VeroRun 维洛智能 (verorun.com / verorun.cn)
# 版权所有 (c) 2026 樊聚民 (fanjumin). All Rights Reserved.

"""
Automation Routes — Flask Blueprint
====================================
自动化管理后台的 REST API 路由。
注册为 '/admin/automation/' 前缀的 blueprint。

API 端点概览:
  仪表盘:     GET    /stats
  Cron 任务:   CRUD   /jobs/*
  工作流定义:   CRUD   /workflows/*
  工作流执行:   POST   /workflows/<id>/run
  工作流实例:   GET    /instances/*, POST /instances/<id>/pause/resume/cancel
  日志:        GET    /logs/*
  系统 Agent:  GET    /system-agents

@package orchestrator
"""

from i18n import _
import os, sys, json
from flask import Blueprint, request, jsonify, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, '..', 'auth-center'))
sys.path.insert(0, os.path.join(BASE_DIR, '..'))

from . import models as m
from . import nodes as node_handlers
from .worker import WorkerPool
from .scheduler import SchedulerEngine

# ============================================================
# Blueprint 定义
# ============================================================

automation_bp = Blueprint('automation', __name__, url_prefix='/admin/automation')

# 全局单例（由 admin/app.py 初始化时创建）
_scheduler: SchedulerEngine = None
_worker_pool: WorkerPool = None


def init_automation(app):
    """初始化自动化系统（由 admin/app.py 调用）"""
    global _scheduler, _worker_pool

    # 1. 初始化数据库表
    m.init_orchestrator_tables()

    # 2. 创建 Worker 池
    _worker_pool = WorkerPool()

    # 3. 创建调度器
    _scheduler = SchedulerEngine()

    # 4. 注册 Worker 回调到调度器
    _worker_pool.register_scheduler_callbacks(_scheduler)

    # 5. 启动调度器
    _scheduler.start()

    # 5.5 消费插件 register_jobs()（APScheduler 定时任务契约）
    try:
        pm = app.extensions.get('plugin_manager')
        if pm is not None:
            n = pm.register_all_plugin_jobs(_scheduler)
            m.add_log('system', 0, 'info',
                       f'✅ Plugin job registration: {n} job(s) scheduled')
    except Exception as e:
        m.add_log('system', 0, 'warn',
                   f'⚠️ Plugin job registration failed: {e}')

    # 6. 注册蓝图
    app.register_blueprint(automation_bp)

    m.add_log('system', 0, 'info', _('✅ Automation System Initialized'))
    return _scheduler, _worker_pool


# ============================================================
# 辅助函数
# ============================================================

def _require_admin():
    """验证管理员身份（复用现有模式）"""
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return None
    return payload


def _success(data=None, message='ok'):
    """成功响应"""
    return jsonify({'success': True, 'data': data, 'message': message})


def _error(message, code=400):
    """错误响应"""
    return jsonify({'success': False, 'error': message}), code


# ============================================================
# 1. 仪表盘
# ============================================================

@automation_bp.route('/stats')
def get_stats():
    """获取自动化系统统计"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        stats = m.get_automation_stats()
    except Exception as e:
        stats = {'db_error': str(e)}

    # 加上调度器和 Worker 状态
    try:
        if _scheduler:
            sched_status = _scheduler.get_status()
            stats['scheduler'] = sched_status
    except Exception:
        stats['scheduler'] = {'error': 'unavailable'}

    try:
        if _worker_pool:
            pool_stats = _worker_pool.get_pool_stats()
            # 移除不可 JSON 序列化的 Future 对象
            active = _worker_pool.get_active_tasks()
            stats['worker_pool'] = {
                'dedicated_queue': pool_stats.get('dedicated_queue', 0),
                'shared_queue': pool_stats.get('shared_queue', 0),
                'active_count': len(active)
            }
    except Exception:
        stats['worker_pool'] = {'error': 'unavailable'}

    return _success(stats)


# ============================================================
# 2. Cron 任务 CRUD
# ============================================================

@automation_bp.route('/jobs', methods=['GET'])
def list_jobs():
    """列出 Cron 任务"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    active_only = request.args.get('active_only', '').lower() == 'true'
    priority = request.args.get('priority')

    result = m.list_cron_jobs(
        active_only=active_only,
        page=page,
        limit=limit,
        priority=priority
    )
    return _success(result)


@automation_bp.route('/jobs', methods=['POST'])
def create_job():
    """创建 Cron 任务"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    if not data or not data.get('name'):
        return _error(_('Name cannot be empty'))

    data['created_by'] = admin.get('id', 0)

    if _scheduler:
        job_id = _scheduler.add_job(data)
    else:
        job_id = m.create_cron_job(data)

    return _success({'job_id': job_id}, _('Task has been created'))


@automation_bp.route('/jobs/<int:job_id>', methods=['GET'])
def get_job(job_id):
    """获取单个任务"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    job = m.get_cron_job(job_id)
    if not job:
        return _error(_('Task does not exist'), 404)
    return _success(job)


@automation_bp.route('/jobs/<int:job_id>', methods=['PUT'])
def update_job(job_id):
    """更新任务"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    if not data:
        return _error(_('Updated data cannot be empty'))

    if _scheduler:
        success = _scheduler.update_job(job_id, data)
    else:
        success = m.update_cron_job(job_id, data)

    if not success:
        return _error(_('Task does not exist or is not updated'), 404)
    return _success(None, _('Task has been updated'))


@automation_bp.route('/jobs/<int:job_id>', methods=['DELETE'])
def delete_job(job_id):
    """删除任务"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    if _scheduler:
        success = _scheduler.remove_job(job_id)
    else:
        success = m.delete_cron_job(job_id)

    if not success:
        return _error(_('Task does not exist'), 404)
    return _success(None, _('Task has been deleted'))


@automation_bp.route('/jobs/<int:job_id>/toggle', methods=['POST'])
def toggle_job(job_id):
    """暂停/恢复任务"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    job = m.get_cron_job(job_id)
    if not job:
        return _error(_('Task does not exist'), 404)

    new_active = 0 if job['is_active'] else 1
    if _scheduler:
        if new_active:
            _scheduler.resume_job(job_id)
        else:
            _scheduler.pause_job(job_id)
    else:
        m.update_cron_job(job_id, {'is_active': new_active})

    return _success({'is_active': new_active}, _('Task has been') + (_('Restore') if new_active else _('Pause')))


@automation_bp.route('/jobs/<int:job_id>/run', methods=['POST'])
def run_job_now(job_id):
    """立即执行一次任务"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    job = m.get_cron_job(job_id)
    if not job:
        return _error(_('Task does not exist'), 404)

    # 通过 Worker 池异步执行（不阻塞请求线程）
    import threading
    if _scheduler:
        # 异步启动任务 — 不阻塞 HTTP 响应
        thread = threading.Thread(
            target=_scheduler._execute_job_wrapper,
            args=(job_id,),
            daemon=True,
            name=f'job-run-{job_id}'
        )
        thread.start()
        # 立即返回，前端可通过 /jobs/{job_id} 轮询状态
        return _success({
            'job_id': job_id,
            'status': 'started',
            'message': 'Job execution started in background'
        }, _('Task has been started'))

    return _error(_('Scheduler not initialized'), 500)


# ============================================================
# 3. 工作流定义 CRUD
# ============================================================

@automation_bp.route('/workflows', methods=['GET'])
def list_workflows():
    """列出工作流定义"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    active_only = request.args.get('active_only', '').lower() == 'true'

    result = m.list_workflows(active_only=active_only, page=page, limit=limit)
    return _success(result)


@automation_bp.route('/workflow-templates', methods=['GET'])
def list_workflow_templates():
    """返回预置工作流模板（只读蓝图，不写库）"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    from .workflow_templates import WORKFLOW_TEMPLATES
    return _success(WORKFLOW_TEMPLATES)


@automation_bp.route('/workflows', methods=['POST'])
def create_workflow():
    """创建工作流定义"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    if not data or not data.get('name'):
        return _error(_('Workflow name cannot be empty'))

    data['created_by'] = admin.get('id', 0)
    wf_id = m.create_workflow(data)

    return _success({'workflow_id': wf_id}, _('Workflow has been created'))


@automation_bp.route('/workflows/<int:wf_id>', methods=['GET'])
def get_workflow(wf_id):
    """获取工作流定义"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    wf = m.get_workflow(wf_id)
    if not wf:
        return _error(_('Workflow does not exist'), 404)

    # 解析 JSON 字段以便前端使用
    if wf.get('definition'):
        wf['definition'] = m.from_json(wf['definition'])
    if wf.get('triggers'):
        wf['triggers'] = m.from_json(wf['triggers'])

    return _success(wf)


@automation_bp.route('/workflows/<int:wf_id>', methods=['PUT'])
def update_workflow(wf_id):
    """更新工作流定义"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    if not data:
        return _error(_('Updated data cannot be empty'))

    # 确保 definition 存为 JSON 字符串
    if 'definition' in data and isinstance(data['definition'], dict):
        data['definition'] = m.to_json(data['definition'])
    if 'triggers' in data and isinstance(data['triggers'], list):
        data['triggers'] = m.to_json(data['triggers'])

    success = m.update_workflow(wf_id, data)
    if not success:
        return _error(_('Workflow does not exist'), 404)

    # 重新获取更新后的工作流，返回完整数据给前端
    updated = m.get_workflow(wf_id)
    if updated:
        if updated.get('definition'):
            updated['definition'] = m.from_json(updated['definition'])
        if updated.get('triggers'):
            updated['triggers'] = m.from_json(updated['triggers'])

    return _success(updated, _('Workflow has been updated'))


@automation_bp.route('/workflows/<int:wf_id>', methods=['DELETE'])
def delete_workflow(wf_id):
    """删除工作流"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    success = m.delete_workflow(wf_id)
    if not success:
        return _error(_('Workflow does not exist'), 404)

    return _success(None, _('Workflow has been deleted'))


# ============================================================
# 4. 工作流执行
# ============================================================

@automation_bp.route('/workflows/<int:wf_id>/run', methods=['POST'])
def run_workflow(wf_id):
    """手动触发工作流执行"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    wf = m.get_workflow(wf_id)
    if not wf:
        return _error(_('Workflow does not exist'), 404)
    if not wf['is_active']:
        return _error(_('Workflow has been disabled'), 400)

    # P1-13: 并发执行检查 — 同一工作流只能有一个运行中的实例
    max_concurrency = wf.get('max_concurrency', 1)
    if max_concurrency == 1:
        from orchestrator import models as om
        running = om.get_running_instances(wf_id)
        if running:
            return _error(_('Workflow is already running'), 409)

    data = request.get_json() or {}
    initial_context = data.get('context', {})

    if not _worker_pool:
        return _error(_('Worker pool not initialized'), 500)

    try:
        inst_id = _worker_pool.workflow_engine.run_workflow(
            wf_id,
            trigger_type='manual',
            trigger_config={'admin_id': admin.get('id')},
            initial_context=initial_context
        )
        return _success({'instance_id': inst_id}, _('Workflow started (instance #{id})', id=inst_id))
    except Exception as e:
        return _error(f'Launch failed: {str(e)}', 500)


# ============================================================
# 5. 工作流实例
# ============================================================

@automation_bp.route('/instances', methods=['GET'])
def list_instances():
    """列出工作流实例"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    status = request.args.get('status')
    workflow_id = request.args.get('workflow_id', type=int)

    result = m.list_workflow_instances(
        workflow_id=workflow_id,
        status=status,
        page=page,
        limit=limit
    )
    return _success(result)


@automation_bp.route('/instances/<int:inst_id>', methods=['GET'])
def get_instance(inst_id):
    """获取工作流实例详情"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    inst = m.get_workflow_instance(inst_id)
    if not inst:
        return _error(_('Instance Does Not Exist'), 404)

    # 获取相关节点实例
    nodes = m.get_node_instances_by_workflow(inst_id)

    # 获取工作流定义
    wf = m.get_workflow(inst['workflow_id'])

    return _success({
        'instance': inst,
        'nodes': nodes,
        'workflow_name': wf['name'] if wf else 'Unknown'
    })


@automation_bp.route('/instances/<int:inst_id>/pause', methods=['POST'])
def pause_instance(inst_id):
    """暂停工作流实例"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    if _worker_pool and _worker_pool.workflow_engine.pause_instance(inst_id):
        return _success(None, _('Workflow has been paused'))
    return _error(_('Operation Failed'), 400)


@automation_bp.route('/instances/<int:inst_id>/resume', methods=['POST'])
def resume_instance(inst_id):
    """恢复工作流实例"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    if _worker_pool and _worker_pool.workflow_engine.resume_instance(inst_id):
        return _success(None, _('Workflow has been restored'))
    return _error(_('Operation Failed'), 400)


@automation_bp.route('/instances/<int:inst_id>/cancel', methods=['POST'])
def cancel_instance(inst_id):
    """取消工作流实例"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    if _worker_pool and _worker_pool.workflow_engine.cancel_instance(inst_id):
        return _success(None, _('Workflow has been canceled'))
    return _error(_('Operation Failed'), 400)


@automation_bp.route('/instances/<int:inst_id>/nodes/<int:node_inst_id>/approve',
                     methods=['POST'])
def approve_node(inst_id, node_inst_id):
    """审批工作流节点（角色鉴权 + 审批备注）"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json() or {}
    approved = data.get('approved', True)
    note = data.get('note', '')

    if not _worker_pool:
        return _error(_('Worker pool not initialized'), 500)
    engine = _worker_pool.workflow_engine

    # 审批鉴权：读取节点配置中的 approver_ids / approver_role
    try:
        node_insts = m.get_node_instances_by_workflow(inst_id)
        node_inst = next((n for n in node_insts if n['id'] == node_inst_id), None)
        if not node_inst:
            return _error(_('Node does not exist'), 404)
        inst = m.get_workflow_instance(inst_id)
        if not inst:
            return _error(_('Instance Does Not Exist'), 404)
        wf = m.get_workflow(inst['workflow_id'])
        definition = m.from_json(wf.get('definition', '{}')) if wf else {}
        node_def = next(
            (n for n in definition.get('nodes', []) if n.get('id') == node_inst.get('node_id')),
            None
        )
        cfg = (node_def or {}).get('config', {}) or {}
    except Exception as e:
        return _error(f'Approval config lookup failed: {e}', 400)

    approver_ids = cfg.get('approver_ids') or []
    if approver_ids:
        # 指定审批人列表：仅列表内的管理员可审批
        try:
            if int(admin.get('id', 0)) not in [int(x) for x in approver_ids]:
                return _error(_('You are not authorized to approve this node'), 403)
        except (TypeError, ValueError):
            return _error(_('Invalid approver_ids in node config'), 400)
    else:
        # 角色鉴权：super_admin 可审批任意角色；'admin' = 任意管理员
        role = cfg.get('approver_role', 'admin')
        reviewer_role = admin.get('role', '') or ''
        if role != 'admin' and role != reviewer_role and reviewer_role != 'super_admin':
            return _error(_('You are not authorized to approve this node'), 403)

    ok = engine.approve_node(
        inst_id, node_inst_id,
        approved=approved,
        reviewer=admin.get('id', 0),
        note=note
    )
    if ok:
        return _success(None, _('Approval') + (_('Approved') if approved else _('Reject')))
    return _error(_('Operation Failed'), 400)


# ============================================================
# 6. 日志
# ============================================================

@automation_bp.route('/logs', methods=['GET'])
def get_logs():
    """查询执行日志"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 100))
    source_type = request.args.get('source_type')
    source_id = request.args.get('source_id', type=int)
    level = request.args.get('level')

    result = m.query_logs(
        source_type=source_type,
        source_id=source_id,
        level=level,
        page=page,
        limit=limit
    )
    return _success(result)


# ============================================================
# 7. 系统 Agent
# ============================================================

@automation_bp.route('/system-agents', methods=['GET'])
def list_system_agents():
    """列出系统 Agent"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    agents = m.list_system_agents()
    return _success({'agents': agents})


@automation_bp.route('/system-agents/<int:agent_id>', methods=['PUT'])
def update_system_agent(agent_id):
    """更新系统 Agent 配置"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    if not data:
        return _error(_('Data cannot be empty'))

    fields = []
    values = []
    for key in ('name', 'description', 'provider', 'model', 'api_key_ref',
                'base_url', 'system_prompt', 'capabilities', 'max_concurrency',
                'is_active'):
        if key in data:
            fields.append(f"{key}=%s")
            v = data[key]
            if isinstance(v, (dict, list)):
                v = m.to_json(v)
            values.append(v)

    if not fields:
        return _error(_('No Valid Fields'))

    values.append(agent_id)
    with m.get_db() as conn:
        conn.execute(
            f"UPDATE system_agents SET {', '.join(fields)}, "
            f"updated_at=NOW() WHERE id=%s",
            values
        )
    return _success(None, _('System Agent has been updated'))


# ============================================================
# 8. 调度器控制
# ============================================================

@automation_bp.route('/scheduler/status', methods=['GET'])
def scheduler_status():
    """获取调度器状态"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    if not _scheduler:
        return _error(_('Scheduler not initialized'), 500)

    return _success(_scheduler.get_status())


@automation_bp.route('/scheduler/pause', methods=['POST'])
def pause_scheduler():
    """暂停调度器"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    if _scheduler:
        _scheduler.pause()
        return _success(None, _('Scheduler paused'))
    return _error(_('Scheduler not initialized'), 500)


@automation_bp.route('/scheduler/resume', methods=['POST'])
def resume_scheduler():
    """恢复调度器"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    if _scheduler:
        _scheduler.resume()
        return _success(None, _('Scheduler recovered'))
    return _error(_('Scheduler not initialized'), 500)


# ============================================================
# 9. 健康检查
# ============================================================

@automation_bp.route('/health')
def health_check():
    """健康检查（不需要认证）"""
    return jsonify({
        'status': 'ok',
        'scheduler_running': bool(_scheduler and _scheduler._apscheduler.running),
        'worker_pool_active': bool(_worker_pool),
        'tables_initialized': True
    })
