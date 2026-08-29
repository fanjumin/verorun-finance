#!/usr/bin/env python3
"""
Orchestrator — 自动化调度中心数据库模型
=======================================
Cron 任务调度系统 + Workflow 工作流引擎的 SQLite 数据模型。

两种 智能体 区分：
- system_agents: 平台内置的系统 Agent，用于执行平台自动化任务
- agents (现有 users 表): 用户配置的 AI 分身 Agent

@package orchestrator
"""

from i18n import _
import json, time
from datetime import datetime
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PostgreSQL 连接配置
DB_CONFIG = {
    'host':     os.environ.get('PG_HOST', ''),
    'port':     int(os.environ.get('PG_PORT', 5432)),
    'dbname':   os.environ.get('PG_DB', 'appdb'),
    'user':     os.environ.get('PG_USER', 'app'),
    'password': os.environ.get('PG_PASSWORD', ''),
    'connect_timeout': 10,  # 建连最多等 10 秒，避免低配机器上无限挂死
}


@contextmanager
def get_db():
    """获取数据库 cursor（PostgreSQL）

    正常退出时自动 commit，发生异常时 rollback，最后关闭连接。
    返回 RealDictCursor — fetchone/fetchall 得到的行为类似 dict，同时支持下标访问。
    """
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def init_orchestrator_tables():
    """初始化调度器所有表（幂等：IF NOT EXISTS）"""
    with get_db() as conn:
        conn.execute("""
            -- =====================================================
            -- 1. 系统 Agent 配置表（平台自己的 AI 执行者）
            -- =====================================================
            CREATE TABLE IF NOT EXISTS system_agents (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL UNIQUE,
                description     TEXT DEFAULT '',
                provider        TEXT NOT NULL DEFAULT 'dashscope',
                model           TEXT NOT NULL DEFAULT 'qwen-turbo',
                api_key_ref     TEXT DEFAULT 'dashscope_text_key',
                base_url        TEXT DEFAULT '',
                system_prompt   TEXT DEFAULT '',
                capabilities    TEXT DEFAULT '[]',       -- JSON array
                max_concurrency BIGINT DEFAULT 1,
                is_active       BIGINT DEFAULT 1,
                created_at      TEXT DEFAULT NOW(),
                updated_at      TEXT DEFAULT NOW()
            );

            -- =====================================================
            -- 2. Cron 任务定义表
            -- =====================================================
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,
                description     TEXT DEFAULT '',

                -- 调度方式
                job_type        TEXT NOT NULL DEFAULT 'cron'
                                CHECK(job_type IN ('cron','interval','once')),
                cron_expr       TEXT DEFAULT '',          -- Standard Cron: '0 30 9 * * 1-5'
                natural_expr    TEXT DEFAULT '',          -- Natural Language: '每个交易日 9:30'
                interval_seconds BIGINT DEFAULT 0,       -- 固定间隔（秒）
                timezone        TEXT DEFAULT 'Asia/Shanghai',
                calendar        TEXT DEFAULT '{}',        -- JSON: {workdays_only, exclude_holidays, trade_days_only}

                -- 执行规划
                start_at        TEXT DEFAULT '',
                end_at          TEXT DEFAULT '',
                next_run_at     TEXT DEFAULT '',
                max_runs        BIGINT DEFAULT 0,        -- 0 = 无限制

                -- Agent 配置（两种 Agent 区分）
                agent_type      TEXT NOT NULL DEFAULT 'system'
                                CHECK(agent_type IN ('system','user')),
                agent_id        BIGINT DEFAULT NULL,     -- system: system_agents.id | user: agents.id

                -- 目标配置
                target_type     TEXT NOT NULL DEFAULT 'workflow'
                                CHECK(target_type IN ('workflow','api','script','agent_task')),
                target_config   TEXT NOT NULL DEFAULT '{}', -- JSON: 根据target_type不同

                -- 优先级与资源
                priority        TEXT NOT NULL DEFAULT 'normal'
                                CHECK(priority IN ('critical','high','normal','low')),
                worker_pool     TEXT DEFAULT 'shared',    -- 'shared' | 'dedicated'

                -- 重试策略
                max_retries     BIGINT DEFAULT 3,
                retry_delay     BIGINT DEFAULT 10,       -- 初次重试延迟（秒）
                retry_backoff   DOUBLE PRECISION DEFAULT 2.0,         -- 指数退避因子
                timeout_seconds BIGINT DEFAULT 300,

                -- 状态
                is_active       BIGINT DEFAULT 1,
                last_run_at     TEXT DEFAULT '',
                last_status     TEXT DEFAULT ''
                                CHECK(last_status IN ('','success','failed','running','timeout','cancelled')),
                last_duration_ms BIGINT DEFAULT 0,
                run_count       BIGINT DEFAULT 0,
                fail_count      BIGINT DEFAULT 0,

                -- 审计
                created_by      BIGINT DEFAULT 0,        -- users.id
                created_at      TEXT DEFAULT NOW(),
                updated_at      TEXT DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_cron_jobs_active
                ON cron_jobs(is_active, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_cron_jobs_type
                ON cron_jobs(job_type, priority);

            -- =====================================================
            -- 3. 任务依赖关系表（DAG 边）
            -- =====================================================
            CREATE TABLE IF NOT EXISTS job_dependencies (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                job_id          BIGINT NOT NULL REFERENCES cron_jobs(id) ON DELETE CASCADE,
                depends_on_job_id BIGINT NOT NULL REFERENCES cron_jobs(id) ON DELETE CASCADE,
                condition       TEXT NOT NULL DEFAULT 'success'
                                CHECK(condition IN ('success','failure','any','completed')),
                UNIQUE(job_id, depends_on_job_id)
            );

            -- =====================================================
            -- 4. 工作流定义表
            -- =====================================================
            CREATE TABLE IF NOT EXISTS workflow_definitions (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,
                description     TEXT DEFAULT '',
                version         BIGINT DEFAULT 1,
                is_active       BIGINT DEFAULT 1,

                -- Agent 配置
                agent_type      TEXT NOT NULL DEFAULT 'system'
                                CHECK(agent_type IN ('system','user')),
                agent_id        BIGINT DEFAULT NULL,

                -- DAG 定义（JSON）
                -- 结构: {
                --   "nodes": [{
                --     "id": "node_1",
                --     "type": "ai_agent|data_collect|ai_process|condition|approval|publish|notify|wait|sub_workflow|market_check",
                --     "name": _("Scrape 36Kr"),
                --     "config": {...},   -- 节点类型特定配置
                --     "position": {x, y}  -- 可视化编辑器坐标
                --   }],
                --   "edges": [{
                --     "from": "node_1",
                --     "to": "node_2",
                --     "condition": "_("  -- Conditional Branch: ")success"|"failure"|"${var} > 0.05"
                --   }]
                -- }
                definition      TEXT NOT NULL DEFAULT '{"nodes":[],"edges":[]}',

                -- 版本控制
                change_log      TEXT DEFAULT '',

                -- 触发方式（可多选）
                triggers        TEXT DEFAULT '[]',        -- JSON: [{type:"cron",config:{...}}]

                -- 并发控制
                max_concurrency BIGINT DEFAULT 1,
                timeout_minutes BIGINT DEFAULT 60,

                -- 错误处理
                on_error        TEXT DEFAULT 'pause'
                                CHECK(on_error IN ('pause','skip','retry','abort')),

                -- 审计
                created_by      BIGINT DEFAULT 0,
                created_at      TEXT DEFAULT NOW(),
                updated_at      TEXT DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_wf_active
                ON workflow_definitions(is_active, version);

            -- =====================================================
            -- 5. 工作流运行实例表
            -- =====================================================
            CREATE TABLE IF NOT EXISTS workflow_instances (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                workflow_id     BIGINT NOT NULL REFERENCES workflow_definitions(id),
                version         BIGINT DEFAULT 1,

                -- 状态机
                status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','running','paused',
                                                 'completed','failed','cancelled','timeout')),

                -- 触发信息
                trigger_type    TEXT NOT NULL DEFAULT 'manual'
                                CHECK(trigger_type IN ('cron','manual','webhook','event','dependency')),
                trigger_config  TEXT DEFAULT '{}',

                -- 运行时数据
                current_node_id TEXT DEFAULT '',
                context_data    TEXT DEFAULT '{}',        -- 全局上下文（JSON），节点间传递数据
                error_message   TEXT DEFAULT '',
                error_detail    TEXT DEFAULT '',

                -- 时间
                started_at      TEXT DEFAULT '',
                finished_at     TEXT DEFAULT '',
                duration_ms     BIGINT DEFAULT 0,

                -- Agent 实际执行者
                executed_by_agent TEXT DEFAULT '',
                executed_by_agent_id BIGINT DEFAULT NULL,

                created_at      TEXT DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_wfi_status
                ON workflow_instances(status, started_at);
            CREATE INDEX IF NOT EXISTS idx_wfi_wf
                ON workflow_instances(workflow_id, status);

            -- =====================================================
            -- 6. 工作流节点运行实例表
            -- =====================================================
            CREATE TABLE IF NOT EXISTS workflow_node_instances (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                workflow_instance_id BIGINT NOT NULL
                                    REFERENCES workflow_instances(id) ON DELETE CASCADE,
                node_id         TEXT NOT NULL,            -- 对应 definition 中的 node.id
                node_type       TEXT NOT NULL,
                node_name       TEXT DEFAULT '',

                -- 状态机
                status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','running','completed',
                                                 'failed','skipped','waiting_approval',
                                                 'waiting','timeout')),

                -- 输入输出
                input_data      TEXT DEFAULT '{}',        -- JSON
                output_data     TEXT DEFAULT '{}',        -- JSON
                error_message   TEXT DEFAULT '',
                error_detail    TEXT DEFAULT '',

                -- 重试
                retry_count     BIGINT DEFAULT 0,
                max_retries     BIGINT DEFAULT 3,

                -- 审批
                approval_status TEXT DEFAULT ''
                                CHECK(approval_status IN ('','pending','approved','rejected')),
                approved_by     BIGINT DEFAULT NULL,
                approved_at     TEXT DEFAULT '',

                -- 时间
                started_at      TEXT DEFAULT '',
                finished_at     TEXT DEFAULT '',
                duration_ms     BIGINT DEFAULT 0,

                -- 执行上下文（用于调试）
                log_snippet     TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_wni_instance
                ON workflow_node_instances(workflow_instance_id, node_id);
            CREATE INDEX IF NOT EXISTS idx_wni_status
                ON workflow_node_instances(status);

            -- =====================================================
            -- 7. 执行日志表
            -- =====================================================
            CREATE TABLE IF NOT EXISTS execution_logs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                source_type     TEXT NOT NULL
                                CHECK(source_type IN ('cron','workflow','node','system')),
                source_id       BIGINT DEFAULT 0,        -- cron_jobs.id / workflow_instances.id / workflow_node_instances.id
                level           TEXT NOT NULL DEFAULT 'info'
                                CHECK(level IN ('debug','info','warn','error','fatal')),
                message         TEXT NOT NULL,
                details         TEXT DEFAULT '{}',         -- JSON
                created_at      TEXT DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_el_source
                ON execution_logs(source_type, source_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_el_level
                ON execution_logs(level, created_at);

            -- =====================================================
            -- 8. 告警配置表
            -- =====================================================
            CREATE TABLE IF NOT EXISTS alerts (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,
                description     TEXT DEFAULT '',
                rule_type       TEXT NOT NULL
                                CHECK(rule_type IN ('job_failed','workflow_failed',
                                                    'timeout','node_failed','custom')),
                rule_config     TEXT DEFAULT '{}',         -- JSON: {source_type, source_id, threshold, ...}
                channel         TEXT NOT NULL DEFAULT 'notification'
                                CHECK(channel IN ('email','webhook','sms','notification','all')),
                channel_config  TEXT DEFAULT '{}',         -- JSON: {webhook_url, email_to, ...}
                is_active       BIGINT DEFAULT 1,
                throttle_minutes BIGINT DEFAULT 5,        -- 防重复
                last_triggered_at TEXT DEFAULT '',
                trigger_count   BIGINT DEFAULT 0,
                created_by      BIGINT DEFAULT 0,
                created_at      TEXT DEFAULT NOW()
            );

            -- =====================================================
            -- 9. 调度器节点状态表（分布式支持）
            -- =====================================================
            CREATE TABLE IF NOT EXISTS scheduler_state (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                scheduler_id    TEXT NOT NULL UNIQUE,      -- 实例唯一标识
                hostname        TEXT DEFAULT '',
                is_leader       BIGINT DEFAULT 0,
                last_heartbeat  TEXT DEFAULT NOW(),
                running_jobs    BIGINT DEFAULT 0,
                running_workflows BIGINT DEFAULT 0,
                state_json      TEXT DEFAULT '{}',
                started_at      TEXT DEFAULT NOW()
            );

            -- =====================================================
            -- 10. 工作流触发器表（事件驱动：发布即触发工作流）
            -- =====================================================
            CREATE TABLE IF NOT EXISTS workflow_triggers (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,
                trigger_event   TEXT NOT NULL,             -- 事件名, 如 'cms.published'/'content_factory.approved'
                workflow_id     BIGINT NOT NULL,          -- 要执行的 workflow_definitions.id
                match_condition TEXT DEFAULT '{}',         -- JSON 匹配条件, 空=无条件. 如 {"category":"news","source":"factory"}
                is_active       BIGINT DEFAULT 1,
                created_at      TEXT DEFAULT NOW(),
                updated_at      TEXT DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_wt_event
                ON workflow_triggers(trigger_event, is_active);

            -- 预置默认系统 Agent（仅当没有数据时插入）
            INSERT INTO system_agents (name, description, provider, model, api_key_ref, system_prompt, capabilities)
            VALUES ('default-system-agent', 'The platform defaults to automatically scheduling Agents to perform automated tasks such as content factory and market monitoring',
                    'dashscope', 'qwen-turbo', 'dashscope_text_key',
                    '你是平台的自动化调度助手。你的职责是执行定时任务、处理工作流、生成内容、监控市场数据。请严格按照任务要求输出结果。',
                    '["content_factory","market_monitor","data_analysis","report_generation"]')
            ON CONFLICT (name) DO NOTHING;
        """)


