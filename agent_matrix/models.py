#!/usr/bin/env python3
"""
Agent Matrix — 数据库模型
======================
4 张新表 + CRUD 操作 + 种子数据（基于 YAML 角色定义）。
复用 auth-center/models/database.py 的 get_db() 模式。
"""
from i18n import _
import json, os, sys, re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROLES_DIR = os.path.join(BASE_DIR, 'roles')

# ── 发行版 edition 归一化（单一事实源：VR_EDITION / RELEASE_EDITION / DEPLOY_TYPE）──
# 历史旧版名归一为现行版名：edu/research→research-desktop、pro/finance→finance-desktop；
# 其余（standard/official/空）视为全量版。
def normalize_edition(e) -> str:
    """归一化版本标识（大小写不敏感）：旧名→现行版名，空→standard。"""
    e = (e or '').strip().lower()
    if e in ('edu', 'research', 'research-desktop'):
        return 'research-desktop'
    if e in ('pro', 'finance', 'finance-desktop'):
        return 'finance-desktop'
    return e or 'standard'


def current_edition() -> str:
    """全系统发行版唯一判定：VR_EDITION → RELEASE_EDITION → DEPLOY_TYPE。"""
    return normalize_edition(os.getenv('VR_EDITION') or os.getenv('RELEASE_EDITION')
                             or os.getenv('DEPLOY_TYPE') or '')


# 兼容别名：早期调用方沿用下划线私有名（agent_matrix/routes.py 等）
_current_edition = current_edition

# ── Edition 服务启用/禁用（单一事实源：deploy/editions/<edition>.yaml 的 services: 段）──
# 与角色 YAML 的 _parse_role_yaml 无关，独立解析避免影响角色加载。
EDITIONS_DIR = os.path.join(BASE_DIR, '..', 'deploy', 'editions')


def _parse_edition_services(text):
    """从 edition yaml 提取 services: 段，返回 {服务名: bool}。

    仅识别顶层 services: 下的缩进子键（true/false），其余内容忽略。
    """
    services = {}
    in_services = False
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith('#'):
            continue
        # 顶层 services: 开启块
        if re.match(r'^services:\s*$', line):
            in_services = True
            continue
        if in_services and line.startswith(' '):
            m = re.match(r'^ +(\w[\w_]*)\s*:\s*(true|false)\s*$', line)
            if m:
                services[m.group(1)] = (m.group(2).lower() == 'true')
        elif re.match(r'^\w[\w_]*\s*:', line):
            # 遇到其他顶层 key → 离开 services 段
            in_services = False
    return services


def edition_services() -> dict:
    """当前 edition 的服务开关表；yaml 缺失 / 无 services 段 → 空 dict（调用方按全开处理）。"""
    edition = current_edition()
    path = os.path.join(EDITIONS_DIR, f'{edition}.yaml')
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return _parse_edition_services(f.read())
    except Exception:
        return {}


def is_service_enabled(name: str) -> bool:
    """服务是否启用：未声明（yaml 缺失 / 无 services 段 / 无该键）→ 默认 True（全开兜底）。"""
    return edition_services().get(name, True)


def _parse_edition_flag(text, key):
    """从 edition yaml 顶层读一个布尔开关（'key: true|false'）；未声明返回 None。"""
    m = re.search(r'^%s:\s*(true|false)\s*$' % re.escape(key), text, re.M | re.I)
    return (m.group(1).lower() == 'true') if m else None


def edition_requires_license() -> bool:
    """本发行版是否强制「许可证/订阅到期即锁」。

    单一事实源：deploy/editions/<edition>.yaml 顶层 `license_enforcement`。
    语义与 is_service_enabled() 的「缺省即全开」**刻意相反** —— 授权强制是产品商业
    决策，未显式声明的版（official / research / 未设版本键的老部署）一律不启用，
    否则一次改动会把所有发行版锁死。
    桌面金融版显式置 true（2026-09-11 产品决策：订阅到期即锁，fail-closed）。
    """
    path = os.path.join(EDITIONS_DIR, f'{current_edition()}.yaml')
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return bool(_parse_edition_flag(f.read(), 'license_enforcement'))
    except OSError:
        return False


# ── Edition 插件白名单（deploy/editions/<edition>.yaml 的 plugins: include/exclude）──
def _parse_edition_plugin_lists(text):
    """从 edition yaml 提取 plugins: 段的 include/exclude 列表。

    仅识别顶层 plugins: 下 include:/exclude: 块的 '  - item' 行，其余内容忽略。
    """
    lists = {'include': [], 'exclude': []}
    section = None  # None | 'plugins' | 'include' | 'exclude'
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith('#'):
            continue
        if re.match(r'^plugins:\s*$', line):
            section = 'plugins'
            continue
        if line.startswith(' '):
            m = re.match(r'^ +(\w+)\s*:\s*$', line)
            if m:
                if m.group(1) in lists:
                    section = m.group(1)
                else:
                    section = 'plugins'
                continue
            li = re.match(r'^ +- +(.+)$', line)
            if li and section in lists:
                lists[section].append(li.group(1).strip())
            continue
        if re.match(r'^\w[\w_]*\s*:', line):
            section = None
    return lists


def edition_plugin_lists() -> dict:
    """当前 edition 的插件 include/exclude 白名单。

    standard（未设 VR_EDITION 的老部署）语义与官方一致，归一为 official；
    yaml 缺失 / 无 plugins 段 → 空列表（不排除任何插件）。
    """
    edition = current_edition()
    if edition in ('', 'standard'):
        edition = 'official'
    path = os.path.join(EDITIONS_DIR, f'{edition}.yaml')
    if not os.path.isfile(path):
        return {'include': [], 'exclude': []}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return _parse_edition_plugin_lists(f.read())
    except Exception:
        return {'include': [], 'exclude': []}


def edition_plugin_excludes() -> list:
    """当前 edition 插件排除名单（插件列表隐藏 / enable 拒绝共用）。"""
    return edition_plugin_lists().get('exclude', []) or []


def edition_plugin_includes() -> list:
    """当前 edition 的插件白名单。

    ⚠️ 返回空列表 = 该发行版**未启用白名单语义**（standard/未设版本键的老部署，
    或 yaml 缺 plugins 段）→ 调用方绝不能据此过滤，否则会把所有插件判为"本版不提供"。
    服务器版由 sync-to-pro.yml 按 include 物理拷贝（空 include 直接 fail），
    故运行时同样遵守 include 即为与该产物口径对齐。
    """
    return edition_plugin_lists().get('include', []) or []


# ── 复用主应用 PostgreSQL 连接 ──
sys.path.append(os.path.join(BASE_DIR, '..', 'auth-center', 'models'))
from database import get_db, get_table_columns


# ── 轻量 YAML 解析器（仅支持角色定义所需子集） ──
def _parse_role_yaml(text):
    """解析简单的 flat key:value / key:\\n  - item 格式 YAML，返回 dict。"""
    data = {}
    current_list_key = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        # key: value 行（非缩进）
        m = re.match(r'^(\w[\w_]*)\s*:\s*(.*)', line)
        if m and not line.startswith(' '):
            key, val = m.group(1), m.group(2).strip()
            current_list_key = None
            if val == '':
                # 后面跟列表项
                current_list_key = key
                data[key] = []
            else:
                data[key] = val
        elif stripped.startswith('- ') and current_list_key:
            data[current_list_key].append(stripped[2:].strip())
        else:
            current_list_key = None
    return data


def _to_int(val, default=0):
    """安全转换：'true'/'false'/数字字符串 → int"""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        v = val.strip().lower()
        if v == 'true':
            return 1
        if v == 'false':
            return 0
        try:
            return int(v)
        except ValueError:
            return default
    return default


def _role_dir_for_edition() -> str:
    """当前发行版角色目录：agent_matrix/roles/<edition>/ 存在则用该子目录，
    否则回退 roles/ 根（官方默认版）。方向版（research/finance/edge…）角色
    目录各自独立，官方角色与方向版角色互不混载。"""
    edition = current_edition()
    if edition:
        sub = os.path.join(ROLES_DIR, edition)
        if os.path.isdir(sub):
            return sub
    return ROLES_DIR


