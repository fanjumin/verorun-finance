#!/usr/bin/env python3
"""
Workflow Admin API — 工作流管理 REST API
=========================================
收归内核 orchestrator 统一管理的工作流管理面（MVP）。

- 挂载: /admin/workflows（由 orchestrator/routes.init_automation 注册）
- 数据模型: workflow_defs（uuid TEXT id，节点/边对齐壳层 WorkflowDefinitionLite：
    nodes: [{id, data: {label, nodeType, config, serverId}}]
    edges: [{source, target, condition}]）
- 执行: 复用 WorkflowEngine.run_workflow（按 DB id 读取定义）。
  引擎无 execute(definition) 入口 → 通过 workflow_defs.engine_id 将定义
  写透同步到 workflow_definitions（引擎侧表）后按 id 触发，不重写 DAG 逻辑。
- 鉴权: admin JWT（_require_admin）+ module_policy 'workflow' 域 gate。

@package orchestrator
"""

from i18n import _
import os, sys, uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, '..', 'auth-center'))

from flask import Blueprint, request, jsonify, current_app

from . import models as m
from .routes import _require_admin, _success, _error, _guard_dangerous_nodes

workflow_admin_bp = Blueprint('workflow_admin', __name__, url_prefix='/admin/workflows')

# 12 种节点类型清单（与 nodes.py / workflow_engine.py 注册表对齐）
WORKFLOW_NODE_TYPES = [
    'ai_agent', 'data_collect', 'ai_process', 'condition',
    'approval', 'publish', 'notify', 'wait',
    'sub_workflow', 'market_check', 'http_request', 'script',
]


# ============================================================
# 辅助函数
# ============================================================

def _require_admin_or_403():
    """适配 orchestrator.routes._require_admin（返回 payload 或 None）→ (admin, err)。

    保持与 auth-center 端点一致的 (admin, err) 元组风格，err 为 Flask 响应或 None。
    """
    admin = _require_admin()
    if not admin:
        return None, (jsonify({'success': False, 'error': 'Unauthorized'}), 403)
    return admin, None


def _require_workflow_module(admin):
    """module_policy 'workflow' 域权限校验。

    复用 module_policy.py L80 的 workflow 域语义（pattern: continuous,
    post_trial_action: pause）：check_access 对首次使用自动开 14 天试用放行，
    试用过期/暂停则拦截。策略引擎异常时保守放行，避免误伤正常请求。
    """
    user_id = admin.get('user_id') or admin.get('id')
    if not user_id:
        return None
    try:
        from services.module_policy import get_policy_engine
        allowed, reason = get_policy_engine().check_access(user_id, 'workflow')
        if not allowed:
            return jsonify({'success': False, 'error': reason or _('Workflow module unavailable')}), 403
    except Exception:
        pass
    return None


def _get_engine():
    """获取 WorkflowEngine 实例（由 admin/app.py 启动时注入 app.config）"""
    worker = current_app.config.get('AUTOMATION_WORKER')
    if worker is None:
        return None
    return worker.workflow_engine


def _get_scheduler():
    """获取 SchedulerEngine 实例（admin/app.py 启动时注入 app.config，与 AUTOMATION_WORKER 同模式）"""
    return current_app.config.get('AUTOMATION_SCHEDULER')