# ========== 辅助函数 ==========

def now_str():
    """当前时间字符串"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def to_json(obj):
    """序列化到 JSON 字符串"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def from_json(s, default=None):
    """从 JSON 字符串反序列化"""
    if not s:
        return default or {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default or {}


# ========== CRON 任务 CRUD ==========

def create_cron_job(data):
    """创建 Cron 任务"""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO cron_jobs
                (name, description, job_type, cron_expr, natural_expr,
                 interval_seconds, timezone, calendar, start_at, end_at,
                 next_run_at, max_runs, agent_type, agent_id,
                 target_type, target_config, priority, worker_pool,
                 max_retries, retry_delay, retry_backoff, timeout_seconds,
                 is_active, created_by)
            VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s)
            RETURNING id
        """, (
            data.get('name'), data.get('description', ''),
            data.get('job_type', 'cron'), data.get('cron_expr', ''),
            data.get('natural_expr', ''),
            data.get('interval_seconds', 0),
            data.get('timezone', 'Asia/Shanghai'),
            to_json(data.get('calendar', {})),
            data.get('start_at', ''), data.get('end_at', ''),
            data.get('next_run_at', ''), data.get('max_runs', 0),
            data.get('agent_type', 'system'), data.get('agent_id'),
            data.get('target_type', 'workflow'),
            to_json(data.get('target_config', {})),
            data.get('priority', 'normal'),
            data.get('worker_pool', 'shared'),
            data.get('max_retries', 3), data.get('retry_delay', 10),
            data.get('retry_backoff', 2.0), data.get('timeout_seconds', 300),
            data.get('is_active', 1), data.get('created_by', 0)
        ))
        return conn.fetchone()['id']


def update_cron_job(job_id, data):
    """更新 Cron 任务"""
    fields = []
    values = []
    for key in ('name','description','job_type','cron_expr','natural_expr',
                'interval_seconds','timezone','calendar','start_at','end_at',
                'next_run_at','max_runs','agent_type','agent_id',
                'target_type','target_config','priority','worker_pool',
                'max_retries','retry_delay','retry_backoff','timeout_seconds',
                'is_active','last_run_at','last_status','last_duration_ms'):
        if key in data:
            fields.append(f"{key}=%s")
            v = data[key]
            if key in ('calendar', 'target_config') and isinstance(v, dict):
                v = to_json(v)
            values.append(v)
    if not fields:
        return False
    values.append(job_id)
    with get_db() as conn:
        fields.append("updated_at=NOW()")
        conn.execute(
            f"UPDATE cron_jobs SET {', '.join(fields)} WHERE id=%s",
            values
        )
        return conn.rowcount > 0


def get_cron_job(job_id):
    """获取单个任务"""
    with get_db() as conn:
        conn.execute("SELECT * FROM cron_jobs WHERE id=%s", (job_id,))
        row = conn.fetchone()
        return dict(row) if row else None


def list_cron_jobs(active_only=False, page=1, limit=50, priority=None):
    """列出 Cron 任务"""
    where = ["1=1"]
    params = []
    if active_only:
        where.append("is_active=1")
    if priority:
        where.append("priority=%s")
        params.append(priority)
    offset = (page - 1) * limit
    with get_db() as conn:
        conn.execute(
            f"SELECT COUNT(*) FROM cron_jobs WHERE {' AND '.join(where)}", params
        )
        total = conn.fetchone()['count']
        conn.execute(
            f"SELECT * FROM cron_jobs WHERE {' AND '.join(where)} "
            f"ORDER BY priority DESC, created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        rows = conn.fetchall()
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "jobs": [dict(r) for r in rows]
        }


def delete_cron_job(job_id):
    """删除任务（级联删除依赖）"""
    with get_db() as conn:
        conn.execute("DELETE FROM job_dependencies WHERE job_id=%s OR depends_on_job_id=%s", (job_id, job_id))
        conn.execute("DELETE FROM cron_jobs WHERE id=%s", (job_id,))
        return conn.rowcount > 0


# ========== 工作流 CRUD ==========

def create_workflow(data):
    """创建工作流定义"""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO workflow_definitions
                (name, description, version, is_active,
                 agent_type, agent_id, definition, change_log,
                 triggers, max_concurrency, timeout_minutes, on_error,
                 created_by)
            VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s)
            RETURNING id
        """, (
            data.get('name'), data.get('description', ''),
            data.get('version', 1), data.get('is_active', 1),
            data.get('agent_type', 'system'), data.get('agent_id'),
            to_json(data.get('definition', {"nodes":[],"edges":[]})),
            data.get('change_log', ''),
            to_json(data.get('triggers', [])),
            data.get('max_concurrency', 1),
            data.get('timeout_minutes', 60),
            data.get('on_error', 'pause'),
            data.get('created_by', 0)
        ))
        return conn.fetchone()['id']