def _load_all_role_yamls():
    """从当前发行版角色目录加载所有 .yaml 文件，返回角色 dict 列表。"""
    roles_dir = _role_dir_for_edition()
    roles = []
    if not os.path.isdir(roles_dir):
        return roles
    _edition = _current_edition()
    for fname in sorted(os.listdir(roles_dir)):
        if not fname.endswith('.yaml') and not fname.endswith('.yml'):
            continue
        fpath = os.path.join(roles_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                raw = _parse_role_yaml(f.read())
            # edition 角色集过滤（editions 字段缺省 = 全版本包含）。
            # 方向版只加载归属角色；standard/official 全量。
            if _edition in ('research', 'research-desktop', 'finance', 'finance-desktop'):
                _owned = raw.get('editions') or []
                if _owned and _edition not in _owned:
                    print(f'[RoleYAML] edition {_edition} skips role: {raw.get("slug")}')
                    continue
            # 类型转换
            raw['is_active'] = _to_int(raw.get('is_active', 1))
            raw['is_system'] = _to_int(raw.get('is_system', 0))
            raw['auto_approve'] = _to_int(raw.get('auto_approve', 0))
            raw['managed_modules'] = json.dumps(raw.get('managed_modules', []))
            raw['capabilities'] = json.dumps(raw.get('capabilities', []))
            raw['allowed_tools'] = json.dumps(raw.get('allowed_tools', []))
            roles.append(raw)
        except Exception as e:
            print(f'[RoleYAML] Skipped {fname}: {e}')
    return roles


# ============================================================
# Task ID 生成器（UUID 后缀，杜绝并发冲突）
# ============================================================

def _next_task_id():
    """生成唯一任务 ID，使用随机十六进制后缀避免跨进程冲突"""
    import secrets
    date = datetime.now().strftime('%Y%m%d')
    return f'AT-{date}-{secrets.token_hex(4).upper()}'


def _next_session_id():
    date = datetime.now().strftime('%Y%m%d')
    import secrets
    suffix = secrets.token_hex(4)
    return f'SESSION-{date}-{suffix}'


def is_valid_session_id(session_id: str) -> bool:
    """P1-F09: 校验 session_id 格式，防止路径穿越。
    只允许 SESSION-YYYYMMDD-{hex8} 或 ^[A-Za-z0-9_-]{8,64}$ 格式。
    """
    import re
    if not session_id:
        return False
    # 服务端生成的格式
    if re.match(r'^SESSION-\d{8}-[a-f0-9]{8}$', session_id):
        return True
    # 通用白名单：字母数字/下划线/连字符，8-64字符
    if re.match(r'^[A-Za-z0-9_-]{8,64}$', session_id):
        return True
    return False


def get_master_agent_config():
    """从 agent_matrix 表中读取 Master Agent 的模型配置（通过 provider_model_id）"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT provider_model_id FROM agent_matrix WHERE role_type='master' LIMIT 1"
            ).fetchone()
            if row and row['provider_model_id']:
                return {'provider_model_id': row['provider_model_id']}
    except Exception:
        pass
    return {}


# ============================================================
# 建表
# ============================================================

def init_agent_matrix_tables():
    """创建 4 张新表（幂等）"""
    with get_db() as conn:
        conn.executescript("""
            -- ================================================
            -- 1. Agent 矩阵配置表
            -- ================================================
            CREATE TABLE IF NOT EXISTS agent_matrix (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,
                slug            TEXT DEFAULT '',
                role_type       TEXT NOT NULL DEFAULT 'sub'
                                CHECK(role_type IN ('master','sub')),
                description     TEXT DEFAULT '',
                domain          TEXT NOT NULL DEFAULT 'general',
                managed_modules TEXT DEFAULT '[]',
                provider        TEXT NOT NULL DEFAULT 'dashscope',
                model_name      TEXT NOT NULL DEFAULT 'qwen-turbo',
                api_key_ref     TEXT DEFAULT 'dashscope_text_key',
                base_url        TEXT DEFAULT '',
                model_provider_id BIGINT DEFAULT NULL,  -- deprecated
                provider_model_id BIGINT DEFAULT NULL,
                system_prompt   TEXT DEFAULT '',
                role_prompt     TEXT DEFAULT '',
                task_template   TEXT DEFAULT '',
                capabilities    TEXT DEFAULT '[]',
                allowed_tools   TEXT DEFAULT '[]',
                max_concurrency BIGINT DEFAULT 1,
                priority        BIGINT DEFAULT 5,
                auto_approve    BIGINT DEFAULT 0,
                is_active       BIGINT DEFAULT 1,
                is_system       BIGINT DEFAULT 0,
                tasks_total     BIGINT DEFAULT 0,
                tasks_success   BIGINT DEFAULT 0,
                tasks_failed    BIGINT DEFAULT 0,
                last_run_at     TEXT DEFAULT '',
                created_at      TEXT DEFAULT (NOW()),
                updated_at      TEXT DEFAULT (NOW()),
                UNIQUE(name, role_type)
            );

            -- ================================================
            -- 2. 任务调度表
            -- ================================================
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                task_id         TEXT UNIQUE NOT NULL,
                parent_task_id  TEXT DEFAULT NULL,
                master_task_id  TEXT DEFAULT NULL,
                source_agent_id BIGINT NOT NULL,
                target_agent_id BIGINT NOT NULL,
                task_type       TEXT NOT NULL DEFAULT 'execute'
                                CHECK(task_type IN ('execute','review','approve','composite','cron')),
                title           TEXT NOT NULL,
                description     TEXT DEFAULT '',
                input_data      TEXT DEFAULT '{}',
                expected_output TEXT DEFAULT '{}',
                target_module   TEXT DEFAULT '',
                target_api      TEXT DEFAULT '',
                priority        BIGINT DEFAULT 5,
                max_retries     BIGINT DEFAULT 3,
                retry_count     BIGINT DEFAULT 0,
                timeout_seconds BIGINT DEFAULT 300,
                status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','running','completed','failed',
                                                 'cancelled','needs_review','retrying')),
                result_data     TEXT DEFAULT '{}',
                confidence      DOUBLE PRECISION DEFAULT 0.0,
                error_message   TEXT DEFAULT '',
                self_review     TEXT DEFAULT '',
                cross_review    TEXT DEFAULT '',
                created_at      TEXT DEFAULT (NOW()),
                started_at      TEXT DEFAULT '',
                completed_at    TEXT DEFAULT '',
                updated_at      TEXT DEFAULT (NOW())
            );

            CREATE INDEX IF NOT EXISTS idx_at_status ON agent_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_at_source ON agent_tasks(source_agent_id);
            CREATE INDEX IF NOT EXISTS idx_at_target ON agent_tasks(target_agent_id);
            CREATE INDEX IF NOT EXISTS idx_at_master ON agent_tasks(master_task_id);
            CREATE INDEX IF NOT EXISTS idx_at_module ON agent_tasks(target_module);

            -- ================================================
            -- 3. 执行日志表
            -- ================================================
            CREATE TABLE IF NOT EXISTS task_logs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                task_id         TEXT NOT NULL,
                agent_id        BIGINT NOT NULL,
                log_level       TEXT NOT NULL DEFAULT 'info'
                                CHECK(log_level IN ('debug','info','warn','error')),
                log_type        TEXT NOT NULL DEFAULT 'execution'
                                CHECK(log_type IN ('execution','self_review','cross_review','approval','api_call')),
                message         TEXT NOT NULL,
                metadata        TEXT DEFAULT '{}',
                created_at      TEXT DEFAULT (NOW())
            );

            CREATE INDEX IF NOT EXISTS idx_tl_task ON task_logs(task_id);
            CREATE INDEX IF NOT EXISTS idx_tl_type ON task_logs(log_type);

            -- ================================================
            -- 4. 对话记录表
            -- ================================================
            CREATE TABLE IF NOT EXISTS agent_conversations (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                master_task_id  TEXT DEFAULT '',
                session_id      TEXT NOT NULL,
                role            TEXT NOT NULL CHECK(role IN ('user','master','sub','system')),
                agent_id        BIGINT DEFAULT NULL,
                agent_name      TEXT DEFAULT '',
                content         TEXT NOT NULL,
                metadata        TEXT DEFAULT '{}',
                created_at      TEXT DEFAULT (NOW())
            );

            CREATE INDEX IF NOT EXISTS idx_ac_session ON agent_conversations(session_id);
            CREATE INDEX IF NOT EXISTS idx_ac_task ON agent_conversations(master_task_id);

            -- ================================================
            -- 5. Token 消耗日志表 (2026-05-16)
            -- ================================================
            CREATE TABLE IF NOT EXISTS agent_token_logs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                agent_id        BIGINT NOT NULL,
                agent_name      TEXT DEFAULT '',
                model_name      TEXT DEFAULT '',
                provider        TEXT DEFAULT '',
                prompt_tokens   BIGINT DEFAULT 0,
                completion_tokens BIGINT DEFAULT 0,
                total_tokens    BIGINT DEFAULT 0,
                call_type       TEXT DEFAULT 'chat',
                dimension       TEXT DEFAULT 'text',
                user_id         BIGINT DEFAULT NULL,
                task_id         TEXT DEFAULT '',
                session_id      TEXT DEFAULT '',
                created_at      TEXT DEFAULT (NOW())
            );

            CREATE INDEX IF NOT EXISTS idx_tkl_agent_id   ON agent_token_logs(agent_id);
            CREATE INDEX IF NOT EXISTS idx_tkl_created_at ON agent_token_logs(created_at);
            -- idx_tkl_date and idx_tkl_agent_date removed:
            -- date(text) is STABLE, not IMMUTABLE — PostgreSQL requires IMMUTABLE for index expressions

            -- ================================================
            -- 6. Token 每日汇总表 (2026-05-16)
            -- ================================================
            CREATE TABLE IF NOT EXISTS agent_token_daily (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                agent_id        BIGINT NOT NULL,
                agent_name      TEXT DEFAULT '',
                stat_date       TEXT NOT NULL DEFAULT (CURRENT_DATE),
                prompt_tokens   BIGINT DEFAULT 0,
                completion_tokens BIGINT DEFAULT 0,
                total_tokens    BIGINT DEFAULT 0,
                call_count      BIGINT DEFAULT 0,
                updated_at      TEXT DEFAULT (NOW()),
                UNIQUE(agent_id, stat_date)
            );

            CREATE INDEX IF NOT EXISTS idx_tkd_date ON agent_token_daily(stat_date);

            -- ================================================
            -- 7. 模块用量日志表 (Phase 1 模块化订阅)
            -- ================================================
            CREATE TABLE IF NOT EXISTS module_usage_log (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                module_key  TEXT NOT NULL,
                agent_id    BIGINT NOT NULL,
                task_id     TEXT NOT NULL,
                used_at     TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_mul_user_module ON module_usage_log(user_id, module_key);
            CREATE INDEX IF NOT EXISTS idx_mul_used_at ON module_usage_log(used_at);

            -- ================================================
            -- 8. Discussion Sessions (Agent Discussion v2.0)
            -- ================================================
            CREATE TABLE IF NOT EXISTS discussion_sessions (
                id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id                 BIGINT NOT NULL,
                task_text               TEXT NOT NULL,
                status                  TEXT DEFAULT 'running',
                final_plan              TEXT DEFAULT NULL,
                workflow_instance_id    BIGINT DEFAULT NULL,
                agent_planner_id        BIGINT DEFAULT NULL,
                agent_reviewer_id       BIGINT DEFAULT NULL,
                agent_decider_id        BIGINT DEFAULT NULL,
                created_at              TEXT DEFAULT (NOW()),
                completed_at            TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ds_user ON discussion_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_ds_status ON discussion_sessions(status);

            -- ================================================
            -- 9. Discussion Messages (Agent Discussion v2.0)
            -- ================================================
            CREATE TABLE IF NOT EXISTS discussion_messages (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                session_id      BIGINT NOT NULL,
                round_num       INT NOT NULL DEFAULT 1,
                agent_name      TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                content_type    TEXT DEFAULT 'text',
                created_at      TEXT DEFAULT (NOW())
            );

            CREATE INDEX IF NOT EXISTS idx_dm_session ON discussion_messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_dm_round ON discussion_messages(session_id, round_num);

            -- ================================================
            -- 10. Prompt entries table (Dynamic Prompt System)
            -- ================================================
            CREATE TABLE IF NOT EXISTS agent_prompts (
                id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name          TEXT NOT NULL,
                slug          TEXT NOT NULL,
                version       INTEGER DEFAULT 1,
                content       TEXT NOT NULL,
                prompt_type   TEXT NOT NULL DEFAULT 'system'
                              CHECK(prompt_type IN ('system','scene','tool','rule','composite')),
                domain        TEXT DEFAULT '',
                tags          TEXT DEFAULT '[]',
                task_triggers TEXT DEFAULT '[]',
                parent_id     BIGINT DEFAULT NULL,
                priority      INTEGER DEFAULT 0,
                is_active     BOOLEAN DEFAULT TRUE,
                created_at    TEXT DEFAULT (NOW()),
                updated_at    TEXT DEFAULT (NOW()),
                UNIQUE(slug, version)
            );
            CREATE INDEX IF NOT EXISTS idx_ap_type ON agent_prompts(prompt_type);
            CREATE INDEX IF NOT EXISTS idx_ap_domain ON agent_prompts(domain);
            CREATE INDEX IF NOT EXISTS idx_ap_active ON agent_prompts(is_active);
            -- 复合索引：常用筛选组合 prompt_type + is_active
            CREATE INDEX IF NOT EXISTS idx_ap_type_active ON agent_prompts(prompt_type, is_active);
            -- 全局安全规则唯一性保证（_get_global_rules 只允许一条 rule+general）
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ap_rule_general
                ON agent_prompts(prompt_type, domain)
                WHERE prompt_type='rule' AND domain='general';

            -- ================================================
            -- 11. Agent-Prompt binding table (Dynamic Prompt System)
            -- ================================================
            CREATE TABLE IF NOT EXISTS agent_prompt_bindings (
                id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                agent_id      BIGINT NOT NULL REFERENCES agent_matrix(id) ON DELETE CASCADE,
                prompt_id     BIGINT NOT NULL REFERENCES agent_prompts(id) ON DELETE CASCADE,
                binding_type  TEXT NOT NULL DEFAULT 'default'
                              CHECK(binding_type IN ('default','scene','override','mode')),
                condition     TEXT DEFAULT '',
                priority      INTEGER DEFAULT 0,
                UNIQUE(agent_id, prompt_id, binding_type)
            );
            CREATE INDEX IF NOT EXISTS idx_apb_agent ON agent_prompt_bindings(agent_id);
            CREATE INDEX IF NOT EXISTS idx_apb_prompt ON agent_prompt_bindings(prompt_id);
            CREATE INDEX IF NOT EXISTS idx_apb_type ON agent_prompt_bindings(binding_type);
        """)
        conn.commit()

    # 会话标题字段 (v2 迁移) — 单独执行，不能放 executescript 内
    with get_db() as conn:
        cols = get_table_columns(conn, 'agent_conversations')
        if 'session_name' not in cols:
            conn.execute("ALTER TABLE agent_conversations ADD COLUMN session_name TEXT DEFAULT ''")
            conn.commit()

    # ── Migration: add provider_model_id to agent_matrix ──
    with get_db() as conn:
        cols = get_table_columns(conn, 'agent_matrix')
        if 'provider_model_id' not in cols:
            conn.execute("ALTER TABLE agent_matrix ADD COLUMN provider_model_id BIGINT DEFAULT NULL")
            conn.commit()
            print('[Migration] Added agent_matrix.provider_model_id')
        # Migrate old model_provider_id → provider_model_id
        rows = conn.execute(
            "SELECT id, model_provider_id FROM agent_matrix WHERE provider_model_id IS NULL AND model_provider_id IS NOT NULL"
        ).fetchall()
        for a in rows:
            conn.execute("UPDATE agent_matrix SET provider_model_id=%s WHERE id=%s",
                         (a['model_provider_id'], a['id']))
        if rows:
            conn.commit()
            print(f'[Migration] Migrated {len(rows)} agent_matrix rows model_provider_id→provider_model_id')

    # ── Migration: add dimension to agent_token_logs ──
    with get_db() as conn:
        cols = get_table_columns(conn, 'agent_token_logs')
        if 'dimension' not in cols:
            conn.execute("ALTER TABLE agent_token_logs ADD COLUMN dimension TEXT DEFAULT 'text'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tkl_dimension ON agent_token_logs(dimension)")
            conn.commit()
            print('[Migration] Added agent_token_logs.dimension')

    # ── Migration: add module to agent_token_logs ──
    with get_db() as conn:
        cols = get_table_columns(conn, 'agent_token_logs')
        if 'module' not in cols:
            conn.execute("ALTER TABLE agent_token_logs ADD COLUMN module TEXT DEFAULT 'legacy'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tkl_module ON agent_token_logs(module)")
            conn.commit()
            print('[Migration] Added agent_token_logs.module')

    # ── Migration: add index on agent_token_logs.user_id (for token_stats JOINs) ──
    with get_db() as conn:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tkl_user_id ON agent_token_logs(user_id)")
        conn.commit()
        print('[Migration] Added agent_token_logs.user_id index')

    # ── Migration: make legacy agent_matrix fields nullable ──
    with get_db() as conn:
        # provider/model_name no longer required — provider_model_id is the canonical reference
        conn.execute("ALTER TABLE agent_matrix ALTER COLUMN provider DROP NOT NULL")
        conn.execute("ALTER TABLE agent_matrix ALTER COLUMN model_name DROP NOT NULL")
        conn.commit()
        print('[Migration] agent_matrix.provider/model_name made nullable')

    # ── Migration: add slug & is_system to agent_matrix ──
    with get_db() as conn:
        cols = get_table_columns(conn, 'agent_matrix')
        if 'slug' not in cols:
            conn.execute("ALTER TABLE agent_matrix ADD COLUMN slug TEXT DEFAULT ''")
        if 'is_system' not in cols:
            conn.execute("ALTER TABLE agent_matrix ADD COLUMN is_system BIGINT DEFAULT 0")
        if 'slug' not in cols or 'is_system' not in cols:
            conn.commit()
            print('[Migration] Added agent_matrix.slug / is_system')


# ============================================================
# Seed 数据（从 YAML 角色定义加载）
# ============================================================

def load_system_roles():
    """从 YAML 文件加载系统角色定义列表。"""
    return _load_all_role_yamls()


def seed_default_agents():
    """YAML→DB 同步：从 YAML 角色定义文件同步到 agent_matrix 表。

    同步规则：
    1. YAML 中的角色，DB 中 slug 不存在 → INSERT（新系统角色）
    2. YAML 中的角色，DB 中 slug 已存在 → UPDATE（同步 name/description/managed_modules 等）
    3. DB 中 is_system=1 但 YAML 中已不存在 → DELETE（旧系统角色被删除/重命名）
    4. DB 中 is_system=0 的角色 → 不处理（用户自定义/插件角色，不受 YAML 影响）
    """
    roles = load_system_roles()
    if not roles:
        print(_('[Seed] Role YAML file not found, skipped seed data'))
        return

    yaml_slugs = set()
    with get_db() as conn:
        # ── Phase 1: UPSERT YAML-defined system roles ──
        for a in roles:
            slug = a.get('slug', '')
            if not slug:
                continue
            yaml_slugs.add(slug)

            exists = conn.execute(
                "SELECT id FROM agent_matrix WHERE slug=%s", (slug,)
            ).fetchone()

            if not exists:
                # slug 不存在 → 查 (name, role_type) 兜底（防止不同 slug 冲突 UNIQUE 约束）
                by_name = conn.execute(
                    "SELECT id, slug as old_slug FROM agent_matrix WHERE name=%s AND role_type=%s",
                    (a.get('name', ''), a.get('role_type', 'sub'))
                ).fetchone()

                if by_name:
                    # 同名同类型记录存在但 slug 不同 → UPDATE 迁移 slug + 同步元数据
                    old_slug = by_name['old_slug']
                    conn.execute("""
                        UPDATE agent_matrix SET
                            slug=%s, name=%s, description=%s, domain=%s,
                            managed_modules=%s, auto_approve=%s,
                            allowed_tools=%s, capabilities=%s, is_system=1, updated_at=NOW()
                        WHERE id=%s
                    """, (
                        slug, a.get('name', ''), a.get('description', ''),
                        a.get('domain', 'general'),
                        a.get('managed_modules', '[]'),
                        a.get('auto_approve', 0),
                        a.get('allowed_tools', '[]'),
                        a.get('capabilities', '[]'),
                        by_name['id']
                    ))
                    print(f'[Seed] Migrate slug: {old_slug} → {slug}')
                else:
                    # INSERT new system role
                    conn.execute("""
                        INSERT INTO agent_matrix
                        (name, slug, role_type, description, domain, managed_modules,
                         provider, model_name, system_prompt, auto_approve, is_active, is_system,
                         capabilities, allowed_tools)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        a.get('name', ''), slug, a.get('role_type', 'sub'),
                        a.get('description', ''), a.get('domain', 'general'),
                        a.get('managed_modules', '[]'),
                        a.get('provider', 'dashscope'), a.get('model_name', 'qwen-turbo'),
                        a.get('system_prompt', ''),
                        a.get('auto_approve', 0), a.get('is_active', 1), a.get('is_system', 1),
                        a.get('capabilities', '[]'), a.get('allowed_tools', '[]')
                    ))
                    print(f'[Seed] Insert system role: {slug}')
            else:
                # UPDATE existing system role — sync metadata only, preserve AI config
                # NEVER overwrite provider/model_name/api_key_ref on existing agents
                # 合并保留插件聚合的能力/模块（统一网关注册 §4），避免重启覆盖
                cur = conn.execute(
                    "SELECT capabilities, managed_modules FROM agent_matrix WHERE slug=%s",
                    (slug,)
                ).fetchone()
                yaml_caps = _json_list(a.get('capabilities', '[]'))
                yaml_mods = _json_list(a.get('managed_modules', '[]'))
                db_caps = _json_list(cur['capabilities'] if cur else [])
                db_mods = _json_list(cur['managed_modules'] if cur else [])
                merged_caps = _merge_unique(yaml_caps, [c for c in db_caps if c not in yaml_caps])
                merged_mods = _merge_unique(yaml_mods, [m for m in db_mods if m not in yaml_mods])
                conn.execute("""
                    UPDATE agent_matrix SET
                        name=%s, description=%s, domain=%s,
                        managed_modules=%s, auto_approve=%s,
                        allowed_tools=%s, capabilities=%s, is_system=1, updated_at=NOW()
                    WHERE slug=%s
                """, (
                    a.get('name', ''), a.get('description', ''),
                    a.get('domain', 'general'),
                    json.dumps(merged_mods, ensure_ascii=False),
                    a.get('auto_approve', 0),
                    a.get('allowed_tools', '[]'),
                    json.dumps(merged_caps, ensure_ascii=False),
                    slug
                ))

        # ── Phase 2: DELETE old system roles no longer in YAML ──
        # 护栏（架构评审 §3.2）：绝不静默删核心角色。
        #   1) 角色集异常收缩（<5）→ 跳过删除（fail-closed）
        #   2) 删除数量超上限 MAX_SYSTEM_ROLE_DELETE（默认 3）→ 跳过删除（fail-closed）
        #   3) SEED_DRY_RUN=1 → 预演模式，只打印不删除
        deleted = 0
        if yaml_slugs:
            placeholders = ','.join(['%s'] * len(yaml_slugs))
            stale = 0
            if len(yaml_slugs) < 5:
                print(f'[Seed] FAIL-CLOSED: role set shrank to {len(yaml_slugs)}, skip delete')
            else:
                stale = conn.execute(
                    "SELECT COUNT(*) AS c FROM agent_matrix "
                    "WHERE is_system=1 AND slug NOT IN ({}) AND slug != ''"
                    .format(placeholders), tuple(yaml_slugs)
                ).fetchone()['c'] or 0
                max_delete = int(os.getenv('MAX_SYSTEM_ROLE_DELETE', '3'))
                if stale > max_delete:
                    print(f'[Seed] FAIL-CLOSED: {stale} stale roles exceed limit {max_delete}, skip delete')
                elif os.getenv('SEED_DRY_RUN', '0') == '1':
                    print(f'[Seed] DRY-RUN: would delete {stale} stale system roles, skipped')
                else:
                    deleted = conn.execute(
                        "DELETE FROM agent_matrix "
                        "WHERE is_system=1 AND slug NOT IN ({}) AND slug != ''"
                        .format(placeholders), tuple(yaml_slugs)
                    ).rowcount
        if deleted:
            print(f'[Seed] Cleaned up {deleted} old system roles')

        conn.commit()


