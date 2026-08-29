#!/usr/bin/env python3
"""
Agent Matrix — 任务协调核心 (Orchestrator)
========================================
负责任务分解 → Agent 选择 → 任务下发 → 结果收集 → 报告生成。
"""
from i18n import _
import json, os, sys, logging, time
from datetime import datetime

from agent_matrix.cache_utils import get_summary_store

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """
    AgentOrchestrator — 主协调器

    核心流程:
      1. decompose_task(instruction) → Task分解方案
      2. dispatch_sub_tasks(decomposed) → 并行下发子任务
      3. collect_results() → 等待并收集
      4. aggregate_report(results) → 生成汇总报告
    """

    def __init__(self, models_module=None, engine_module=None, runner_class=None):
        self.models = models_module
        self._engine_module = engine_module
        self._runner_class = runner_class
        # 动态提示词解析引擎（懒初始化，避免循环依赖）
        self._prompt_resolver = None
        self._resolver_init_failed = False

    # -------------------------------------------------------
    # 外部入口
    # -------------------------------------------------------

    def process_instruction(self, instruction: str, master_agent_id: int, session_id: str = None, mode: str = 'fast', user_id: int = 0):
        """
        处理用户指令的完整流程（同步版本）

        返回: {
            'master_task_id': 'AT-...',
            'decomposition': [...],
            'sub_task_results': [...],
            'status': 'completed' | 'failed',
            'summary': _('Summary Report'),
            'all_completed': bool
        }
        """
        startup = time.time()
        # 持久化当前模式，供 PromptResolver 动态解析使用
        self._current_mode = mode or ''

        # 注入模式指令
        mode_prefixes = {
            'deep': '【深度思考模式】请进行深入、全面、细致的分析，尽可能给出最详尽的回答。',
            'image': '【图像处理模式】请优先将任务委派给内容管理Agent（内容管理域），由其负责图像生成，包括文生图、图生图、配图等操作。',
        }
        if mode in mode_prefixes:
            instruction = mode_prefixes[mode] + '\n\n' + instruction

        # 图像模式：派发给 Content Agent（含图像能力）
        if mode == 'image':
            content_agents = [a for a in self.models.list_agents(role_type='sub', active_only=True)
                              if a.get('domain') == 'content']
            if content_agents:
                content_agent = content_agents[0]
                instruction = (f'请将以下任务委派给内容管理Agent（ID={content_agent["id"]}, '
                               f'名称={content_agent["name"]}），由其执行图像相关操作：\n\n{instruction}')

        # 1. 创建 Master 任务
        master_task_id = self.models.create_task({
            'source_agent_id': master_agent_id,
            'target_agent_id': master_agent_id,
            'task_type': 'composite',
            'title': instruction[:100],
            'description': instruction,
            'input_data': {'raw_instruction': instruction},
            'max_retries': 1,
            'timeout_seconds': 600,
        })
        self.models.update_task_status(master_task_id, 'running')

        # 2. 获取 Master Agent 配置
        master_config = self.models.get_agent(master_agent_id)
        if not master_config:
            self.models.update_task_status(master_task_id, 'failed', error_message=_('Master Agent does not exist'))
            return {'status': 'failed', 'error': _('Master Agent does not exist')}

        self._add_task_log(master_task_id, master_agent_id, 'info', 'execution',
                           f'Start processing instruction: {instruction[:80]}...')

        # 2.5 能力闸门：指令指向未安装的付费插件能力 → 返回订阅引导
        # 通用能力检测（数据驱动）：CAPABILITY_INDEX 关键词命中 + 本机未安装 → 引导订阅。
        # 已安装插件不触发；未命中关键词不触发（AI 按现有已加载端点正常回复）。
        if user_id:
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'auth-center'))
                from services.module_policy import get_policy_engine, detect_capability_plugins
                _policy_engine = get_policy_engine()
                missing = [pid for pid in detect_capability_plugins(instruction)
                           if not _policy_engine.is_plugin_installed(pid)]
                if missing:
                    prompt = '；'.join(_policy_engine.subscribe_prompt(pid) for pid in missing)
                    logger.info(f'[CapabilityGate] blocked, missing plugins: {", ".join(missing)}')
                    self.models.update_task_status(
                        master_task_id, 'completed', confidence=1.0,
                        self_review=f'Capability blocked, please subscribe: {", ".join(missing)}'
                    )
                    self._add_task_log(master_task_id, master_agent_id, 'info', 'capability',
                                       f'Capability blocked, please subscribe: {", ".join(missing)}')
                    return {
                        'master_task_id': master_task_id,
                        'decomposition': [],
                        'sub_task_results': [],
                        'status': 'completed',
                        'summary': prompt,
                        'all_completed': True,
                        'duration_s': round(time.time() - startup, 2),
                        'capability_blocked': True,
                    }
            except Exception as e:
                logger.warning(f'[CapabilityGate] check failed: {e}')

        # 3. 任务分解
        try:
            decomposed = self.decompose_task(instruction, master_config)
        except Exception as e:
            self.models.update_task_status(master_task_id, 'failed', error_message=str(e))
            self._add_task_log(master_task_id, master_agent_id, 'error', 'execution',
                               f'Task decomposition failed: {e}')
            return {'status': 'failed', 'error': f'Task decomposition failed: {e}', 'master_task_id': master_task_id}

        self._add_task_log(master_task_id, master_agent_id, 'info', 'execution',
                           f'Task decomposition completed: {len(decomposed)} sub-tasks')

        # 4. 保存会话消息
        if session_id:
            self.models.add_message(session_id, 'user', instruction, master_task_id=master_task_id)

        # 5. 下发子任务（传入原始指令用于参考图识别 + user_id 用于模块策略校验 + mode 用于动态提示词）
        sub_results = self.dispatch_sub_tasks(decomposed, master_task_id, session_id,
                                              original_instruction=instruction, user_id=user_id, mode=mode)

        # 6. 汇总结果
        all_completed = all(r.get('status') == 'completed' for r in sub_results)
        total_time = round(time.time() - startup, 2)

        # 7. 更新 Master 任务
        if all_completed:
            self.models.update_task_status(master_task_id, 'completed',
                                           confidence=1.0,
                                           self_review=f'All subtasks completed ({total_time}s)')
        else:
            failed_count = sum(1 for r in sub_results if r.get('status') == 'failed')
            self.models.update_task_status(
                master_task_id,
                'completed' if failed_count < len(sub_results) else 'failed',
                self_review=f'{len(sub_results)} sub-tasks, {failed_count} failed ({total_time}s)'
            )

        self._add_task_log(master_task_id, master_agent_id, 'info', 'execution',
                           f'Task completed ({total_time}s), Status: {"All Completed" if all_completed else "Partially Failed"}')

        # 8. 保存会话回复
        summary = self._build_summary(decomposed, sub_results, total_time, all_completed)
        if session_id:
            self.models.add_message(
                session_id, 'master', summary,
                agent_id=master_agent_id, agent_name='Athena',
                master_task_id=master_task_id
            )

        return {
            'master_task_id': master_task_id,
            'decomposition': decomposed,
            'sub_task_results': sub_results,
            'status': 'completed' if all_completed else 'partial',
            'summary': summary,
            'all_completed': all_completed,
            'duration_s': total_time
        }

    # -------------------------------------------------------
    # 智能记忆：对话结束自动提取（Write 层）
    # -------------------------------------------------------
    _extraction_executor = None

    @classmethod
    def _get_executor(cls):
        """延迟创建 ThreadPoolExecutor（避免多进程问题）"""
        import concurrent.futures
        if cls._extraction_executor is None:
            cls._extraction_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix='kb_extract_'
            )
        return cls._extraction_executor

    def _on_task_complete(self, conversation_text: str, user_id: int, task_result: dict):
        """对话结束 → 异步判断 → 送 Cleaner（不阻塞响应）"""
        try:
            self._get_executor().submit(
                self._async_extract_and_store, conversation_text, user_id, task_result
            )
        except Exception as e:
            logger.warning(f"提交知识提取任务失败 user={user_id}: {e}")

    def _async_extract_and_store(self, conversation_text: str, user_id: int, task_result: dict):
        """后台线程执行提取+入库（不阻塞对话响应）"""
        try:
            if not self._should_extract(conversation_text, task_result):
                return

            facts = self._extract_facts(conversation_text)
            if not facts:
                return

            import os as _os, sys as _sys
            _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', 'auth-center'))
            from routes.cleaner_agent import process_clean_content
            for fact in facts:
                try:
                    process_clean_content(fact, admin_id=user_id)
                except Exception as e:
                    logger.warning(f"单条知识入库失败 user={user_id}: {e}")
        except Exception as e:
            logger.error(f"自动知识提取失败 user={user_id}: {e}")

    def _should_extract(self, conversation_text: str, task_result: dict) -> bool:
        """判断对话是否值得提取知识"""
        import hashlib

        # P1-F07: 全局开关 — 默认关闭自动知识提取，需显式启用
        if os.environ.get('AUTO_KNOWLEDGE_EXTRACT', '0') != '1':
            return False

        conv = (conversation_text or '').strip()
        if not conv:
            return False

        # 纯寒暄过滤：太短的跳过
        if len(conv) < 20:
            return False

        # 敏感信息过滤
        import re
        sensitive_patterns = [
            r'\b1[3-9]\d{9}\b',           # 手机号
            r'\b\d{6}(19|20)\d{8}[\dXx]\b',  # 身份证
            r'(password|密码|secret|密钥|AKSK|access_key)',  # 密钥类
        ]
        for pat in sensitive_patterns:
            if re.search(pat, conv, re.IGNORECASE):
                return False

        # 幂等保护：已处理过的跳过
        try:
            conv_hash = hashlib.md5(conv.encode()).hexdigest()
            from models import get_db
            with get_db() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM knowledge_queue WHERE processed_hash = %s LIMIT 1",
                    (conv_hash,)
                ).fetchone()
                if exists:
                    return False
                # 标记为已处理
                conn.execute(
                    "INSERT INTO knowledge_queue (source, raw_content, status, processed_hash) VALUES (%s,%s,%s,%s)",
                    ('auto_extract', conv[:500], 'processed', conv_hash)
                )
                conn.commit()
        except Exception:
            pass  # 幂等检查失败不影响提取

        # 必须有实质内容才提取
        return True

    def _extract_facts(self, conversation_text: str) -> list:
        """
        用轻量 LLM 调用从对话中提取关键事实。
        返回事实列表，每条为简洁陈述句。
        失败返回空列表，不影响对话响应。
        """
        prompt = (
            "从以下对话中提取关键事实，每条一行，简洁陈述。只提取客观事实，不推测。\n"
            "格式：每行一条事实，以 '- ' 开头。\n"
            "跳过寒暄和闲聊。如果对话中没有任何值得记录的事实，输出 '无'。\n\n"
            "对话：\n" + conversation_text[:4000] + "\n\n"
            "输出示例：\n"
            "- 用户经营餐饮品牌\n"
            "- 用户偏好暖色调设计\n"
            "- 用户上次建了名为XX餐厅的官网\n"
        )

        try:
            # 使用与 orchestrator 相同的 AI 配置（统一走 UnifiedLLM，避免裸 requests + LLM_API_* 环境变量断流）
            agent = self.models.get_agent(1)  # Master Agent 配置
            if not agent:
                return []

            from agent_matrix.engine import UnifiedLLM
            engine = UnifiedLLM(agent)
            content = engine.chat(
                [
                    {'role': 'system',
                     'content': '你是事实提取器。每行输出一条以 "- " 开头的客观事实，无可提取则只输出：无'},
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.2, max_tokens=500, module='memory_extract',
            )

            # 解析事实列表
            facts = []
            for line in (content or '').strip().split('\n'):
                line = line.strip()
                if line.startswith('- ') and len(line) > 3:
                    fact = line[2:].strip()
                    if fact and fact != '无':
                        facts.append(fact)

            logger.info(f"提取事实 {len(facts)} 条: {facts}")
            return facts

        except Exception as e:
            logger.warning(f"事实提取 LLM 调用失败: {e}")
            return []

    # -------------------------------------------------------
    # 任务分解
    # -------------------------------------------------------

    def decompose_task(self, instruction: str, master_config: dict):
        """
        任务分解：让 Master Agent 将指令拆分为子任务

        返回: [{
            'title': str,
            'description': str,
            'target_agent_name': str,
            'task_type': 'execute' | 'review',
            'priority': int,
            'input_data': dict,
            'expected_output': dict
        }, ...]
        """
        # 获取可用 Sub Agents 列表
        all_agents = self.models.list_agents(active_only=True)
        sub_agents = [a for a in all_agents if a['role_type'] == 'sub']

        # 构建系统提示：注入可用 Agent 列表
        agent_list_str = json.dumps(
            [{'name': a['name'], 'domain': a['domain'],
              'managed_modules': a['managed_modules'],
              'description': a['description']}
             for a in sub_agents],
            ensure_ascii=False, indent=2
        )

        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from agent_matrix.engine import UnifiedLLM

        engine = UnifiedLLM(master_config)

        if not engine.is_ready():
            logger.warning("Master Agent AI 引擎未就绪，使用模板分解")
            return self._template_decompose(instruction, sub_agents)

        # 加载 Master Agent 的 System Prompt（动态解析）
        task_ctx = {
            'domain': master_config.get('domain', 'general'),
            'task_type': 'decompose',
            'mode': getattr(self, '_current_mode', '') or '',
            'user_query': instruction[:200],
        }
        master_prompt = self._resolve_prompt(master_config, task_ctx)

        decompose_prompt = f"""{master_prompt}

现在，用户下达了以下指令：

<instruction>
{instruction}
</instruction>

当前可用的 Sub Agent 团队：
{agent_list_str}

请按以下 JSON 格式输出任务分解方案（不要加 markdown 代码块标记，只输出纯 JSON）：

{{
  "tasks": [
    {{
      "title": _("Sub-task Summary Title"),
      "description": "子任务详细描述，包含执行要求",
      "target_agent_name": _("Target Agent Name (must be in the list above)"),
      "task_type": "execute",
      "priority": 5,
      "input_data": {{"action": _("Specific Operation"), "params": {{...}}}},
      "expected_output": {{"fields": [_("Expected output field name")]}}
    }}
  ]
}}

注意：
- 如果没有合适的 Sub Agent，设 target_agent_name 为 "none" 说明原因
- 如果指令可以直接回复（无需子任务），输出一个空 tasks 数组并设置 direct_reply
- 任务之间如果有依赖关系，在 description 中注明
- 每个任务必须能独立执行"""

        response = engine.ask(decompose_prompt, temperature=0.3)

        # 解析 JSON 响应
        try:
            # 尝试直接解析
            data = json.loads(response)
            tasks = data.get('tasks', [])
            direct_reply = data.get('direct_reply', '')
        except (json.JSONDecodeError, TypeError):
            # 尝试提取 JSON 块
            import re
            match = re.search(r'\{[\s\S]*"tasks"[\s\S]*\}', response)
            if match:
                try:
                    data = json.loads(match.group())
                    tasks = data.get('tasks', [])
                    direct_reply = data.get('direct_reply', '')
                except (json.JSONDecodeError, TypeError):
                    tasks = []
                    direct_reply = ''
            else:
                tasks = []
                direct_reply = ''

        if not tasks:
            # LLM 已就绪但返回空任务 → 创建直接回复，不回退到模板分解
            # （模板分解仅用于 AI 引擎未就绪的情况，已在 _engine.is_ready() 处理）
            logger.info("Master Agent 返回空子任务，创建直接回复")
            return [{
                'title': _('Direct Reply'),
                'description': instruction,
                'target_agent_id': master_config.get('id'),
                'target_agent_name': master_config.get('name', 'Athena'),
                'target_module': 'general',
                'task_type': 'execute',
                'priority': 5,
                'input_data': {'raw_instruction': instruction},
                'expected_output': {},
                'skip_critique': True,
            }]

        # 图像关键词集合（与 _template_decompose 同步）
        _ai_image_kw = {_('Picture'), _('Image'), _('Illustration'), _('Cover'), _('Poster'), _('Generate Image'), _('Text to Image'),
                        _('Draw'), _('Crop'), _('Compress'), _('Format Conversion'), _('Image Library'), _('Social Media Cover Image')}

        # 将 Agent 名称映射为 ID
        agent_map = {a['name']: a for a in sub_agents}
        result = []
        for t in tasks:
            agent_name = t.get('target_agent_name', '')
            if agent_name and agent_name in agent_map:
                agent = agent_map[agent_name]
                title_desc = (t.get('title', '') + ' ' + t.get('description', '')).lower()
                target_module = 'image' if any(kw in title_desc for kw in _ai_image_kw) else agent.get('domain', '')
                result.append({
                    'title': t.get('title', ''),
                    'description': t.get('description', ''),
                    'target_agent_id': agent['id'],
                    'target_agent_name': agent['name'],
                    'target_module': target_module,
                    'task_type': t.get('task_type', 'execute'),
                    'priority': t.get('priority', 5),
                    'input_data': t.get('input_data', {}),
                    'expected_output': t.get('expected_output', {}),
                })
            else:
                logger.warning(f"任务 '{t.get('title')}' 指向未知 Agent: {agent_name}")

        return result if result else self._template_decompose(instruction, sub_agents)

    def _template_decompose(self, instruction, sub_agents):
        """模板分解：AI 不可用时，根据关键词匹配。
        关键词自动从每个角色的 domain + managed_modules 动态生成。
        """
        instruction_lower = instruction.lower()
        matched = []

        # 图像关键词集合（始终匹配）
        _image_kw = {_('Picture'), _('Image'), _('Illustration'), _('Cover'), _('Poster'), _('Generate Image'), _('Text to Image'),
                     _('Draw'), _('Crop'), _('Compress'), _('Format Conversion'), _('Image Library'), _('Social Media Cover Image')}

        # 从 sub_agents 动态构建关键词映射
        agent_keywords = {}
        for a in sub_agents:
            name = a['name']
            kws = set()
            # 1. domain 关键词
            domain = (a.get('domain') or '').lower()
            if domain and domain != 'general':
                kws.add(domain)
            # 2. managed_modules 关键词
            modules = []
            try:
                modules = json.loads(a.get('managed_modules') or '[]')
            except (json.JSONDecodeError, TypeError):
                pass
            for mod in modules:
                if isinstance(mod, str):
                    kws.add(mod.lower())
                    # 模块名拆分（site_builder → site, builder）
                    for part in mod.replace('-', '_(').split(')_('):
                        if len(part) > 2:
                            kws.add(part)
            # 3. name 拆分关键词
            for part in name.replace(')-', ' ').replace('_(', ') ').split():
                w = part.lower().strip()
                if len(w) > 2:
                    kws.add(w)
            agent_keywords[name] = list(kws)

        agent_map = {a['name']: a for a in sub_agents}
        found_agents = set()

        for agent_name, keywords in agent_keywords.items():
            if agent_name not in agent_map:
                continue
            for kw in keywords:
                if kw in instruction_lower:
                    if agent_name not in found_agents:
                        a = agent_map[agent_name]
                        target_module = 'image' if kw in _image_kw else a.get('domain', '')
                        matched.append({
                            'title': f'{a["description"].split("—")[0] if "—" in a["description"] else a["name"]} — 指令相关操作',
                            'description': instruction[:200],
                            'target_agent_id': a['id'],
                            'target_agent_name': a['name'],
                            'target_module': target_module,
                            'task_type': 'execute',
                            'priority': 5,
                            'input_data': {'raw_instruction': instruction},
                            'expected_output': {'fields': ['result']},
                        })
                        found_agents.add(agent_name)
                    break

        return matched

    # -------------------------------------------------------
    # 任务分发与执行
    # -------------------------------------------------------

    def dispatch_sub_tasks(self, tasks: list, master_task_id: str, session_id: str = None, original_instruction: str = '', user_id: int = 0, mode: str = ''):
        """
        并行分发并执行子任务（ThreadPoolExecutor + 超时熔断）

        返回: [{
            'sub_task_id': 'AT-...',
            'agent_name': '...',
            'status': 'completed' | 'failed',
            'response': '...',
            'confidence': 0.0,
            'self_review': '...',
            'logs': [...]
        }, ...]
        """
        if not tasks:
            return []
            
        from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
        import threading

        results = []
        futures = {}
        completed_count = 0
        total = len(tasks)
        results_lock = threading.Lock()
        # 将 mode 注入每个子任务，供 _execute_standard_agent 读取（避免跨线程共享实例属性）
        for t in tasks:
            t['_mode'] = mode

        def _run_single(task_def):
            """在线程池中执行单个子任务"""
            target_id = task_def['target_agent_id']
            agent_config = self.models.get_agent(target_id)
            if not agent_config:
                return {
                    'sub_task_id': None,
                    'agent_name': task_def.get('target_agent_name', '?'),
                    'status': 'failed',
                    'error': _('Agent configuration does not exist'),
                    'title': task_def.get('title', ''),
                }

            # ── Phase 1: 模块策略校验 ──
            module_key = None
            _policy_engine = None
            if user_id and agent_config:
                try:
                    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'auth-center'))
                    from services.module_policy import get_policy_engine, get_module_key
                    _policy_engine = get_policy_engine()
                    module_key = get_module_key(agent_config, task_def)

                    if module_key:
                        # 1. 检查模块是否可用
                        allowed, reason = _policy_engine.check_access(user_id, module_key)
                        if not allowed:
                            return {
                                'sub_task_id': None,
                                'agent_name': task_def.get('target_agent_name', '?'),
                                'status': 'blocked',
                                'error': reason or f'Module {module_key} is unavailable',
                                'title': task_def.get('title', ''),
                            }

                        # 2. 检查配额（仅 interactive 模式生效）
                        ok, used, limit = _policy_engine.check_quota(user_id, module_key)
                        if not ok:
                            return {
                                'sub_task_id': None,
                                'agent_name': task_def.get('target_agent_name', '?'),
                                'status': 'blocked',
                                'error': f'Module {module_key} daily quota exhausted ({used}/{limit}), please upgrade',
                                'title': task_def.get('title', ''),
                            }
                except Exception as e:
                    logger.warning(f"[ModulePolicy] check failed for user={user_id}: {e}")

            # 创建子任务记录
            sub_task_id = self.models.create_task({
                'source_agent_id': agent_config.get('id', 0),
                'target_agent_id': target_id,
                'parent_task_id': master_task_id,
                'master_task_id': master_task_id,
                'task_type': task_def.get('task_type', 'execute'),
                'title': task_def.get('title', ''),
                'description': task_def.get('description', ''),
                'input_data': task_def.get('input_data', {}),
                'expected_output': task_def.get('expected_output', {}),
                'target_module': task_def.get('target_module', ''),
                'priority': task_def.get('priority', 5),
                'max_retries': 2,
                'timeout_seconds': 300,
            })
            self.models.update_task_status(sub_task_id, 'running')

            target_module = task_def.get('target_module', '')
            if target_module == 'image':
                exec_result = self._execute_image_agent(
                    task_def, agent_config, sub_task_id, target_id,
                    session_id, original_instruction
                )
            else:
                exec_result = self._execute_standard_agent(
                    task_def, agent_config, sub_task_id, target_id,
                    session_id, master_task_id
                )

            # 更新结果状态
            if exec_result['status'] == 'completed':
                self.models.update_task_status(
                    sub_task_id, 'completed',
                    result_data=exec_result.get('response', ''),
                    confidence=exec_result.get('confidence', 0.9),
                    self_review=exec_result.get('self_review', '')
                )
                self.models.update_agent_stats(target_id, success=True)

                # Phase 1: 记录模块用量（仅成功执行 + 付费模块）
                if module_key and user_id and _policy_engine:
                    try:
                        _policy_engine.record_usage(user_id, module_key, target_id, sub_task_id)
                    except Exception as _pe:
                        logger.warning(f"[ModulePolicy] record_usage failed: {_pe}")
            else:
                self.models.update_task_status(
                    sub_task_id, 'failed',
                    error_message=exec_result.get('response', ''),
                    confidence=0.0
                )

            if session_id:
                status_icon = 'completed' if exec_result['status'] == 'completed' else 'failed'
                self.models.add_message(
                    session_id, 'system',
                    f"[{status_icon}] {task_def.get('title', '')}: {exec_result['status']} (confidence={exec_result.get('confidence', 0)})",
                    metadata={'sub_task_id': sub_task_id, 'status': exec_result['status']},
                    master_task_id=master_task_id
                )

            self._add_task_log(sub_task_id, target_id, 'info', 'execution',
                               f'{exec_result["status"]}: confidence={exec_result.get("confidence", 0)}')

            return {
                'sub_task_id': sub_task_id,
                'agent_name': agent_config.get('name', ''),
                'agent_id': target_id,
                'status': exec_result['status'],
                'response': exec_result.get('response', ''),
                'confidence': exec_result.get('confidence', 0),
                'self_review': exec_result.get('self_review', ''),
                'logs': exec_result.get('logs', []),
                'title': task_def.get('title', ''),
                'image_url': exec_result.get('image_url', ''),
            }

        # 用 ThreadPoolExecutor 并行提交所有任务
        max_workers = min(len(tasks), 5)  # 最多5个并行
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for task_def in tasks:
                future = executor.submit(_run_single, task_def)
                futures[future] = task_def

            # 等待完成（带超时）
            timeout_per_task = 300  # 每个任务最多300秒
            try:
                for future in as_completed(futures, timeout=timeout_per_task):
                    try:
                        result = future.result()
                        with results_lock:
                            results.append(result)
                            completed_count += 1
                    except Exception as e:
                        task_def = futures[future]
                        with results_lock:
                            results.append({
                                'sub_task_id': None,
                                'agent_name': task_def.get('target_agent_name', '?'),
                                'status': 'failed',
                                'error': str(e),
                                'title': task_def.get('title', ''),
                            })
                            completed_count += 1
            except TimeoutError:
                # 超时未完成的任务标记为 failed
                for future in futures:
                    if not future.done():
                        task_def = futures[future]
                        with results_lock:
                            results.append({
                                'sub_task_id': None,
                                'agent_name': task_def.get('target_agent_name', '?'),
                                'status': 'failed',
                                'error': _('Task execution timed out (300s)'),
                                'title': task_def.get('title', ''),
                            })
                            completed_count += 1

        return results

    def _compress_history(self, conv, agent_config, session_id=None):
        """构建注入 Agent 的历史消息。

        - 会话 <= 8 条：直接返回原文。
        - 会话 > 8 条：保留最近 6 条原文，对更早消息用 LLM 生成一段摘要，
          作为一条 assistant 记忆消息插到最前。LLM 不可用/失败时回退为 conv[-6:]。
        - Phase 3: 摘要写入 .cache/sessions/ 缓存，避免重复 LLM 调用。
        """
        recent_n = 6
        threshold = 8
        if not conv:
            return None
        if len(conv) <= threshold:
            return [{'role': m['role'], 'content': m['content']} for m in conv]

        older = conv[:-recent_n]
        recent = conv[-recent_n:]
        recent_msgs = [{'role': m['role'], 'content': m['content']} for m in recent]

        # Phase 3: check session summary cache before calling LLM
        summary = None
        if session_id:
            summary = get_summary_store().get_summary(session_id, len(conv))

        if summary is None:
            summary = self._summarize_messages(older, agent_config)
            # Phase 3: cache the generated summary
            if summary and session_id:
                get_summary_store().set_summary(
                    session_id, summary, len(conv),
                    (0, len(conv) - recent_n),
                    agent_config.get('model_name', '')
                )

        if not summary:
            # 摘要失败，回退为最近 6 条原文
            return recent_msgs

        memory_msg = {'role': 'assistant', 'content': f'[历史对话摘要]\n{summary}'}
        return [memory_msg] + recent_msgs

    def _summarize_messages(self, messages, agent_config):
        """用 LLM 把较早的历史消息压缩成摘要，失败返回 None"""
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
            from agent_matrix.engine import UnifiedLLM
            engine = UnifiedLLM(agent_config)
            if not engine.is_ready():
                return None
            convo_text = '\n'.join(
                f"{m['role']}: {m['content']}" for m in messages
            )[:4000]
            prompt = (
                "请把下面的多轮对话压缩成简洁摘要，保留关键事实、结论与未完成事项，"
                "用要点列出，不要寒暄：\n\n" + convo_text
            )
            summary = engine.ask(prompt, temperature=0.3)
            if not summary or summary.startswith('Error:'):
                return None
            return summary.strip()
        except Exception as e:
            logger.warning(f"历史摘要压缩失败，回退最近消息: {e}")
            return None

    def _execute_standard_agent(self, task_def, agent_config, sub_task_id, target_id,
                                 session_id, master_task_id):
        """执行标准 LLM Agent"""
        from agent_matrix.agent_runner import AgentRunner
        # 动态解析 System Prompt
        task_ctx = {
            'domain': agent_config.get('domain', 'general'),
            'task_type': task_def.get('task_type', 'execute'),
            'mode': task_def.get('_mode', getattr(self, '_current_mode', '') or ''),
            'user_query': task_def.get('description', ''),
        }
        prompt = self._resolve_prompt(agent_config, task_ctx)
        if prompt:
            agent_config['system_prompt'] = prompt
        runner = AgentRunner(agent_config, db_models=self.models)
        history = None
        if session_id:
            conv = self.models.get_conversation(session_id)
            history = self._compress_history(conv, agent_config, session_id)
        if session_id:
            self.models.add_message(
                session_id, 'system', f"Start Execution: {task_def.get('title', '')}",
                agent_id=target_id, agent_name=agent_config.get('name', ''),
                master_task_id=master_task_id
            )
        return runner.execute({
            'task_id': sub_task_id,
            'title': task_def.get('title', ''),
            'description': task_def.get('description', ''),
            'input_data': task_def.get('input_data', {}),
            'expected_output': task_def.get('expected_output', {}),
            'max_retries': 2,
            'skip_critique': task_def.get('skip_critique', False),
        }, history=history)

    def _execute_image_agent(self, task_def, agent_config, sub_task_id, target_id,
                              session_id, original_instruction):
        """执行图片处理 Agent（Wan2.7 / PIL）"""
        import os, re as _re, uuid, json as _json
        exec_result = {'status': 'completed', 'response': '', 'image_url': '', 'confidence': 0.95}
        try:
            prompt = task_def.get('description', '') or task_def.get('title', '')
            ref_image_url = None
            ref_local_path = None

            # 从指令中提取参考图 URL
            urls = _re.findall(r'/static/uploads/temp/[^\s]+', original_instruction)
            if urls:
                rel_path = urls[0]
                from services.deployment_config import deploy
                ref_image_url = deploy.url('agent') + rel_path
                ref_local_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    '..', 'admin', 'static', 'uploads', 'temp',
                    os.path.basename(rel_path)
                )
            else:
                try:
                    conv = self.models.get_conversation(session_id) if session_id else []
                    for msg in reversed(conv):
                        content = str(msg.get('content', ''))
                        old_urls = _re.findall(r'/static/uploads/temp/[^\s]+', content)
                        if old_urls:
                            rel_path = old_urls[0]
                            from services.deployment_config import deploy
                            ref_image_url = deploy.url('agent') + rel_path
                            ref_local_path = os.path.join(
                                os.path.dirname(os.path.abspath(__file__)),
                                '..', 'admin', 'static', 'uploads', 'temp',
                                os.path.basename(rel_path)
                            )
                            break
                except:
                    pass
            self._add_task_log(sub_task_id, target_id, 'info', 'image_gen',
                               f'Reference image: {ref_image_url or "none"}')

            # 视觉识别
            vision_analysis = ''
            if ref_image_url and prompt:
                try:
                    from services.ai_content_generator import analyze_image
                    if any(kw in prompt for kw in [_('Extract'),_('Crop'),_('Capture'),_('Background Removal'),_('Withdraw')]):
                        vq = f'用户要求: {prompt}\n请分析这张图片中用户想要提取的区域的精确位置坐标(x,y,width,height)，只返回JSON: {{"x":数字,"y":数字,"w":数字,"h":数字}}'
                    elif any(kw in prompt for kw in [_('Add Text'),_('Write Text'),_('Add Text')]):
                        vq = f'用户要求: {prompt}\n请描述图片的布局，建议文字添加的最佳位置'
                    else:
                        vq = f'用户要求: {prompt}\n请详细描述这张图片的内容、风格、颜色、布局'
                    vision_analysis = analyze_image(ref_image_url, question=vq)
                except:
                    pass

            TEMP_DIR = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                '..', 'admin', 'static', 'uploads', 'temp'
            )
            os.makedirs(TEMP_DIR, exist_ok=True)

            # 解析裁剪坐标
            crop_coords = None
            if vision_analysis:
                j_match = _re.search(r'\{[^}]+\}', vision_analysis)
                if j_match:
                    try:
                        jd = _json.loads(j_match.group())
                        if all(k in jd for k in ['x','y','w','h']):
                            crop_coords = (jd['x'], jd['y'], jd['x']+jd['w'], jd['y']+jd['h'])
                    except:
                        pass

            # 操作路由表
            def _op_crop(p, ref_path):
                from PIL import Image as _PIL
                img = _PIL.open(ref_path)
                w, h = img.size
                if crop_coords: box = crop_coords
                elif _('Top-left') in p: box = (0, 0, min(int(w*0.20),400), min(int(h*0.15),200))
                elif _('Top Right') in p: box = (max(0,w-min(int(w*0.20),400)), 0, w, min(int(h*0.15),200))
                elif _('Bottom-left') in p: box = (0, max(0,h-min(int(h*0.15),200)), min(int(w*0.20),400), h)
                elif _('Bottom Right') in p: box = (max(0,w-min(int(w*0.20),400)), max(0,h-min(int(h*0.15),200)), w, h)
                elif _('Middle') in p: box = (int(w*0.25), int(h*0.25), int(w*0.75), int(h*0.75))
                else: box = (0, 0, min(int(w*0.20),400), min(int(h*0.15),200))
                cr = img.crop(box)
                fn = f'{uuid.uuid4().hex}.png'
                cr.save(os.path.join(TEMP_DIR, fn))
                return f'/static/uploads/temp/{fn}', f'Cropped area {box}'

            def _op_resize(p, ref_path):
                from PIL import Image as _PIL
                img = _PIL.open(ref_path)
                nums = _re.findall(r'(\d+)\s*[xX*]\s*(\d+)', p)
                nw, nh = (int(nums[0][0]), int(nums[0][1])) if nums else (img.width//2, img.height//2)
                img.resize((nw, nh)).save(os.path.join(TEMP_DIR, fn := f'{uuid.uuid4().hex}.png'))
                return f'/static/uploads/temp/{fn}', f'Scaled to {nw}x{nh}'

            def _op_add_text(p, ref_path):
                from PIL import Image, ImageDraw, ImageFont
                img = Image.open(ref_path).convert('RGBA')
                txt = Image.new('RGBA', img.size, (0,0,0,0))
                draw = ImageDraw.Draw(txt)
                text = p.replace(_('Add Text'),'').replace(_('Write Text'),'').replace(_('Add Text'),'').replace(_('Add Watermark'),'').strip().strip('，,') or 'AI'
                try: font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 36)
                except: font = ImageFont.load_default()
                bbox = draw.textbbox((0,0), text, font=font)
                x, y = (img.width-(bbox[2]-bbox[0]))//2, img.height-(bbox[3]-bbox[1])-20
                draw.text((x,y), text, fill=(255,255,255,255), font=font)
                Image.alpha_composite(img, txt).convert('RGB').save(os.path.join(TEMP_DIR, fn := f'{uuid.uuid4().hex}.png'))
                return f'/static/uploads/temp/{fn}', f'Added text "{text}"'

            def _op_rotate(p, ref_path):
                from PIL import Image as _PIL
                deg = _re.findall(r'(\d+)', p)
                _PIL.open(ref_path).rotate(int(deg[0]) if deg else 90, expand=True).save(
                    os.path.join(TEMP_DIR, fn := f'{uuid.uuid4().hex}.png'))
                return f'/static/uploads/temp/{fn}', f'Rotated {deg[0] if deg else 90} degrees'

            def _op_compress(p, ref_path):
                from PIL import Image as _PIL
                fn = f'{uuid.uuid4().hex}.jpg'
                _PIL.open(ref_path).convert('RGB').save(os.path.join(TEMP_DIR, fn), quality=60)
                return f'/static/uploads/temp/{fn}', _('Compressed (quality=60)')

            action_map = [
                ([_('Extract'),_('Crop'),_('Capture'),_('Background Removal'),_('Withdraw'),_('Cut-out')], _op_crop),
                ([_('Add Text'),_('Write Text'),_('Add Text'),_('Add Watermark')], _op_add_text),
                ([_('Compress'),_('Decrease')], _op_compress),
                ([_('Zoom'),_('Resize'),_('Zoom in'),_('Zoom In')], _op_resize),
                ([_('Rotate'),_('Flip')], _op_rotate),
            ]

            op_result = None
            if ref_local_path and os.path.exists(ref_local_path):
                for keywords, handler in action_map:
                    if any(kw in prompt for kw in keywords):
                        op_result = handler(prompt, ref_local_path)
                        break

            if op_result:
                local_url, msg = op_result
                exec_result['image_url'] = local_url
                exec_result['response'] = msg
            elif ref_image_url:
                from services.ai_content_generator import generate_image, _validate_image_url
                import urllib.request as _urlreq
                gen_prompt = f'{prompt}\n\n参考图分析: {vision_analysis[:500]}' if vision_analysis else prompt
                oss_url = generate_image(gen_prompt, size='1280x720', reference_image_url=ref_image_url)
                if oss_url:
                    _validate_image_url(oss_url)
                    img_data = _urlreq.urlopen(oss_url, timeout=30).read()
                    ext = '.jpg' if ('jpg' in oss_url or 'jpeg' in oss_url) else '.webp' if 'webp' in oss_url else '.png'
                    fn = f'{uuid.uuid4().hex}{ext}'
                    with open(os.path.join(TEMP_DIR, fn), 'wb') as f: f.write(img_data)
                    exec_result['image_url'] = f'/static/uploads/temp/{fn}'
                    exec_result['response'] = _('Picture generated')
            elif prompt:
                from services.ai_content_generator import generate_image, _validate_image_url
                import urllib.request as _urlreq
                oss_url = generate_image(prompt, size='1280x720')
                if oss_url:
                    _validate_image_url(oss_url)
                    img_data = _urlreq.urlopen(oss_url, timeout=30).read()
                    ext = '.jpg' if ('jpg' in oss_url or 'jpeg' in oss_url) else '.webp' if 'webp' in oss_url else '.png'
                    fn = f'{uuid.uuid4().hex}{ext}'
                    with open(os.path.join(TEMP_DIR, fn), 'wb') as f: f.write(img_data)
                    exec_result['image_url'] = f'/static/uploads/temp/{fn}'
                    exec_result['response'] = _('Picture generated')
        except Exception as img_err:
            exec_result['status'] = 'failed'
            exec_result['response'] = f'Picture generation failed: {str(img_err)}'
            self._add_task_log(sub_task_id, target_id, 'error', 'image_gen', str(img_err))

        return exec_result

    # -------------------------------------------------------
    # 报告生成
    # -------------------------------------------------------

    def _build_summary(self, decomposed, sub_results, total_time, all_completed):
        """构建人类可读的汇总报告"""
        parts = []
        parts.append(f"📋 **任务执行报告**\n")
        parts.append(f"共 {len(decomposed)} 个子任务 | 耗时 {total_time}s\n")

        for i, (task, result) in enumerate(zip(decomposed, sub_results), 1):
            icon = '✅' if result.get('status') == 'completed' else '❌'
            agent = task.get('target_agent_name', '?')
            conf = result.get('confidence', 0)
            title = task.get('title', '')
            parts.append(f"{icon} #{i} [{agent}] {title}")
            parts.append(f"   ├ Confidence: {conf}")
            if result.get('self_review'):
                parts.append(f"   └ Self-check: {result['self_review']}")
            if result.get('status') == 'failed':
                err_msg = result.get('error', '') or result.get('response', '')
                parts.append(f"   └ Error: {err_msg[:200]}")
            # 添加子任务的实际产出内容
            resp = result.get('response', '')
            if resp and result.get('status') == 'completed' and len(resp) > 5:
                # 截取合理长度显示
                display = resp[:1500]
                if len(resp) > 1500:
                    display += '\n...（内容较长，已截断）'
                parts.append(f"   └ 产出:\n{display}")

        if all_completed:
            parts.append(f"\n✅ **全部 {len(decomposed)} 个子任务完成**")
        else:
            failed = sum(1 for r in sub_results if r.get('status') == 'failed')
            parts.append(f"\n⚠️ **{failed}/{len(decomposed)} 个子任务失败**")

        return '\n'.join(parts)

    # -------------------------------------------------------
    # 辅助方法
    # -------------------------------------------------------

    def _load_prompt(self, prompt_source):
        """加载 System Prompt（支持文件路径或直接文本）"""
        if not prompt_source:
            return ''
        # 如果是文件路径（以 prompts/ 开头）
        if prompt_source.startswith('prompts/'):
            base_dir = os.path.dirname(__file__)
            file_path = os.path.join(base_dir, prompt_source)
            # 防止路径遍历：确保最终路径在 base_dir 内
            real_path = os.path.realpath(file_path)
            if not real_path.startswith(os.path.realpath(base_dir)):
                logger.warning(f"Prompt 路径遍历尝试被拦截: {file_path}")
                return ''
            if os.path.exists(real_path):
                with open(real_path, 'r', encoding='utf-8') as f:
                    return f.read()
            logger.warning(f"Prompt 文件不存在: {file_path}")
            return ''
        return prompt_source

    def _get_prompt_resolver(self):
        """懒初始化 PromptResolver（避免循环依赖）"""
        if self._prompt_resolver is None and not self._resolver_init_failed:
            try:
                from agent_matrix.prompt_resolver import PromptResolver
                self._prompt_resolver = PromptResolver(self.models)
            except Exception as e:
                logger.warning(f"PromptResolver 初始化失败: {e}")
                self._resolver_init_failed = True
        return self._prompt_resolver

    def _resolve_prompt(self, agent_config, task_context):
        """通过 PromptResolver 动态解析 Prompt；失败/不可用时回退 _load_prompt。"""
        resolver = self._get_prompt_resolver()
        if resolver:
            return resolver.resolve(agent_config, task_context)
        return self._load_prompt(agent_config.get('system_prompt', ''))

    def _add_task_log(self, task_id, agent_id, level, log_type, message):
        if self.models:
            try:
                self.models.add_log(task_id, agent_id, level, log_type, message)
            except Exception:
                pass

    # ============================================================
    # Discussion Mode — Multi-Agent Collaborative Orchestration
    # ============================================================

    # Module-level constants
    MAX_CONTEXT_CHARS = 8000
    DISCUSS_TOTAL_TIMEOUT = 300
    AGENT_TIMEOUT = 120
    MAX_DISCUSS_ROUNDS = 3

    # Discussion prompt 源文件路径（标签查询失败时的降级兜底）
    _DISCUSSION_PROMPT_FILES = {
        'planner': 'prompts/discuss_planner.md',
        'reviewer': 'prompts/discuss_reviewer.md',
        'decider': 'prompts/discuss_decider.md',
    }

    def _get_discussion_prompt(self, agent_config, role):
        """动态解析 Discussion 角色提示词。

        优先按标签 discussion_<role> 从 agent_prompts 表查询；
        无匹配时回退读取原 .md 文件（保持既有行为）。

        agent_config: 对应讨论角色的 Agent 配置（供 Resolver 上下文参考）。
        """
        resolver = self._get_prompt_resolver()
        if resolver:
            content = resolver.get_by_tag(f'discussion_{role}')
            if content:
                return content
        return self._load_prompt(self._DISCUSSION_PROMPT_FILES.get(role, ''))

    # ============================================================
    # Helper: find agent by domain
    # ============================================================

    def _find_agent_by_domain(self, domain):
        """Find the first active sub-agent matching the given domain.

        Returns the agent config dict, or None if not found.
        """
        agents = self.models.list_agents(role_type='sub', domain=domain, active_only=True)
        return agents[0] if agents else None

    # ============================================================
    # Core: run a single discussion agent round
    # ============================================================

    def _run_discussion_agent(self, agent_config, task, context, user_id, prompt_path):
        """Run a single agent round in discussion mode.

        Uses the agent's existing provider/model config but overrides the
        system_prompt with the discussion-specific prompt file.

        Args:
            agent_config: dict from agent_matrix table (Builder/Ops/Steward)
            task: str, the task description to send to the agent
            context: list of prior discussion messages
            user_id: int
            prompt_path: str, path to discussion prompt (e.g. 'prompts/discuss_planner.md')

        Returns:
            str, the agent's text response
        """
        import copy
        from agent_matrix.agent_runner import AgentRunner

        config = copy.deepcopy(agent_config)
        config['system_prompt'] = prompt_path

        # Build the full task text including discussion context
        full_task = ''
        if context:
            full_task += '=== Discussion History ===\n'
            for msg in context:
                full_task += f'[{msg["role"]}] {msg["agent"]}:\n{msg["content"]}\n\n'
            full_task += '=== Current Task ===\n'
        full_task += task

        runner = AgentRunner(config, db_models=self.models)
        result = runner.execute({
            'task_id': f'DISCUSS-{time.time()}',
            'title': 'Discussion Round',
            'description': full_task,
            'input_data': {'user_id': user_id},
            'max_retries': 1,
            'skip_critique': True,
        }, history=[])

        return result.get('response', '') if result.get('status') == 'completed' else ''

    # ============================================================
    # Timeout wrapper for single agent round
    # ============================================================

    def _run_discussion_agent_with_timeout(self, agent_config, task, context,
                                            user_id, prompt_path, timeout=None):
        """Run a single agent round with a timeout guard.

        Raises TimeoutError if the agent round exceeds the timeout.
        """
        import threading, queue

        if timeout is None:
            timeout = self.AGENT_TIMEOUT

        result_queue = queue.Queue()

        def runner():
            try:
                res = self._run_discussion_agent(
                    agent_config=agent_config,
                    task=task,
                    context=context,
                    user_id=user_id,
                    prompt_path=prompt_path
                )
                result_queue.put(('ok', res))
            except Exception as e:
                result_queue.put(('error', str(e)))

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            raise TimeoutError(f'Agent round timed out after {timeout}s')

        status, value = result_queue.get_nowait()
        if status == 'error':
            raise RuntimeError(value)
        return value

    # ============================================================
    # JSON parsing with real LLM retries (Fix #1)
    # ============================================================

    def _parse_decision_json(self, decision_text, agent_config, user_id=0, max_retries=2):
        """Parse JSON decision from agent output with real LLM retries.

        Attempt 1: direct regex extraction.
        Attempts 2..N: ask the LLM to reformat its output as pure JSON.
        Returns None if all attempts fail (caller triggers manual approval fallback).
        """
        import re, json as _json

        # Attempt 1: direct parsing
        try:
            match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', decision_text)
            if match:
                return _json.loads(match.group(1))
            match = re.search(r'\{[\s\S]*"approved"[\s\S]*\}', decision_text)
            if match:
                return _json.loads(match.group(0))
        except _json.JSONDecodeError:
            pass

        # Attempts 2..N: LLM re-generation
        last_text = decision_text
        for _attempt in range(max_retries):
            try:
                fix_prompt = (
                    'Your previous output was not valid JSON and could not be parsed.\n'
                    'Output ONLY the following JSON object — no markdown fences, no extra text:\n\n'
                    '{\n'
                    '  "approved": true,\n'
                    '  "confidence": 0.0,\n'
                    '  "reason": "your decision rationale",\n'
                    '  "steps": [{"type": "node_type", "params": {}}]\n'
                    '}\n\n'
                    'Your previous raw output (for reference):\n'
                    f'{last_text[-800:]}'
                )

                fixed = self._run_discussion_agent(
                    agent_config=agent_config,
                    task=fix_prompt,
                    context=[],
                    user_id=user_id,
                    prompt_path=self._get_discussion_prompt(agent_config, 'decider')
                )

                match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', fixed)
                if match:
                    return _json.loads(match.group(1))
                match = re.search(r'\{[\s\S]*"approved"[\s\S]*\}', fixed)
                if match:
                    return _json.loads(match.group(0))

                last_text = fixed
            except (_json.JSONDecodeError, Exception):
                continue

        return None

    # ============================================================
    # Context compaction (Fix #2)
    # ============================================================

    def _compact_context(self, context):
        """Compress discussion context when it exceeds MAX_CONTEXT_CHARS.

        Keeps the most recent message intact; summarizes all earlier messages
        into a short System note so the downstream agent still has context.
        """
        total = sum(len(m['content']) for m in context)
        if total <= self.MAX_CONTEXT_CHARS:
            return context

        latest = context[-1]
        earlier = context[:-1]

        summary = self._summarize_earlier(earlier)

        return [
            {
                'agent': 'System',
                'role': 'Discussion Summary',
                'content': summary
            },
            latest,
        ]

    def _summarize_earlier(self, messages):
        """Generate a compact summary of earlier discussion rounds.

        Uses a lightweight LLM call directly for speed.
        Falls back to truncation if the LLM call fails.
        """
        history_text = '\n\n'.join(
            f'[{m["role"]}] {m["agent"]}:\n{m["content"][:500]}'
            for m in messages
        )

        try:
            from agent_matrix.engine import UnifiedLLM
            llm = UnifiedLLM()
            summary = llm.chat(
                messages=[{
                    'role': 'user',
                    'content': (
                        'Summarize the following multi-agent discussion history '
                        'in under 200 characters. Keep key decisions and revision points:\n\n'
                        f'{history_text}'
                    )
                }],
                max_tokens=300,
                temperature=0.3
            )
            return summary.strip()
        except Exception:
            return '\n'.join(
                f'[{m["role"]}] {m["agent"]}: {m["content"][:100]}...'
                for m in messages
            )

    # ============================================================
    # DAG workflow trigger (Fix #6)
    # ============================================================

    def _trigger_dag_from_plan(self, exec_plan, user_id):
        """Submit the parsed execution plan to the DAG workflow engine.

        Prefers the workflow_id specified by the Decider agent.
        Falls back to DISCUSS_DEFAULT_WORKFLOW_ID env var.
        Uses workflow_id=1 as last resort with a warning.
        """
        import os as _os
        from orchestrator.workflow_engine import WorkflowEngine

        engine = WorkflowEngine()
        workflow_id = exec_plan.get('workflow_id')

        if workflow_id is None:
            workflow_id = _os.environ.get('DISCUSS_DEFAULT_WORKFLOW_ID')
            if workflow_id:
                try:
                    workflow_id = int(workflow_id)
                except (ValueError, TypeError):
                    workflow_id = None

        if workflow_id is None:
            workflow_id = 1
            logger.warning(
                'No workflow_id specified and DISCUSS_DEFAULT_WORKFLOW_ID not set. '
                'Falling back to workflow_id=1.'
            )

        try:
            instance_id = engine.run_workflow(
                workflow_id=workflow_id,
                trigger_type='agent_discussion',
                trigger_config={'user_id': user_id},
                initial_context={
                    'steps': exec_plan.get('steps', []),
                    'confidence': exec_plan.get('confidence', 0),
                    'reason': exec_plan.get('reason', ''),
                }
            )
        except Exception as e:
            raise RuntimeError(f'DAG workflow failed to start: {e}') from e

        return (
            f'Workflow triggered. Instance ID: {instance_id}. '
            f'Steps: {len(exec_plan.get("steps", []))}'
        )

    # ============================================================
    # Main entry: discuss_and_execute (SSE generator)
    # ============================================================

    def discuss_and_execute(self, instruction, user_id=0, session_id=None):
        """Multi-agent discussion orchestration — SSE event generator.

        Protocol: Agent A (Planner) → Agent B (Reviewer) → Agent A (Revise)
                  → Agent C (Decider) → Parse JSON → Trigger DAG

        Yields SSE dicts with keys: type, phase, agent, role, content, timestamp

        On JSON parse failure, yields 'needs_approval' event for manual fallback.
        On agent unavailability, degrades to single-agent fast mode.
        """
        import re as _re
        startup = time.time()
        discussion_context = []
        round_num = 0

        # Helper to emit a discussion event
        def _emit(typ, **kwargs):
            event = {'type': typ, **kwargs}
            if 'timestamp' not in event:
                event['timestamp'] = time.time() - startup
            return event

        # ── Agent availability check with degradation (Fix #5) ──
        planner_agent = self._find_agent_by_domain('site_builder')   # Builder → Planner
        reviewer_agent = self._find_agent_by_domain('ops')           # Ops → Reviewer
        decider_agent = self._find_agent_by_domain('finance')        # Finance → Decider

        missing = []
        if not planner_agent:
            missing.append('Planner (Builder/site_builder)')
        if not reviewer_agent:
            missing.append('Reviewer (Ops/ops)')
        if not decider_agent:
            missing.append('Decider (Finance/finance)')

        if missing:
            degradation_msg = 'Discussion roles unavailable: ' + ', '.join(missing) + '. '
            available = planner_agent or reviewer_agent or decider_agent

            if available:
                yield _emit('warning',
                            content=degradation_msg + 'Degraded to single-agent fast mode.')

                try:
                    plan = self._run_discussion_agent(
                        agent_config=available,
                        task='Generate an executable plan for the following task '
                             'and output it as JSON:\n\n' + instruction,
                        context=[],
                        user_id=user_id,
                        prompt_path=self._get_discussion_prompt(available, 'decider')
                    )
                    exec_plan = self._parse_decision_json(plan, available, user_id)

                    if exec_plan is None:
                        yield _emit('needs_approval',
                                    agent=available.get('name', 'Agent'),
                                    role='Fast Mode',
                                    content=plan,
                                    raw_output=plan,
                                    hint=(
                                        'Single-agent output could not be parsed as JSON. '
                                        'Please review and manually approve.'
                                    ))
                    elif exec_plan.get('approved'):
                        result = self._trigger_dag_from_plan(exec_plan, user_id)
                        yield _emit('message',
                                    agent=available.get('name', 'Agent'),
                                    role='Fast Mode',
                                    content=result)
                    else:
                        yield _emit('message',
                                    agent=available.get('name', 'Agent'),
                                    role='Fast Mode',
                                    content=(
                                        'Plan not approved. '
                                        f'Reason: {exec_plan.get("reason", "Unknown")}'
                                    ))
                except Exception as e:
                    yield _emit('error', content=f'Fast-mode execution failed: {e}')
            else:
                yield _emit('error',
                            content=(
                                degradation_msg +
                                'Cannot start discussion. Please configure at least one '
                                'sub-agent in Admin → Agent Management.'
                            ))
            return

        # ── Round 1: Planner (Agent A) produces initial plan ──
        round_num += 1
        yield _emit('phase', phase='planning',
                    agent=planner_agent['name'], role='Planner',
                    content=f'Round {round_num}: Generating initial plan...')

        plan_v1 = self._run_discussion_agent_with_timeout(
            agent_config=planner_agent,
            task=instruction,
            context=discussion_context,
            user_id=user_id,
            prompt_path=self._get_discussion_prompt(planner_agent, 'planner')
        )

        discussion_context.append({
            'agent': planner_agent['name'],
            'role': 'Planner',
            'content': plan_v1
        })
        yield _emit('message', agent=planner_agent['name'], role='Planner',
                    content=plan_v1)

        discussion_context = self._compact_context(discussion_context)

        # ── Round 2: Reviewer (Agent B) critiques the plan ──
        round_num += 1
        yield _emit('phase', phase='review',
                    agent=reviewer_agent['name'], role='Reviewer',
                    content=f'Round {round_num}: Reviewing plan...')

        review_task = (
            'Review the following execution plan. Identify issues, risks, and '
            'missing steps. Output your review as a JSON object with "revised_steps" '
            'field containing the corrected step list.\n\n'
            f'Plan to review:\n{plan_v1}'
        )

        review_output = self._run_discussion_agent_with_timeout(
            agent_config=reviewer_agent,
            task=review_task,
            context=discussion_context,
            user_id=user_id,
            prompt_path=self._get_discussion_prompt(reviewer_agent, 'reviewer')
        )

        discussion_context.append({
            'agent': reviewer_agent['name'],
            'role': 'Reviewer',
            'content': review_output
        })
        yield _emit('message', agent=reviewer_agent['name'], role='Reviewer',
                    content=review_output)

        discussion_context = self._compact_context(discussion_context)

        # ── Round 3: Planner (Agent A) revises based on review ──
        round_num += 1
        yield _emit('phase', phase='revision',
                    agent=planner_agent['name'], role='Planner (Revise)',
                    content=f'Round {round_num}: Revising plan based on review...')

        revision_task = (
            'Revise your original plan based on the reviewer\'s feedback below. '
            'Output the final revised plan as a JSON object with "steps" field.\n\n'
            f'Your original plan:\n{plan_v1}\n\n'
            f'Reviewer feedback:\n{review_output}\n\n'
            'Output the FINAL revised plan as JSON.'
        )

        plan_v2 = self._run_discussion_agent_with_timeout(
            agent_config=planner_agent,
            task=revision_task,
            context=discussion_context,
            user_id=user_id,
            prompt_path=self._get_discussion_prompt(planner_agent, 'planner')
        )

        discussion_context.append({
            'agent': planner_agent['name'],
            'role': 'Planner (Revised)',
            'content': plan_v2
        })
        yield _emit('message', agent=planner_agent['name'], role='Planner (Revised)',
                    content=plan_v2)

        discussion_context = self._compact_context(discussion_context)

        # ── Round 4: Decider (Agent C) makes final decision ──
        round_num += 1
        yield _emit('phase', phase='decision',
                    agent=decider_agent['name'], role='Decider',
                    content=f'Round {round_num}: Making final decision...')

        decision_task = (
            'You are the final Decider. Review the discussion and make a decision.\n\n'
            f'Original user request:\n{instruction}\n\n'
            f'Final revised plan (v2):\n{plan_v2}\n\n'
            'Output ONLY a JSON object with "approved", "confidence", "reason", and "steps".'
        )

        decision = self._run_discussion_agent_with_timeout(
            agent_config=decider_agent,
            task=decision_task,
            context=discussion_context,
            user_id=user_id,
            prompt_path=self._get_discussion_prompt(decider_agent, 'decider')
        )

        discussion_context.append({
            'agent': decider_agent['name'],
            'role': 'Decider',
            'content': decision
        })
        yield _emit('message', agent=decider_agent['name'], role='Decider',
                    content=decision)

        # ── Round 5: Parse decision → trigger DAG ──
        yield _emit('phase', phase='execution',
                    agent='System', role='Execution Engine',
                    content='Parsing decision and triggering execution...')

        exec_plan = self._parse_decision_json(decision, decider_agent, user_id)

        # Fallback: JSON parsing failed → ask user for manual approval (Fix #4)
        if exec_plan is None:
            yield _emit('needs_approval',
                        agent='Finance', role='Decision Maker',
                        content=decision,
                        raw_output=decision,
                        hint=(
                            'Agent C output could not be parsed as valid JSON. '
                            'Please review the raw output above and either: '
                            '(1) approve with manually entered steps, or (2) reject and re-discuss.'
                        ))
            return

        if not exec_plan.get('approved', False):
            yield _emit('message',
                        agent='System', role='Execution Engine',
                        content=(
                            'Plan not approved. '
                            f'Reason: {exec_plan.get("reason", "Unknown")}'
                        ))
            return

        # Normal path: trigger DAG
        try:
            result = self._trigger_dag_from_plan(exec_plan, user_id)
        except Exception as e:
            yield _emit('error', content=f'Execution failed: {e}')
            return

        yield _emit('message', agent='System', role='Execution Engine',
                    content=result)

        # ── Done ──
        yield _emit('done', agent='System', role='Orchestrator',
                    content='Discussion complete.')


# ============================================================
# 更新 Agent 统计的辅助函数
# ============================================================

def update_agent_stats(agent_id, success=True):
    """更新 Agent 的任务统计"""
    try:
        from agent_matrix import models as m
        with m.get_db() as conn:
            if success:
                conn.execute("""
                    UPDATE agent_matrix
                    SET tasks_total = tasks_total + 1,
                        tasks_success = tasks_success + 1,
                        last_run_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                """, (agent_id,))
            else:
                conn.execute("""
                    UPDATE agent_matrix
                    SET tasks_total = tasks_total + 1,
                        tasks_failed = tasks_failed + 1,
                        last_run_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                """, (agent_id,))
            conn.commit()
    except Exception:
        pass