def update_workflow(wf_id, data):
    """更新工作流定义（版本递增）"""
    fields = []
    values = []
    for key in ('name','description','is_active','agent_type','agent_id',
                'definition','change_log','triggers','max_concurrency',
                'timeout_minutes','on_error'):
        if key in data:
            fields.append(f"{key}=%s")
            v = data[key]
            if isinstance(v, (dict, list)):
                v = to_json(v)
            values.append(v)
    if not fields:
        return False
    fields.append("version=version+1")
    fields.append("updated_at=NOW()")
    values.append(wf_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE workflow_definitions SET {', '.join(fields)} WHERE id=%s",
            values
        )
        return conn.rowcount > 0


def get_workflow(wf_id):
    """获取工作流定义"""
    with get_db() as conn:
        conn.execute(
            "SELECT * FROM workflow_definitions WHERE id=%s", (wf_id,)
        )
        row = conn.fetchone()
        return dict(row) if row else None


def list_workflows(active_only=False, page=1, limit=50):
    """列出工作流定义"""
    where = ["1=1"]
    params = []
    if active_only:
        where.append("is_active=1")
    offset = (page - 1) * limit
    with get_db() as conn:
        conn.execute(
            f"SELECT COUNT(*) FROM workflow_definitions WHERE {' AND '.join(where)}",
            params
        )
        total = conn.fetchone()['count']
        conn.execute(
            f"SELECT * FROM workflow_definitions WHERE {' AND '.join(where)} "
            f"ORDER BY updated_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        rows = conn.fetchall()
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "workflows": [dict(r) for r in rows]
        }