# ============================================================
# Agent CRUD
# ============================================================

def register_plugin_roles(plugin_id, declare_roles_list):
    """注册插件声明的角色到 agent_matrix（幂等）。
    declare_roles_list: [ {name, slug, description, domain, managed_modules, ...} ]
    返回注册数量。
    """
    count = 0
    with get_db() as conn:
        for r in declare_roles_list:
            slug = r.get('slug') or r.get('name', '').lower().replace(' ', '-')
            exists = conn.execute(
                "SELECT id FROM agent_matrix WHERE slug=%s", (slug,)
            ).fetchone()
            if not exists:
                conn.execute("""
                    INSERT INTO agent_matrix
                    (name, slug, role_type, description, domain, managed_modules,
                     provider, model_name, system_prompt, is_active, is_system)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
                """, (
                    r.get('name', ''), slug, r.get('role_type', 'sub'),
                    r.get('description', ''), r.get('domain', 'general'),
                    json.dumps(r.get('managed_modules', [])),
                    r.get('provider', 'dashscope'),
                    r.get('model_name', 'qwen-turbo'),
                    r.get('system_prompt', ''),
                    r.get('is_active', 1),
                ))
                count += 1
                print(f'[PluginRoles] Register plugin role: {slug} (from {plugin_id})')
        if count:
            conn.commit()
    return count