def _sync_workflow_cron(def_id: str, def_row: dict):
    """将 workflow_defs.cron_expr 同步为真实定时调度（cron_jobs + SchedulerEngine）。

    - cron_expr 非空且非 'off'/'' → upsert 一条 target_type='workflow' 的 cron_jobs
      （target_config.workflow_id = 引擎侧 engine_id；is_active 与 workflow.enabled 联动）；
    - cron_expr 为空/off → 删除该 workflow 对应的 cron job（enabled 置 False 不会删行，
      仅把 is_active 置 0 暂停调度）。
    复用 SchedulerEngine.add_job/update_job/remove_job（未初始化时降级 models 层），
    与现有 job 生命周期（DB 行 + APScheduler 内存调度）保持一致；幂等：
    以 workflow_def_id 定位已有 job，先查后建/改，不产生重复行。
    """
    cron_expr = (def_row.get('cron_expr') or '').strip()
    engine_id = def_row.get('engine_id')
    existing = m.get_cron_job_by_workflow(def_id)

    if cron_expr and cron_expr.lower() != 'off' and engine_id:
        job_data = {
            'name': f'Workflow: {def_row.get("name", def_id)}',
            'description': 'workflow_defs.cron_expr 同步调度',
            'job_type': 'cron',
            'cron_expr': cron_expr,
            'target_type': 'workflow',
            'target_config': {'workflow_id': engine_id},
            'workflow_def_id': def_id,
            'is_active': 1 if def_row.get('enabled', True) else 0,
            'created_by': def_row.get('created_by', 0),
        }
        scheduler = _get_scheduler()
        if existing:
            if scheduler:
                scheduler.update_job(existing['id'], job_data)
            else:
                m.update_cron_job(existing['id'], job_data)
        else:
            if scheduler:
                scheduler.add_job(job_data)
            else:
                m.create_cron_job(job_data)
    elif existing:
        scheduler = _get_scheduler()
        if scheduler:
            scheduler.remove_job(existing['id'])
        else:
            m.delete_cron_job(existing['id'])


def _to_engine_definition(def_row: dict) -> dict:
    """API 形状 (WorkflowDefinitionLite) → 引擎形状（definition JSON）。

    - node: {id, data: {label, nodeType, config}} → {id, type, name, config}
    - edge: {source, target, condition} → {from, to, condition}
    兼容已按引擎形状提交的节点/边（透传）。nodes/edges 可为 JSON 字符串或 list。
    """
    nodes = def_row.get('nodes', '[]')
    edges = def_row.get('edges', '[]')
    nodes = m.from_json(nodes) if isinstance(nodes, str) else (nodes or [])
    edges = m.from_json(edges) if isinstance(edges, str) else (edges or [])
    engine_nodes = []
    for n in nodes:
        data = n.get('data') or {}
        if isinstance(data, dict) and data.get('nodeType'):
            engine_nodes.append({
                'id': n.get('id'),
                'type': data.get('nodeType'),
                'name': data.get('label') or n.get('id'),
                'config': data.get('config') or {},
            })
        else:
            engine_nodes.append(dict(n))  # 引擎形状透传
    engine_edges = []
    for e in edges:
        engine_edges.append({
            'from': e.get('source') or e.get('from'),
            'to': e.get('target') or e.get('to'),
            'condition': e.get('condition') or 'success',
        })
    return {'nodes': engine_nodes, 'edges': engine_edges}


def _validate_definition(data) -> str:
    """校验节点/边结构：nodes/edges 非空 + 引用完整性。返回错误信息或 None。"""
    nodes = data.get('nodes')
    edges = data.get('edges')
    if not isinstance(nodes, list) or not nodes:
        return _('nodes must be a non-empty list')
    if not isinstance(edges, list) or not edges:
        return _('edges must be a non-empty list')
    node_ids = set()
    for n in nodes:
        nid = n.get('id')
        if not nid:
            return _('each node requires an id')
        node_ids.add(nid)
    for e in edges:
        src = e.get('source') or e.get('from')
        tgt = e.get('target') or e.get('to')
        if not src or not tgt:
            return _('each edge requires source and target')
        if src not in node_ids or tgt not in node_ids:
            return f'edge references unknown node: {src} -> {tgt}'
    return None


def _validate_cron_expr(expr) -> str:
    """基本 cron 合法性校验（对齐 SchedulerEngine._schedule_job 的段数约定）。

    None/空串/'off' 视为"不调度"，合法；否则必须为 5 段（分 时 日 月 周）或
    6 段（秒 分 时 日 月 周）。返回错误信息或 None。
    """
    if expr is None:
        return None
    expr = str(expr).strip()
    if not expr or expr.lower() == 'off':
        return None
    if len(expr.split()) not in (5, 6):
        return f'Invalid cron_expr: "{expr}" (expected 5 or 6 fields)'
    return None