def delete_workflow(wf_id):
    """删除工作流（级联删除关联实例和节点）"""
    with get_db() as conn:
        # 先删除节点实例
        conn.execute("""DELETE FROM workflow_node_instances WHERE workflow_instance_id IN 
                        (SELECT id FROM workflow_instances WHERE workflow_id=%s)""", (wf_id,))
        # 再删除实例
        conn.execute("DELETE FROM workflow_instances WHERE workflow_id=%s", (wf_id,))
        # 最后删除定义
        conn.execute("DELETE FROM workflow_definitions WHERE id=%s", (wf_id,))
        return conn.rowcount > 0


# ========== 工作流实例 ==========

def create_workflow_instance(workflow_id, trigger_type='manual', trigger_config=None):
    """创建工作流运行实例"""
    wf = get_workflow(workflow_id)
    if not wf:
        return None
    defn = from_json(wf.get('definition', '{}'))
    with get_db() as conn:
        conn.execute("""
            INSERT INTO workflow_instances
                (workflow_id, version, trigger_type, trigger_config,
                 status, context_data, started_at)
            VALUES (%s,%s,%s,%s, 'running', '{}', %s)
            RETURNING id
        """, (
            workflow_id, wf.get('version', 1),
            trigger_type, to_json(trigger_config or {}),
            now_str()
        ))
        inst_id = conn.fetchone()['id']

        # 创建所有节点的实例记录
        nodes = defn.get('nodes', [])
        for node in nodes:
            conn.execute("""
                INSERT INTO workflow_node_instances
                    (workflow_instance_id, node_id, node_type, node_name,
                     status, input_data, max_retries)
                VALUES (%s,%s,%s,%s, 'pending', '{}', %s)
            """, (
                inst_id, node.get('id', ''),
                node.get('type', ''), node.get('name', ''),
                node.get('max_retries', 3)
            ))

        return inst_id