def unregister_plugin_roles(plugin_id, declare_roles_list):
    """卸载插件对应的角色。"""
    slugs = [
        r.get('slug') or r.get('name', '').lower().replace(' ', '-')
        for r in declare_roles_list
    ]
    with get_db() as conn:
        for slug in slugs:
            conn.execute("DELETE FROM agent_matrix WHERE slug=%s AND is_system=0", (slug,))
            print(f'[PluginRoles] Uninstall plugin role: {slug} (from {plugin_id})')
        conn.commit()


# ============================================================
# 统一 Agent 声明注册（§4 — agents + declare_roles 合并）
# ============================================================

def _merged_agent_slugs(metadata: dict) -> list:
    """收集插件声明的全部 Agent slug（agents.identifier / declare_roles.slug）。"""
    slugs = set()
    for decl in metadata.get('declare_roles') or []:
        slugs.add(decl.get('slug') or decl.get('name', '').lower().replace(' ', '-'))
    for decl in metadata.get('agents') or []:
        slugs.add(decl.get('identifier') or decl.get('name', '').lower().replace(' ', '-'))
    return sorted(slugs)


def _merge_agent_declarations(plugin_dir: str, metadata: dict) -> list:
    """合并 agents + declare_roles 两种声明，按 slug 归一，agents 字段优先。

    agents（推荐）：identifier/prompt_file/model_policy/capabilities/enabled_by_default
    declare_roles（兼容别名）：slug/role_type/description/managed_modules/...
    declare_roles 仅补齐 agents 未提供的字段（如 description）。
    """
    merged = {}
    for decl in metadata.get('declare_roles') or []:
        slug = decl.get('slug') or decl.get('name', '').lower().replace(' ', '-')
        role_type = decl.get('role_type', 'sub')
        if role_type not in ('master', 'sub'):
            print(f'[PluginRoles] WARNING: {slug} invalid role_type {role_type!r}, coerced to "sub"')
            role_type = 'sub'
        merged[slug] = {
            'slug': slug,
            'name': decl.get('name', slug),
            'role_type': role_type,
            'description': decl.get('description', ''),
            'domain': decl.get('domain', 'general'),
            'system_prompt': decl.get('system_prompt', ''),
            'capabilities': json.dumps(decl.get('capabilities', [])),
            'is_active': _to_int(decl.get('is_active', 1)),
        }
    for decl in metadata.get('agents') or []:
        slug = decl.get('identifier') or decl.get('name', '').lower().replace(' ', '-')
        prev = merged.get(slug, {})
        name = decl.get('name', slug)
        domain = decl.get('domain', prev.get('domain', 'general'))
        role_type = decl.get('role_type', prev.get('role_type', 'sub'))
        if role_type not in ('master', 'sub'):
            print(f'[PluginRoles] WARNING: {slug} invalid role_type {role_type!r}, coerced to "sub"')
            role_type = 'sub'
        system_prompt = ''
        prompt_file = decl.get('prompt_file', '')
        if prompt_file and plugin_dir:
            prompt_path = os.path.join(plugin_dir, prompt_file)
            if not os.path.isfile(prompt_path):
                print(f'[PluginRoles] WARNING: {slug} prompt_file not found: {prompt_file}')
            try:
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    system_prompt = f.read().strip()
            except OSError:
                pass
        merged[slug] = {
            'slug': slug,
            'name': name,
            'role_type': role_type,
            'description': prev.get('description') or f'{name} — {domain}',
            'domain': domain,
            'system_prompt': system_prompt,
            'capabilities': json.dumps(decl.get('capabilities', [])),
            'is_active': 1 if decl.get('enabled_by_default', True) else 0,
        }
    return list(merged.values())