def _materialize_engine_definition(conn, def_row: dict) -> int:
    """将 workflow_defs 定义写透同步到引擎表 workflow_definitions（同一事务连接）。

    引擎按 DB id 取定义执行（WorkflowEngine.run_workflow），无 execute(definition)
    入口 —— 这是当前内核的最小桥接方案。返回引擎侧 engine_id。
    """
    engine_def = _to_engine_definition(def_row)
    enabled = 1 if def_row.get('enabled', True) else 0
    engine_id = def_row.get('engine_id')
    if engine_id:
        conn.execute(
            "UPDATE workflow_definitions SET name=%s, description=%s, definition=%s, "
            "is_active=%s, updated_at=NOW() WHERE id=%s",
            (def_row.get('name'), def_row.get('description', ''),
             m.to_json(engine_def), enabled, engine_id)
        )
        if conn.rowcount == 0:
            engine_id = None  # 引擎侧行已被外部删除 → 重建
    if not engine_id:
        conn.execute("""
            INSERT INTO workflow_definitions
                (name, description, version, is_active, agent_type, definition,
                 triggers, max_concurrency, timeout_minutes, on_error, created_by)
            VALUES (%s,%s,1,%s,'system',%s, '[]',1,60,'pause',%s)
            RETURNING id
        """, (def_row.get('name'), def_row.get('description', ''),
              enabled, m.to_json(engine_def), def_row.get('created_by', 0)))
        engine_id = conn.fetchone()['id']
        conn.execute(
            "UPDATE workflow_defs SET engine_id=%s WHERE id=%s",
            (engine_id, def_row['id'])
        )
    return engine_id


def _sync_run_from_engine(run: dict) -> dict:
    """懒刷新：从引擎 workflow_instances 同步 running 记录的状态。"""
    if run.get('status') != 'running':
        return run
    progress = m.from_json(run.get('progress', '{}'))
    inst_id = progress.get('instance_id')
    if not inst_id:
        return run
    inst = m.get_workflow_instance(inst_id)
    if not inst:
        return run
    updates = {}
    if inst.get('status') != 'running':
        updates['status'] = inst.get('status')
        updates['finished_at'] = inst.get('finished_at', '') or ''
        updates['error'] = inst.get('error_message', '') or ''
    progress['current_node_id'] = inst.get('current_node_id', '') or ''
    updates['progress'] = progress
    m.update_workflow_run(run['id'], updates)
    run.update(updates)
    return run


# ============================================================
# 1. 工作流定义
# ============================================================

@workflow_admin_bp.route('', methods=['GET'])
def list_workflow_defs():
    """列出工作流管理定义"""
    admin, err = _require_admin_or_403()
    if err:
        return err
    gate = _require_workflow_module(admin)
    if gate:
        return gate

    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    result = m.list_workflow_defs(page=page, limit=limit)
    for wf in result['workflows']:
        wf['nodes'] = m.from_json(wf.get('nodes', '[]'))
        wf['edges'] = m.from_json(wf.get('edges', '[]'))
    return _success(result)