def update_workflow_instance(inst_id, updates):
    """更新工作流实例状态"""
    fields = []
    values = []
    for key in ('status','current_node_id','context_data','error_message',
                'error_detail','finished_at','duration_ms',
                'executed_by_agent','executed_by_agent_id'):
        if key in updates:
            fields.append(f"{key}=%s")
            values.append(updates[key])
    if not fields:
        return False
    values.append(inst_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE workflow_instances SET {', '.join(fields)} WHERE id=%s",
            values
        )
        return conn.rowcount > 0


def get_workflow_instance(inst_id):
    """获取工作流实例"""
    with get_db() as conn:
        conn.execute(
            "SELECT * FROM workflow_instances WHERE id=%s", (inst_id,)
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_running_instances(workflow_id: int) -> list:
    """获取指定工作流正在运行的实例"""
    with get_db() as conn:
        conn.execute(
            "SELECT id FROM workflow_instances WHERE workflow_id=%s AND status='running'",
            (workflow_id,)
        )
        return [dict(row) for row in conn.fetchall()]


def list_workflow_instances(workflow_id=None, status=None, page=1, limit=50):
    """列出工作流运行实例"""
    where = ["1=1"]
    params = []
    if workflow_id:
        where.append("workflow_id=%s")
        params.append(workflow_id)
    if status:
        where.append("status=%s")
        params.append(status)
    offset = (page - 1) * limit
    with get_db() as conn:
        conn.execute(
            f"SELECT COUNT(*) FROM workflow_instances WHERE {' AND '.join(where)}",
            params
        )
        total = conn.fetchone()['count']
        conn.execute(
            f"SELECT * FROM workflow_instances WHERE {' AND '.join(where)} "
            f"ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        rows = conn.fetchall()
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "instances": [dict(r) for r in rows]
        }


# ========== 节点实例 ==========

def update_node_instance(node_inst_id, updates):
    """更新节点实例状态"""
    fields = []
    values = []
    for key in ('status','input_data','output_data','error_message',
                'error_detail','retry_count','started_at','finished_at',
                'duration_ms','log_snippet','approval_status',
                'approved_by','approved_at'):
        if key in updates:
            fields.append(f"{key}=%s")
            v = updates[key]
            if isinstance(v, (dict, list)):
                v = to_json(v)
            values.append(v)
    if not fields:
        return False
    values.append(node_inst_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE workflow_node_instances SET {', '.join(fields)} WHERE id=%s",
            values
        )
        return conn.rowcount > 0


def get_node_instance(node_inst_id):
    """获取节点实例"""
    with get_db() as conn:
        conn.execute(
            "SELECT * FROM workflow_node_instances WHERE id=%s", (node_inst_id,)
        )
        row = conn.fetchone()
        return dict(row) if row else None


def get_node_instances_by_workflow(inst_id):
    """获取工作流所有节点实例"""
    with get_db() as conn:
        conn.execute(
            "SELECT * FROM workflow_node_instances WHERE workflow_instance_id=%s ORDER BY id",
            (inst_id,)
        )
        rows = conn.fetchall()
        return [dict(r) for r in rows]


# ========== 日志 ==========

def add_log(source_type, source_id, level, message, details=None):
    """添加执行日志"""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO execution_logs (source_type, source_id, level, message, details)
            VALUES (%s,%s,%s,%s,%s)
        """, (source_type, source_id, level, message, to_json(details or {})))


def query_logs(source_type=None, source_id=None, level=None, page=1, limit=100):
    """查询日志"""
    where = ["1=1"]
    params = []
    if source_type:
        where.append("source_type=%s")
        params.append(source_type)
    if source_id:
        where.append("source_id=%s")
        params.append(source_id)
    if level:
        where.append("level=%s")
        params.append(level)
    offset = (page - 1) * limit
    with get_db() as conn:
        conn.execute(
            f"SELECT COUNT(*) FROM execution_logs WHERE {' AND '.join(where)}",
            params
        )
        total = conn.fetchone()['count']
        conn.execute(
            f"SELECT * FROM execution_logs WHERE {' AND '.join(where)} "
            f"ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        rows = conn.fetchall()
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "logs": [dict(r) for r in rows]
        }


# ========== 统计 ==========

def get_automation_stats():
    """获取自动化系统统计概览"""
    with get_db() as conn:
        conn.execute("SELECT COUNT(*) FROM cron_jobs")
        total_jobs = conn.fetchone()['count']
        conn.execute("SELECT COUNT(*) FROM cron_jobs WHERE is_active=1")
        active_jobs = conn.fetchone()['count']
        conn.execute("SELECT COUNT(*) FROM workflow_definitions")
        total_wfs = conn.fetchone()['count']
        conn.execute(
            "SELECT COUNT(*) FROM workflow_instances WHERE status IN ('running','paused')"
        )
        running_instances = conn.fetchone()['count']
        conn.execute(
            "SELECT COUNT(*) FROM workflow_instances WHERE status='completed' AND finished_at::date = CURRENT_DATE"
        )
        completed_today = conn.fetchone()['count']
        conn.execute(
            "SELECT COUNT(*) FROM workflow_instances WHERE status='failed' AND finished_at::date = CURRENT_DATE"
        )
        failed_today = conn.fetchone()['count']
        conn.execute(
            "SELECT COALESCE(AVG(duration_ms),0) AS avg_duration FROM workflow_instances WHERE status='completed' AND finished_at::date = CURRENT_DATE"
        )
        avg_duration = conn.fetchone()['avg_duration']

        # 获取最近失败的
        conn.execute("""
            SELECT wi.id, w.name, wi.status, wi.error_message, wi.finished_at
            FROM workflow_instances wi
            LEFT JOIN workflow_definitions w ON wi.workflow_id = w.id
            WHERE wi.status IN ('failed','timeout')
            ORDER BY wi.finished_at DESC LIMIT 5
        """)
        recent_failures = conn.fetchall()

        return {
            "total_jobs": total_jobs,
            "active_jobs": active_jobs,
            "total_workflows": total_wfs,
            "running_instances": running_instances,
            "completed_today": completed_today,
            "failed_today": failed_today,
            "avg_duration_ms": avg_duration,
            "recent_failures": [dict(r) for r in recent_failures]
        }


# ========== 系统 Agent ==========

def get_default_system_agent():
    """获取默认系统 Agent"""
    with get_db() as conn:
        conn.execute(
            "SELECT * FROM system_agents WHERE is_active=1 ORDER BY id LIMIT 1"
        )
        row = conn.fetchone()
        return dict(row) if row else None


def list_system_agents():
    """列出所有系统 Agent"""
    with get_db() as conn:
        conn.execute("SELECT * FROM system_agents ORDER BY id")
        rows = conn.fetchall()
        return [dict(r) for r in rows]