def register_plugin_agents(plugin_id: str, plugin_dir: str, metadata: dict) -> int:
    """统一注册插件声明的 Agent 到 agent_matrix（幂等，§4）。

    合并 agents + declare_roles 声明；已存在（同 slug 且非系统角色）则更新字段，
    否则插入。所有权以 slug + is_system=0 追踪（不依赖 source_plugin 列）。
    """
    roles = _merge_agent_declarations(plugin_dir, metadata or {})
    count = 0
    with get_db() as conn:
        for r in roles:
            slug = r['slug']
            exists = conn.execute(
                "SELECT id FROM agent_matrix WHERE slug=%s AND is_system=0",
                (slug,)
            ).fetchone()
            if exists:
                conn.execute("""
                    UPDATE agent_matrix SET
                        name=%s, role_type=%s, description=%s, domain=%s,
                        system_prompt=%s, capabilities=%s, is_active=%s,
                        updated_at=NOW()
                    WHERE slug=%s AND is_system=0
                """, (
                    r['name'], r['role_type'], r['description'], r['domain'],
                    r['system_prompt'], r['capabilities'], r['is_active'], slug
                ))
            else:
                conn.execute("""
                    INSERT INTO agent_matrix
                    (name, slug, role_type, description, domain,
                     system_prompt, capabilities, is_active, is_system)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0)
                """, (
                    r['name'], slug, r['role_type'], r['description'],
                    r['domain'], r['system_prompt'], r['capabilities'],
                    r['is_active']
                ))
            count += 1
            print(f'[PluginRoles] Register plugin agent: {slug} (from {plugin_id})')
        if count:
            conn.commit()
    return count


def unregister_plugin_agents(plugin_id: str, metadata: dict) -> int:
    """注销插件声明的全部 Agent（按 slug 列表，兼容无 source_plugin 列的旧数据）。"""
    slugs = _merged_agent_slugs(metadata or {})
    if not slugs:
        return 0
    with get_db() as conn:
        placeholders = ','.join(['%s'] * len(slugs))
        conn.execute(
            f"DELETE FROM agent_matrix WHERE slug IN ({placeholders}) AND is_system=0",
            tuple(slugs)
        )
        conn.commit()
        print(f'[PluginRoles] Unregister {len(slugs)} plugin agents (from {plugin_id})')
    return len(slugs)