@workflow_admin_bp.route('', methods=['POST'])
def create_workflow_def():
    """创建工作流管理定义（id 服务端生成 uuid；写透同步到引擎表）"""
    admin, err = _require_admin_or_403()
    if err:
        return err
    gate = _require_workflow_module(admin)
    if gate:
        return gate

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return _error(_('Workflow name cannot be empty'))
    err_msg = _validate_definition(data)
    if err_msg:
        return _error(err_msg)
    err_msg = _validate_cron_expr(data.get('cron_expr'))
    if err_msg:
        return _error(err_msg)

    # A5.3：危险节点分级授权。本端点是**第二条工作流管理面**（/admin/workflows），
    # 必须与 orchestrator.routes 的 /workflows 同口径，否则构成完整旁路
    # （A5 的收口目标随之失效）。
    guard = _guard_dangerous_nodes(_to_engine_definition(data), admin)
    if guard:
        return guard

    def_id = str(uuid.uuid4())
    enabled = bool(data.get('enabled', True))
    def_row = {
        'id': def_id,
        'name': name,
        'description': data.get('description', '') or '',
        'nodes': data.get('nodes'),
        'edges': data.get('edges'),
        'cron_expr': data.get('cron_expr') or None,
        'enabled': enabled,
        'created_by': admin.get('user_id') or admin.get('id') or 0,
    }
    with m.get_db() as conn:
        conn.execute("""
            INSERT INTO workflow_defs
                (id, name, description, nodes, edges, cron_expr, enabled, created_by)
            VALUES (%s,%s,%s,%s,%s,%s, %s,%s)
        """, (def_id, name, def_row['description'],
              m.to_json(def_row['nodes']), m.to_json(def_row['edges']),
              def_row['cron_expr'], enabled, def_row['created_by']))
        # 写透同步到引擎表（同一事务，保证原子）
        engine_id = _materialize_engine_definition(conn, def_row)

    # 事务已提交；按 cron_expr 同步真实定时调度（cron_jobs + SchedulerEngine）
    def_row['engine_id'] = engine_id
    _sync_workflow_cron(def_id, def_row)

    return _success({'workflow_id': def_id}, _('Workflow has been created'))


@workflow_admin_bp.route('/<def_id>', methods=['GET'])
def get_workflow_def(def_id):
    """获取工作流管理定义详情"""
    admin, err = _require_admin_or_403()
    if err:
        return err
    gate = _require_workflow_module(admin)
    if gate:
        return gate

    wf = m.get_workflow_def(def_id)
    if not wf:
        return _error(_('Workflow does not exist'), 404)
    wf['nodes'] = m.from_json(wf.get('nodes', '[]'))
    wf['edges'] = m.from_json(wf.get('edges', '[]'))
    return _success(wf)


@workflow_admin_bp.route('/<def_id>', methods=['PUT'])
def update_workflow_def(def_id):
    """更新工作流管理定义"""
    admin, err = _require_admin_or_403()
    if err:
        return err
    gate = _require_workflow_module(admin)
    if gate:
        return gate

    wf = m.get_workflow_def(def_id)
    if not wf:
        return _error(_('Workflow does not exist'), 404)

    data = request.get_json() or {}
    if not data:
        return _error(_('Updated data cannot be empty'))

    if 'nodes' in data or 'edges' in data:
        err_msg = _validate_definition(data)
        if err_msg:
            return _error(err_msg)
    if 'name' in data and not (data.get('name') or '').strip():
        return _error(_('Workflow name cannot be empty'))
    if 'cron_expr' in data:
        err_msg = _validate_cron_expr(data.get('cron_expr'))
        if err_msg:
            return _error(err_msg)

    # A5.3：本端点提交了节点/边时，按同一口径校验危险节点（与 /workflows 对齐）
    if 'nodes' in data or 'edges' in data:
        guard = _guard_dangerous_nodes(_to_engine_definition(data), admin)
        if guard:
            return guard

    update_data = {}
    for key in ('name', 'description', 'nodes', 'edges', 'cron_expr', 'enabled'):
        if key in data:
            update_data[key] = data[key]
    if not update_data:
        return _error(_('No Valid Fields'))

    engine_id = None
    fresh = None
    with m.get_db() as conn:
        fields = []
        values = []
        for key, v in update_data.items():
            fields.append(f"{key}=%s")
            if isinstance(v, (dict, list)):
                v = m.to_json(v)
            values.append(v)
        values.append(def_id)
        fields.append("updated_at=NOW()")
        conn.execute(
            f"UPDATE workflow_defs SET {', '.join(fields)} WHERE id=%s",
            values
        )
        # 重新读取并写透同步到引擎表（同一事务）
        conn.execute("SELECT * FROM workflow_defs WHERE id=%s", (def_id,))
        row = conn.fetchone()
        if row:
            fresh = dict(row)
            engine_id = _materialize_engine_definition(conn, fresh)

    # 事务已提交；按最新 cron_expr/enabled 同步定时调度（enabled 变更仅同步 is_active）
    if fresh:
        fresh['engine_id'] = engine_id
        _sync_workflow_cron(def_id, fresh)

    updated = m.get_workflow_def(def_id)
    if updated:
        updated['nodes'] = m.from_json(updated.get('nodes', '[]'))
        updated['edges'] = m.from_json(updated.get('edges', '[]'))
    return _success(updated, _('Workflow has been updated'))


