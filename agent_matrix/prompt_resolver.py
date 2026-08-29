#!/usr/bin/env python3
"""
Agent Matrix — Dynamic Prompt Resolution Engine (PromptResolver)
================================================================
将静态 .md 提示词升级为数据库驱动、标签匹配、链式组装的动态调度引擎。

四层组装：
  Layer 1: 角色基础 (default binding / prompt_type='system')
  Layer 2: 全局安全规则 (prompt_type='rule' AND domain='general')
  Layer 3: 场景模版 (prompt_type='scene'，task_triggers 精确匹配)
  Layer 4: 模式增强 (prompt_type='tool'/'scene'，按 mode 标签匹配)

降级策略：
  - system_config.prompt_resolver_enabled = false → 回退 _legacy_load()
  - 任何异常 → 记录日志并回退 _legacy_load()
  - 无匹配条目 → 回退 agent_matrix.system_prompt 原加载逻辑
"""
from i18n import _
import json, os, logging

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENABLE_CONFIG_KEY = 'prompt_resolver_enabled'

# 组装分隔符
PART_SEPARATOR = '\n\n---\n\n'


class PromptResolver:
    """动态提示词解析引擎：基于任务上下文选择并组装 Prompt。"""

    def __init__(self, db_models):
        """
        db_models: agent_matrix.models 模块引用（提供 get_db()）
        """
        self.models = db_models

    # ============================================================
    # 对外主入口
    # ============================================================

    def resolve(self, agent_config, task_context):
        """解析 Agent 的完整 System Prompt（入口：组装后统一应用 before_prompt_resolve 过滤器）。"""
        prompt = self._resolve_raw(agent_config, task_context)
        return self._apply_prompt_filters(prompt, agent_config, task_context or {})

    def _resolve_raw(self, agent_config, task_context):
        """原 resolve 主体：四层组装 + 降级。"""
        if not agent_config:
            return ''
        # 全局开关
        if not self._is_enabled():
            return self._legacy_load(agent_config)
        try:
            return self._do_resolve(agent_config, task_context or {})
        except Exception as e:
            logger.error(f'PromptResolver failed, falling back to legacy: {e}')
            return self._legacy_load(agent_config)

    def _apply_prompt_filters(self, prompt, agent_config, task_context):
        """应用内核 before_prompt_resolve 过滤器链（memory_engine 等在此注入记忆）。

        ctx 传入 user_id / agent_id / user_query，供过滤器做隐私门控与检索。
        """
        try:
            from plugin_manager.hooks import get_hook_registry
            ctx = {
                'user_id': task_context.get('user_id'),
                'agent_id': agent_config.get('id') if agent_config else None,
                'user_query': task_context.get('user_query', ''),
            }
            return get_hook_registry().apply_filters('before_prompt_resolve', prompt, ctx=ctx)
        except Exception as e:
            logger.warning(f'before_prompt_resolve filter failed: {e}')
            return prompt

    # ============================================================
    # 四层组装
    # ============================================================

    def _do_resolve(self, agent_config, task_context):
        agent_id = agent_config.get('id')
        parts = []
        # 单连接复用，避免四层查询各自建立连接（N+1 优化）
        with self.models.get_db() as conn:
            # Layer 1: 角色基础 Prompt（default 绑定 → system 类型）
            base = self._get_binding_prompt(agent_id, 'default', conn=conn)
            if base:
                parts.append(base)

            # Layer 2: 全局安全规则（rule + domain=general）
            rules = self._get_global_rules(conn=conn)
            if rules:
                parts.append(rules)

            # Layer 3: 场景模版（task_triggers 精确匹配）
            scene = self._match_scene(task_context, conn=conn)
            if scene:
                parts.append(scene)

            # Layer 4: 模式增强（按 mode 标签 / mode 绑定）
            mode = task_context.get('mode')
            if mode:
                mode_prompt = self._get_binding_prompt(agent_id, 'mode', condition=mode, conn=conn)
                if not mode_prompt:
                    mode_prompt = self._match_by_tag(mode, prompt_types=('tool', 'scene'), conn=conn)
                if mode_prompt:
                    parts.append(mode_prompt)

        return self._assemble(parts)

    def _assemble(self, parts):
        """拼接非空部分"""
        return PART_SEPARATOR.join(p for p in parts if p)

    # ============================================================
    # 数据库查询
    # ============================================================

    def _get_binding_prompt(self, agent_id, binding_type, condition=None, conn=None):
        """查询 agent_prompt_bindings 获取对应类型绑定的 prompt 内容。

        优先按 priority 取第一条 is_active 的 prompt。
        condition: 可选的 JSON 条件匹配（scene/mode 类型）——Python 侧精确判断。
        conn: 可选复用连接（_do_resolve 单连接优化）。
        """
        if not agent_id:
            return ''
        if conn is None:
            with self.models.get_db() as db:
                return self._get_binding_prompt(agent_id, binding_type, condition)
        rows = conn.execute("""
            SELECT p.content AS content, b.condition AS cond, b.priority AS priority
            FROM agent_prompt_bindings b
            JOIN agent_prompts p ON p.id = b.prompt_id
            WHERE b.agent_id = %s
              AND b.binding_type = %s
              AND p.is_active = TRUE
            ORDER BY b.priority DESC, p.version DESC
        """, (agent_id, binding_type)).fetchall()
        for r in rows:
            if condition:
                # 精确匹配：解析 condition JSON，值包含指定 condition 才命中
                if not self._cond_contains(r['cond'], condition):
                    continue
            if r['content']:
                return r['content']
        return ''

    @staticmethod
    def _cond_contains(cond_json, value):
        """判断 condition JSON 的任一值是否等于 value（精确匹配，防误命中）。"""
        if not cond_json:
            return False
        try:
            cond = json.loads(cond_json)
        except (ValueError, TypeError):
            return False
        if isinstance(cond, dict):
            return value in cond.values()
        if isinstance(cond, (list, tuple)):
            return value in cond
        return cond == value

    def _get_global_rules(self, conn=None):
        """全局安全规则：prompt_type='rule' AND domain='general'"""
        if conn is None:
            with self.models.get_db() as db:
                return self._get_global_rules(conn=db)
        row = conn.execute("""
            SELECT content FROM agent_prompts
            WHERE prompt_type = 'rule' AND domain = 'general'
              AND is_active = TRUE
            ORDER BY priority DESC, version DESC
            LIMIT 1
        """).fetchone()
        return row['content'] if row else ''

    def _match_scene(self, task_context, conn=None):
        """场景模版：prompt_type='scene' 且 task_triggers 精确匹配 task_type。

        使用 JSON 解析 + Python 侧精确成员判断，而非 LIKE 模糊匹配，
        避免部分匹配（如 'image' 误命中 'image_gen'）导致的调度漂移。
        """
        task_type = (task_context.get('task_type') or '').strip()
        if not task_type:
            return ''
        if conn is None:
            with self.models.get_db() as db:
                return self._match_scene(task_context, conn=db)
        rows = conn.execute("""
            SELECT content, task_triggers AS triggers FROM agent_prompts
            WHERE prompt_type = 'scene' AND is_active = TRUE
            ORDER BY priority DESC, version DESC
        """).fetchall()
        for r in rows:
            triggers = self._json_list(r['triggers'])
            if task_type in triggers and r['content']:
                return r['content']
        return ''

    @staticmethod
    def _json_list(raw):
        """解析 JSON 数组，失败返回空列表（防御性）。"""
        if not raw:
            return []
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except (ValueError, TypeError):
            return []

    def _match_by_tag(self, tag, prompt_types=None, conn=None):
        """按标签精确匹配：tags JSON 数组精确包含指定 tag 的 prompt。"""
        tag = (tag or '').strip()
        if not tag:
            return ''
        if conn is None:
            with self.models.get_db() as db:
                return self._match_by_tag(tag, prompt_types, conn=db)
        sql = """
            SELECT content, tags FROM agent_prompts
            WHERE is_active = TRUE
        """
        params = []
        if prompt_types:
            sql += " AND prompt_type = ANY(%s)"
            params.append(list(prompt_types))
        sql += " ORDER BY priority DESC, version DESC LIMIT 50"
        rows = conn.execute(sql, params).fetchall()
        for r in rows:
            if tag in self._json_list(r['tags']) and r['content']:
                return r['content']
        return ''

    def get_by_tag(self, tag, prompt_types=None):
        """公开入口：按标签查询 prompt 内容（供 Discussion 模式等调用）。"""
        try:
            return self._match_by_tag(tag, prompt_types=prompt_types)
        except Exception as e:
            logger.error(f'PromptResolver.get_by_tag failed: {e}')
            return ''

    # ============================================================
    # 开关与降级
    # ============================================================

    def _is_enabled(self):
        """读取 system_config 的 prompt_resolver_enabled（默认开启）。

        异常时默认 False（安全降级回 legacy），避免配置读取失败导致不可预期行为。
        """
        try:
            with self.models.get_db() as conn:
                row = conn.execute(
                    "SELECT value FROM system_config WHERE key=%s",
                    (ENABLE_CONFIG_KEY,)
                ).fetchone()
                if row and row['value'] is not None:
                    return str(row['value']).lower() not in ('0', 'false', 'no', 'off')
        except Exception as e:
            logger.warning(f'prompt_resolver enabled check failed, defaulting to disabled: {e}')
        return False

    def _legacy_load(self, agent_config):
        """回退：读取 agent_matrix.system_prompt 字段（支持文件路径/直接文本）。"""
        return self._load_prompt(agent_config.get('system_prompt', ''))

    def _load_prompt(self, prompt_source):
        """加载 System Prompt（支持文件路径或直接文本）——原 orchestrator._load_prompt 逻辑。"""
        if not prompt_source:
            return ''
        # 文件路径格式：prompts/xxx.md
        if prompt_source.startswith('prompts/'):
            file_path = os.path.join(BASE_DIR, prompt_source)
            real_path = os.path.realpath(file_path)
            if not real_path.startswith(os.path.realpath(BASE_DIR)):
                logger.warning(f'Prompt 路径遍历尝试被拦截: {file_path}')
                return ''
            if os.path.exists(real_path):
                with open(real_path, 'r', encoding='utf-8') as f:
                    return f.read()
            logger.warning(f'Prompt 文件不存在: {file_path}')
            return ''
        return prompt_source