# ============================================================
# 统一网关注册（插件标准 §2.2/§4）— 插件能力聚合到核心角色
# 强制要求：plugin.json 必须声明 agent_role（9 核心角色之一）+ capabilities。
# 聚合目标为核心角色行（is_system=1），不新建独立 Agent 行。
# ============================================================

# 系统预设核心角色 = roles/*.yaml 目录（is_system=1），单一事实源。
# 不再维护手写 slug 元组；get_core_role_slugs() 由 YAML 动态推导，
# edition 过滤（_load_all_role_yamls 内）自动生效。


def get_core_role_slugs() -> list:
    """返回系统预设核心角色 slug 列表（由 roles/*.yaml 动态推导）。"""
    return [r['slug'] for r in _load_all_role_yamls() if r.get('slug')]


def _json_list(value) -> list:
    """兼容 list / JSON 字符串 / None，安全转为 list。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            v = json.loads(value or '[]')
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _merge_unique(base: list, extra: list) -> list:
    """去重并集，保持顺序。"""
    seen = set()
    result = []
    for item in list(base) + list(extra):
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _declared_agent_role(metadata: dict) -> str:
    """提取并校验插件声明的 agent_role，非法返回 ''。"""
    role = (metadata or {}).get('agent_role', '')
    if role not in get_core_role_slugs():
        return ''
    return role


def attach_plugin_capabilities(plugin_id: str, metadata: dict) -> int:
    """将插件能力聚合到所选核心角色（幂等，插件标准 §4）。

    校验失败（无合法 agent_role / 目标核心角色不存在）返回 -1，不抛异常。
    返回受影响的核心角色数（成功为 1）。
    """
    role = _declared_agent_role(metadata or {})
    if not role:
        print(f'[PluginRoles] WARNING: {plugin_id} 缺少合法 agent_role '
              f'（须为 {get_core_role_slugs()} 之一），跳过网关注册')
        return -1
    caps = _json_list((metadata or {}).get('capabilities', []))
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, capabilities, managed_modules FROM agent_matrix WHERE slug=%s AND is_system=1",
            (role,)
        ).fetchone()
        if not row:
            print(f'[PluginRoles] WARNING: {plugin_id} 目标核心角色 {role} 不存在（is_system=1），跳过')
            return -1
        new_caps = _merge_unique(_json_list(row['capabilities']), caps)
        new_mods = _merge_unique(_json_list(row['managed_modules']), [plugin_id])
        conn.execute(
            "UPDATE agent_matrix SET capabilities=%s, managed_modules=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(new_caps, ensure_ascii=False),
             json.dumps(new_mods, ensure_ascii=False),
             row['id'])
        )
        conn.commit()
    print(f'[PluginRoles] Attach plugin capabilities: {plugin_id} → {role} ({len(caps)} caps)')
    return 1


def detach_plugin_capabilities(plugin_id: str, metadata: dict) -> int:
    """从核心角色移除插件聚合的能力与模块标记（幂等，插件标准 §4）。"""
    role = _declared_agent_role(metadata or {})
    if not role:
        return 0
    caps = _json_list((metadata or {}).get('capabilities', []))
    cap_set = set(caps)
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, capabilities, managed_modules FROM agent_matrix WHERE slug=%s AND is_system=1",
            (role,)
        ).fetchone()
        if not row:
            return 0
        new_caps = [c for c in _json_list(row['capabilities']) if c not in cap_set]
        new_mods = [m for m in _json_list(row['managed_modules']) if m != plugin_id]
        conn.execute(
            "UPDATE agent_matrix SET capabilities=%s, managed_modules=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(new_caps, ensure_ascii=False),
             json.dumps(new_mods, ensure_ascii=False),
             row['id'])
        )
        conn.commit()
    print(f'[PluginRoles] Detach plugin capabilities: {plugin_id} ← {role}')
    return 1


def list_agents(role_type=None, domain=None, active_only=False):
    """列出 Agent，支持筛选"""
    with get_db() as conn:
        sql = "SELECT * FROM agent_matrix WHERE 1=1"
        params = []
        if role_type:
            sql += " AND role_type=%s"
            params.append(role_type)
        if domain:
            sql += " AND domain=%s"
            params.append(domain)
        if active_only:
            sql += " AND is_active=1"
        sql += " ORDER BY priority DESC, created_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_agent(agent_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agent_matrix WHERE id=%s", (agent_id,)).fetchone()
        return dict(row) if row else None


def get_agent_by_name(name, role_type='sub'):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM agent_matrix WHERE name=%s AND role_type=%s",
            (name, role_type)
        ).fetchone()
        return dict(row) if row else None


def get_agent_by_slug(slug):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM agent_matrix WHERE slug=%s", (slug,)
        ).fetchone()
        return dict(row) if row else None


def create_agent(data):
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO agent_matrix
            (name, role_type, description, domain, managed_modules,
             provider, model_name, api_key_ref, base_url, provider_model_id,
             system_prompt, role_prompt, task_template,
             capabilities, allowed_tools,
             max_concurrency, priority, auto_approve, is_active)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            data.get('name', ''),
            data.get('role_type', 'sub'),
            data.get('description', ''),
            data.get('domain', 'general'),
            json.dumps(data.get('managed_modules', [])),
            data.get('provider', 'dashscope'),
            data.get('model_name', 'qwen-turbo'),
            data.get('api_key_ref', 'dashscope_text_key'),
            data.get('base_url', ''),
            data.get('provider_model_id'),
            data.get('system_prompt', ''),
            data.get('role_prompt', ''),
            data.get('task_template', ''),
            json.dumps(data.get('capabilities', [])),
            json.dumps(data.get('allowed_tools', [])),
            data.get('max_concurrency', 1),
            data.get('priority', 5),
            data.get('auto_approve', 0),
            data.get('is_active', 1)
        )).fetchone()
        conn.commit()
        return row[0]


def register_capability_to_role(domain, name, capabilities, description='', system_prompt=''):
    """将模块/插件的能力注册到已存在的 YAML 角色中（按 domain 匹配）。

    不创建新 Agent，只扩展目标角色的 capabilities 字段。
    匹配规则：agent_matrix 中 is_system=1 且 role_type='sub' 且 domain 匹配的角色。
    无匹配角色时仅记录警告（未来可能由独立 YAML 编排接管）。
    """
    with get_db() as conn:
        role = conn.execute(
            "SELECT id, capabilities FROM agent_matrix WHERE domain=%s AND is_system=1 AND role_type='sub' LIMIT 1",
            (domain,)
        ).fetchone()
        if not role:
            print(f'[Capability] ⚠️ No system role found for domain={domain}, skipping {name}')
            return False

        existing = json.loads(role['capabilities'] or '[]')
        if isinstance(capabilities, str):
            capabilities = json.loads(capabilities)
        new_caps = [c for c in capabilities if c not in existing]
        if new_caps:
            conn.execute(
                "UPDATE agent_matrix SET capabilities=%s, updated_at=NOW() WHERE id=%s",
                (json.dumps(existing + new_caps), role['id'])
            )
            conn.commit()
            print(f'[Capability] ✅ Registered {len(new_caps)} capabilities to role domain={domain}')
        else:
            print(f'[Capability] ℹ️ No new capabilities for {name} on domain={domain}')
        return True


def update_agent(agent_id, data):
    with get_db() as conn:
        # 当设置了 provider_model_id，自动同步 provider 和 model_name
        if data.get('provider_model_id'):
            row = conn.execute(
                'SELECT p.slug AS provider, pm.model_name '
                'FROM provider_models pm '
                'JOIN providers p ON p.id = pm.provider_id '
                'WHERE pm.id = %s',
                (data['provider_model_id'],)
            ).fetchone()
            if row:
                data['provider'] = row['provider']
                data['model_name'] = row['model_name']

        fields = []
        values = []
        for key in ['name', 'role_type', 'description', 'domain',
                     'provider', 'model_name', 'api_key_ref', 'base_url', 'provider_model_id',
                     'system_prompt', 'role_prompt', 'task_template',
                     'max_concurrency', 'priority', 'auto_approve', 'is_active']:
            if key in data:
                fields.append(f"{key}=%s")
                values.append(data[key])
        for key in ['managed_modules', 'capabilities', 'allowed_tools']:
            if key in data:
                fields.append(f"{key}=%s")
                values.append(json.dumps(data[key]) if isinstance(data[key], list) else data[key])
        if not fields:
            return False
        fields.append("updated_at=NOW()")
        values.append(agent_id)
        conn.execute(
            f"UPDATE agent_matrix SET {','.join(fields)} WHERE id=%s",
            values
        )
        conn.commit()
        return True


def delete_agent(agent_id):
    """删除 Agent。系统角色（is_system=1）不可删除。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT is_system FROM agent_matrix WHERE id=%s", (agent_id,)
        ).fetchone()
        if not row:
            return False
        if row['is_system']:
            raise PermissionError(_("System roles cannot be deleted, only disabled"))
        conn.execute("DELETE FROM agent_matrix WHERE id=%s", (agent_id,))
        conn.commit()
        return True