@workflow_admin_bp.route('/<def_id>', methods=['DELETE'])
def delete_workflow_def(def_id):
    """删除工作流管理定义（级联删除运行记录与引擎侧定义/实例）"""
    admin, err = _require_admin_or_403()
    if err:
        return err
    gate = _require_workflow_module(admin)
    if gate:
        return gate

    wf = m.get_workflow_def(def_id)
    if not wf:
        return _error(_('Workflow does not exist'), 404)

    # 级联删除对应定时调度（cron_jobs + APScheduler 内存 job）
    _sync_workflow_cron(def_id, {'cron_expr': '', 'enabled': False})

    engine_id = wf.get('engine_id')
    if engine_id:
        m.delete_workflow(engine_id)  # 引擎侧：节点实例 → 实例 → 定义
    m.delete_workflow_def(def_id)
    return _success(None, _('Workflow has been deleted'))


# ============================================================
# 2. 工作流执行
# ============================================================

@workflow_admin_bp.route('/<def_id>/run', methods=['POST'])
def run_workflow_def(def_id):
    """手动触发工作流执行（复用 orchestrator 引擎，不重写 DAG 逻辑）"""
    admin, err = _require_admin_or_403()
    if err:
        return err
    gate = _require_workflow_module(admin)
    if gate:
        return gate

    wf = m.get_workflow_def(def_id)
    if not wf:
        return _error(_('Workflow does not exist'), 404)
    if not wf.get('enabled', True):
        return _error(_('Workflow has been disabled'), 400)

    engine = _get_engine()
    if engine is None:
        return _error(_('Worker pool not initialized'), 500)

    # 确保引擎侧定义存在（write-through；若引擎侧行被外部删除则重建）
    with m.get_db() as conn:
        conn.execute("SELECT * FROM workflow_defs WHERE id=%s", (def_id,))
        row = conn.fetchone()
        if row:
            engine_id = _materialize_engine_definition(conn, dict(row))
        else:
            return _error(_('Workflow does not exist'), 404)

    try:
        inst_id = engine.run_workflow(
            engine_id,
            trigger_type='manual',
            trigger_config={'admin_id': admin.get('user_id') or admin.get('id') or 0}
        )
    except ValueError as e:
        return _error(f'Launch failed: {str(e)}', 400)
    except Exception as e:
        return _error(f'Launch failed: {str(e)}', 500)

    run_id = m.record_workflow_run(def_id, inst_id, trigger_type='manual')
    return _success({'run_id': run_id, 'instance_id': inst_id, 'status': 'running'},
                    _('Workflow started'))


@workflow_admin_bp.route('/<def_id>/runs', methods=['GET'])
def list_workflow_runs(def_id):
    """工作流运行历史（running 记录懒刷新引擎状态）"""
    admin, err = _require_admin_or_403()
    if err:
        return err
    gate = _require_workflow_module(admin)
    if gate:
        return gate

    if not m.get_workflow_def(def_id):
        return _error(_('Workflow does not exist'), 404)

    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    result = m.list_workflow_runs(def_id, page=page, limit=limit)
    for run in result['runs']:
        _sync_run_from_engine(run)
        if isinstance(run.get('progress'), str):
            run['progress'] = m.from_json(run.get('progress', '{}'))
    return _success(result)


# ============================================================
# 3. 节点类型
# ============================================================

@workflow_admin_bp.route('/node-types', methods=['GET'])
def list_node_types():
    """返回 12 种节点类型清单（常量）"""
    admin, err = _require_admin_or_403()
    if err:
        return err
    return _success({'node_types': WORKFLOW_NODE_TYPES})