def toggle_agent(agent_id):
    """切换启用/禁用状态"""
    with get_db() as conn:
        row = conn.execute("SELECT is_active FROM agent_matrix WHERE id=%s", (agent_id,)).fetchone()
        if not row:
            return None
        new = 0 if row['is_active'] else 1
        conn.execute("UPDATE agent_matrix SET is_active=%s, updated_at=NOW() WHERE id=%s", (new, agent_id))
        conn.commit()
        return new


# ============================================================
# Prompt CRUD (Dynamic Prompt System)
# ============================================================

def list_prompts(prompt_type=None, domain=None, keyword=None, active_only=False):
    """列出提示词条目，支持按类型/域/关键词筛选"""
    with get_db() as conn:
        sql = "SELECT * FROM agent_prompts WHERE 1=1"
        params = []
        if prompt_type:
            sql += " AND prompt_type=%s"
            params.append(prompt_type)
        if domain:
            sql += " AND domain=%s"
            params.append(domain)
        if keyword:
            sql += " AND (name LIKE %s OR slug LIKE %s OR content LIKE %s)"
            kw = f'%{keyword}%'
            params.extend([kw, kw, kw])
        if active_only:
            sql += " AND is_active=TRUE"
        sql += " ORDER BY prompt_type, priority DESC, version DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_prompt(prompt_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agent_prompts WHERE id=%s", (prompt_id,)).fetchone()
        return dict(row) if row else None


def get_prompt_versions(slug):
    """返回同一 slug 的全部版本（升序）"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_prompts WHERE slug=%s ORDER BY version ASC",
            (slug,)
        ).fetchall()
        return [dict(r) for r in rows]


def create_prompt(data):
    """创建新提示词。同 slug 已存在时 version 自动 +1。

    使用 UNIQUE(slug, version) + ON CONFLICT DO NOTHING 原子化，
    避免并发创建时 MAX(version)+1 竞态（R2-A3）。
    """
    with get_db() as conn:
        slug = (data.get('slug') or '').strip()
        if not slug:
            # 未提供 slug 时由 name 生成
            slug = (data.get('name') or 'prompt').strip().lower().replace(' ', '-')
        for _attempt in range(3):
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM agent_prompts WHERE slug=%s",
                (slug,)
            ).fetchone()
            version = int(row['v']) + 1
            new_id = conn.execute("""
                INSERT INTO agent_prompts
                (name, slug, version, content, prompt_type, domain, tags,
                 task_triggers, parent_id, priority, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (slug, version) DO NOTHING
                RETURNING id
            """, (
                data.get('name', slug),
                slug,
                version,
                data.get('content', ''),
                data.get('prompt_type', 'system'),
                data.get('domain', ''),
                json.dumps(data.get('tags', [])) if isinstance(data.get('tags'), list) else data.get('tags', '[]'),
                json.dumps(data.get('task_triggers', [])) if isinstance(data.get('task_triggers'), list) else data.get('task_triggers', '[]'),
                data.get('parent_id'),
                data.get('priority', 0),
                bool(data.get('is_active', True)),
            )).fetchone()
            if new_id:
                conn.commit()
                return new_id[0]
            # 冲突（并发已插入相同 slug+version）→ 重试计算新版本
        raise RuntimeError(f"Failed to create prompt: concurrent conflict for slug={slug}")


def update_prompt(prompt_id, data):
    """更新提示词。content 变更时 version 自动 +1。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT slug, version, content FROM agent_prompts WHERE id=%s", (prompt_id,)
        ).fetchone()
        if not row:
            return None

        content = data.get('content')
        if content is not None and content != row['content']:
            # 内容有变 → 新版本
            version = int(row['version']) + 1
            new_id = conn.execute("""
                INSERT INTO agent_prompts
                (name, slug, version, content, prompt_type, domain, tags,
                 task_triggers, parent_id, priority, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                data.get('name', ''), row['slug'], version, content,
                data.get('prompt_type', 'system'), data.get('domain', ''),
                data.get('tags', '[]'), data.get('task_triggers', '[]'),
                data.get('parent_id'), data.get('priority', 0),
                bool(data.get('is_active', True)),
            )).fetchone()
            conn.commit()
            return new_id[0]

        # 仅元数据更新
        fields = []
        values = []
        for key in ['name', 'prompt_type', 'domain', 'tags', 'task_triggers',
                    'parent_id', 'priority', 'is_active']:
            if key in data:
                val = data[key]
                if key in ('tags', 'task_triggers') and isinstance(val, list):
                    val = json.dumps(val)
                elif key == 'is_active':
                    val = bool(val)
                fields.append(f"{key}=%s")
                values.append(val)
        if not fields:
            return prompt_id
        fields.append("updated_at=NOW()")
        values.append(prompt_id)
        conn.execute(
            f"UPDATE agent_prompts SET {','.join(fields)} WHERE id=%s",
            values
        )
        conn.commit()
        return prompt_id


def delete_prompt(prompt_id):
    """软删除：is_active=false"""
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_prompts SET is_active=FALSE, updated_at=NOW() WHERE id=%s",
            (prompt_id,)
        )
        conn.commit()


def list_bindings(agent_id=None):
    """列出 Agent-Prompt 绑定关系"""
    with get_db() as conn:
        sql = """
            SELECT b.*, p.name AS prompt_name, p.slug AS prompt_slug, p.version,
                   p.prompt_type, p.domain, a.name AS agent_name
            FROM agent_prompt_bindings b
            LEFT JOIN agent_prompts p ON p.id = b.prompt_id
            LEFT JOIN agent_matrix a ON a.id = b.agent_id
            WHERE 1=1
        """
        params = []
        if agent_id:
            sql += " AND b.agent_id=%s"
            params.append(agent_id)
        sql += " ORDER BY b.agent_id, b.binding_type, b.priority DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def create_binding(agent_id, prompt_id, binding_type='default', condition='', priority=0):
    """为 Agent 绑定 Prompt（幂等：UNIQUE 冲突时原子更新，避免 DELETE+INSERT 非原子）"""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO agent_prompt_bindings
            (agent_id, prompt_id, binding_type, condition, priority)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (agent_id, prompt_id, binding_type)
            DO UPDATE SET condition=excluded.condition, priority=excluded.priority
        """, (agent_id, prompt_id, binding_type, condition, priority))
        conn.commit()


def delete_binding(binding_id, agent_id=None):
    """解绑。可指定 agent_id 校验归属（R2-A2）。返回受影响行数。"""
    with get_db() as conn:
        if agent_id is not None:
            cur = conn.execute(
                "DELETE FROM agent_prompt_bindings WHERE id=%s AND agent_id=%s",
                (binding_id, agent_id)
            )
        else:
            cur = conn.execute("DELETE FROM agent_prompt_bindings WHERE id=%s", (binding_id,))
        conn.commit()
        return cur.rowcount if cur and hasattr(cur, 'rowcount') else 0


# ============================================================
# Task CRUD
# ============================================================

def create_task(data):
    task_id = _next_task_id()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO agent_tasks
            (task_id, parent_task_id, master_task_id,
             source_agent_id, target_agent_id,
             task_type, title, description,
             input_data, expected_output,
             target_module, target_api,
             priority, max_retries, timeout_seconds, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            task_id,
            data.get('parent_task_id'),
            data.get('master_task_id', task_id) if not data.get('parent_task_id') else data.get('master_task_id'),
            data['source_agent_id'],
            data['target_agent_id'],
            data.get('task_type', 'execute'),
            data.get('title', ''),
            data.get('description', ''),
            json.dumps(data.get('input_data', {})),
            json.dumps(data.get('expected_output', {})),
            data.get('target_module', ''),
            data.get('target_api', ''),
            data.get('priority', 5),
            data.get('max_retries', 3),
            data.get('timeout_seconds', 600),
            'pending'
        ))
        conn.commit()
        return task_id


def get_task(task_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agent_tasks WHERE task_id=%s", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(status=None, module=None, agent_id=None, limit=50):
    with get_db() as conn:
        sql = "SELECT * FROM agent_tasks WHERE 1=1"
        params = []
        if status:
            sql += " AND status=%s"
            params.append(status)
        if module:
            sql += " AND target_module=%s"
            params.append(module)
        if agent_id:
            sql += " AND (source_agent_id=%s OR target_agent_id=%s)"
            params.extend([agent_id, agent_id])
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def update_task_status(task_id, status, result_data=None, confidence=None,
                       error_message=None, self_review=None):
    with get_db() as conn:
        fields = ["status=%s", "updated_at=NOW()"]
        values = [status]
        if result_data is not None:
            fields.append("result_data=%s")
            values.append(json.dumps(result_data) if not isinstance(result_data, str) else result_data)
        if confidence is not None:
            fields.append("confidence=%s")
            values.append(confidence)
        if error_message is not None:
            fields.append("error_message=%s")
            values.append(error_message)
        if self_review is not None:
            fields.append("self_review=%s")
            values.append(self_review)
        if status == 'running':
            fields.append("started_at=NOW()")
        if status in ('completed', 'failed', 'cancelled'):
            fields.append("completed_at=NOW()")
        values.append(task_id)
        conn.execute(
            f"UPDATE agent_tasks SET {','.join(fields)} WHERE task_id=%s",
            values
        )
        conn.commit()


def get_sub_tasks(master_task_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE master_task_id=%s ORDER BY created_at ASC",
            (master_task_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def cancel_task(task_id):
    update_task_status(task_id, 'cancelled')


# ============================================================
# 日志 CRUD
# ============================================================

def add_log(task_id, agent_id, level='info', log_type='execution', message='', metadata=None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO task_logs (task_id, agent_id, log_level, log_type, message, metadata)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (task_id, agent_id, level, log_type, message,
              json.dumps(metadata or {})))
        conn.commit()


def get_task_logs(task_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM task_logs WHERE task_id=%s ORDER BY created_at ASC",
            (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# 对话 CRUD
# ============================================================

def create_session():
    return _next_session_id()


def add_message(session_id, role, content, agent_id=None, agent_name='', master_task_id='', metadata=None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO agent_conversations
            (session_id, role, agent_id, agent_name, content, master_task_id, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (session_id, role, agent_id, agent_name, content,
              master_task_id, json.dumps(metadata or {})))
        # 第一条用户消息设为会话标题
        if role == 'user':
            existing = conn.execute(
                "SELECT COUNT(*) as c FROM agent_conversations WHERE session_id=%s AND role='user'",
                (session_id,)
            ).fetchone()['c']
            if existing == 1:
                title = content.strip()[:40]
                conn.execute(
                    "UPDATE agent_conversations SET session_name=%s WHERE session_id=%s",
                    (title, session_id)
                )
        conn.commit()


def get_conversation(session_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_conversations WHERE session_id=%s ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_sessions(limit=20):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT session_id, MAX(created_at) as last_msg,
                   (SELECT content FROM agent_conversations WHERE session_id=ac.session_id
                    AND role='user' ORDER BY created_at ASC LIMIT 1) as first_query,
                   MAX(session_name) as session_name
            FROM agent_conversations ac
            GROUP BY session_id
            ORDER BY last_msg DESC
            LIMIT %s
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def search_conversations(keyword, limit=50):
    """全文检索对话内容（关键词匹配 content 字段）"""
    with get_db() as conn:
        like = f'%{keyword}%'
        rows = conn.execute("""
            SELECT ac.*,
                   (SELECT session_name FROM agent_conversations
                    WHERE session_id=ac.session_id AND session_name!=''
                    LIMIT 1) as session_name
            FROM agent_conversations ac
            WHERE content LIKE %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (like, limit)).fetchall()
        return [dict(r) for r in rows]


def batch_delete_sessions(session_ids):
    """批量删除指定会话的所有消息"""
    if not session_ids:
        return 0
    with get_db() as conn:
        placeholders = ','.join(['%s'] * len(session_ids))
        count = conn.execute(
            f"DELETE FROM agent_conversations WHERE session_id IN ({placeholders})",
            session_ids
        ).rowcount
        conn.commit()
        return count


# ============================================================
# Agent 统计更新
# ============================================================

def update_agent_stats(agent_id, success=True):
    """更新 Agent 的任务统计"""
    with get_db() as conn:
        field = 'tasks_success' if success else 'tasks_failed'
        conn.execute(f"""
            UPDATE agent_matrix
            SET tasks_total = tasks_total + 1,
                {field} = {field} + 1,
                last_run_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
        """, (agent_id,))
        conn.commit()


# ============================================================
# 统计
# ============================================================

def get_matrix_stats():
    with get_db() as conn:
        total_agents = conn.execute("SELECT COUNT(*) as c FROM agent_matrix").fetchone()['c']
        active_agents = conn.execute("SELECT COUNT(*) as c FROM agent_matrix WHERE is_active=1").fetchone()['c']
        master_count = conn.execute("SELECT COUNT(*) as c FROM agent_matrix WHERE role_type='master' AND is_active=1").fetchone()['c']
        sub_count = conn.execute("SELECT COUNT(*) as c FROM agent_matrix WHERE role_type='sub' AND is_active=1").fetchone()['c']

        total_tasks = conn.execute("SELECT COUNT(*) as c FROM agent_tasks").fetchone()['c']
        today = datetime.now().strftime('%Y-%m-%d')
        today_tasks = conn.execute(
            "SELECT COUNT(*) as c FROM agent_tasks WHERE created_at >= %s", (today,)
        ).fetchone()['c']
        completed = conn.execute(
            "SELECT COUNT(*) as c FROM agent_tasks WHERE status='completed'"
        ).fetchone()['c']
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM agent_tasks WHERE status='failed'"
        ).fetchone()['c']
        running = conn.execute(
            "SELECT COUNT(*) as c FROM agent_tasks WHERE status='running'"
        ).fetchone()['c']
        pending = conn.execute(
            "SELECT COUNT(*) as c FROM agent_tasks WHERE status='pending'"
        ).fetchone()['c']

        success_rate = round(completed / total_tasks * 100, 1) if total_tasks > 0 else 100.0

        return {
            'agents': {
                'total': total_agents,
                'active': active_agents,
                'master': master_count,
                'sub': sub_count
            },
            'tasks': {
                'total': total_tasks,
                'today': today_tasks,
                'completed': completed,
                'failed': failed,
                'running': running,
                'pending': pending,
                'success_rate': success_rate
            }
        }


def get_recent_tasks(limit=10):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT t.*, s.name as source_name, tg.name as target_name
            FROM agent_tasks t
            LEFT JOIN agent_matrix s ON t.source_agent_id = s.id
            LEFT JOIN agent_matrix tg ON t.target_agent_id = tg.id
            ORDER BY t.created_at DESC
            LIMIT %s
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
