#!/usr/bin/env python3
"""Agent Matrix — Agent 执行器

加载 Agent 配置 → 注入 System Prompt → 执行 LLM 调用 → 自检。
"""
from i18n import _
import json, os, sys, time, logging
logger = logging.getLogger(__name__)


class AgentRunner:
    """Agent 执行器：负责一次 Agent 对话的完整生命周期"""

    def __init__(self, agent_config: dict, db_models=None):
        """
        agent_config: agent_matrix 行字典
        db_models: models 模块引用（用于日志记录）
        """
        self.config = agent_config
        self.agent_id = agent_config.get('id', 0)
        self.name = agent_config.get('name', 'Unnamed Agent')
        self.role_type = agent_config.get('role_type', 'sub')
        self.domain = agent_config.get('domain', 'general')

        # 延迟加载 engine，避免循环依赖
        self._engine = None
        self._engine_ready = False
        self.models = db_models

    def _get_engine(self):
        if not self._engine:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
            from agent_matrix.engine import UnifiedLLM
            self._engine = UnifiedLLM(self.config)
            self._engine_ready = self._engine.is_ready()
        return self._engine

    def is_ready(self):
        self._get_engine()
        return self._engine_ready

    def execute(self, task: dict, history: list = None):
        """执行一次 Agent 任务（wrapper：结束后发射 AGENT_TASK_COMPLETED 事件）。"""
        result = self._execute_impl(task, history)
        self._emit_task_completed(task, result)
        return result

    def _execute_impl(self, task: dict, history: list = None):
        """
        执行一次 Agent 任务

        task: {
            'task_id': 'AT-...',
            'title': '...',
            'description': '...',
            'input_data': {...},
        }
        history: 可选的历史对话 (用于多轮)

        Returns: {
            'status': 'completed' | 'failed',
            'response': '...',
            'confidence': 0.0-1.0,
            'self_review': '...',
            'logs': [...]
        }
        """
        logs = []
        task_id = task.get('task_id', 'unknown')

        # 1. 日志：开始执行
        self._log(task_id, 'info', 'execution', f'🤖 {self.name} starts executing the task')
        logs.append(f'[{self.name}] Received task: {task.get("title", "")}')

        # 2. 构建完整的 Prompt
        user_query = self._build_query(task)
        logs.append(f'[Prompt] Construction completed')

        # 3. 调用 LLM
        engine = self._get_engine()
        if not engine or not engine.is_ready():
            err_msg = f'AI Engine Not Ready ({self.config.get("provider", "?")}/{self.config.get("model_name", "?")})'
            self._log(task_id, 'error', 'execution', err_msg)
            return self._fail(err_msg, logs)

        self._log(task_id, 'info', 'api_call',
                   f'Call {self.config.get("provider")}/{self.config.get("model_name")}')

        # 按 allowed_tools 白名单决定是否启用 ReAct 工具循环
        # B-03: 模型/上游错误（401/5xx/网络异常/Error 前缀）走有限重试，受 max_retries 约束
        tools = self._get_tools()
        api_max_retries = task.get('max_retries', 2)
        response = None
        for attempt in range(api_max_retries + 1):
            try:
                if tools:
                    logs.append(f'[Tools] Enabled {len(tools)} tools, entering ReAct loop')
                    response = self._run_react_loop(engine, user_query, history, tools, logs, task_id)
                elif history:
                    response = engine.ask_with_history(history, user_query)
                else:
                    response = engine.ask(user_query)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error(f"[{self.name}] LLM execute failed (attempt {attempt + 1}): {e}\n{tb}")
                logs.append(f'[ERROR] attempt {attempt + 1}: {e}')
                logs.append(f'[TRACEBACK] {tb[:500]}')
                response = f'Error: {e}'

            if isinstance(response, str) and response.startswith('Error:'):
                self._log(task_id, 'warn', 'api_call',
                          f'LLM error (attempt {attempt + 1}): {response[:200]}')
                if attempt < api_max_retries:
                    logs.append(f'[Retry #{attempt + 1}] upstream/model error, retrying')
                    time.sleep(min(2 * (attempt + 1), 8))
                    continue
                self._log(task_id, 'error', 'execution', response)
                return self._fail(f'{response} (after {attempt + 1} attempts)', logs)
            break

        logs.append(f'[LLM] Response length: {len(response)} characters')
        self._log(task_id, 'info', 'execution', f'LLM Response Completed ({len(response)} characters)')

        # 4. 自检 (Self-Critique) — Direct Reply 任务跳过
        if task.get('skip_critique'):
            self._log(task_id, 'info', 'execution', f'✅ Direct reply, confidence=1.0')
            return {
                'status': 'completed',
                'response': response,
                'confidence': 1.0,
                'self_review': 'Direct reply (auto-passed)',
                'logs': logs,
                'retries': 0,
                'failed': False
            }

        self_review = self._self_critique(response, task)
        logs.append(f'[Self-Critique] {self_review.get("review", "None")}')

        confidence = self_review.get('confidence', 0.85)
        self._log(task_id, 'info', 'self_review',
                   f'Self-check Complete: confidence={confidence}, review={self_review.get("review", "")[:100]}')

        # 5. 如果置信度过低，重试
        retries = 0
        max_retries = task.get('max_retries', 2)
        while confidence < 0.7 and retries < max_retries:
            retries += 1
            logs.append(f'[Retry #{retries}] confidence={confidence} < 0.7，重新执行')
            self._log(task_id, 'warn', 'execution',
                       f'重试 #{retries}: confidence={confidence} < 0.7')

            # 用更明确的 prompt 重试，带入自检发现的问题点
            issues = self_review.get('issues', [])
            issues_str = ('\n'.join(f'- {i}' for i in issues)) if issues else _('(No specific issue list)')
            retry_query = (
                f"之前的结果不理想（置信度: {confidence}）。\n"
                f"自检反馈: {self_review.get('review', '')}\n"
                f"需改进的问题:\n{issues_str}\n"
                f"改进建议: {self_review.get('suggestion', '') or _('None')}\n\n"
                f"请针对上述问题重新执行任务。\n\n"
                f"Original task: {user_query}"
            )
            response = engine.ask(retry_query)
            if response.startswith('Error:'):
                break
            self_review = self._self_critique(response, task)
            confidence = self_review.get('confidence', 0.85)
            logs.append(f'[Retry #{retries}] 新 confidence={confidence}')
            self._log(task_id, 'info', 'self_review',
                       f'重试 #{retries} 后 confidence={confidence}')

        # 6. 更新统计
        if self.models:
            self.models.update_agent_stats(self.agent_id, success=(confidence >= 0.7))

        if confidence >= 0.7:
            self._log(task_id, 'info', 'execution', f'✅ Task completed, confidence={confidence}')
            return {
                'status': 'completed',
                'response': response,
                'confidence': confidence,
                'self_review': self_review.get('review', ''),
                'logs': logs,
                'retries': retries,
                'failed': False
            }
        else:
            err_msg = f'After {retries} retries, confidence={confidence} is still below threshold'
            self._log(task_id, 'error', 'execution', err_msg)
            return self._fail(err_msg, logs, confidence)

    def _emit_task_completed(self, task, result):
        """发射 agent.task.completed 事件（内核补丁 B），供 memory_engine 等订阅。

        成功/失败路径都会触发；任何异常仅告警，不影响原执行流程。
        """
        try:
            from plugin_manager.event_bus import get_event_bus, EventName
            ev = getattr(EventName, 'AGENT_TASK_COMPLETED', None)
            if ev is None:
                return
            get_event_bus().emit(ev, task=task, result=result or {},
                                 agent_id=self.agent_id, agent_name=self.name)
        except Exception as e:
            logger.warning(f'[{self.name}] emit task completed failed: {e}')

    def _get_tools(self):
        """按 Agent 的 allowed_tools 返回可用工具 schema，无则返回 []"""
        try:
            from agent_matrix.tools import get_tools_for_agent
            return get_tools_for_agent(self.config.get('allowed_tools'))
        except Exception as e:
            logger.warning(f"[{self.name}] 加载工具失败，退回单轮: {e}")
            return []

    def _run_react_loop(self, engine, user_query, history, tools, logs, task_id,
                        max_rounds=5):
        """ReAct 工具循环：思考→调用工具→观察→再思考，直到模型给出终态答复。

        任何异常/达到轮次上限均安全收尾，返回已有的文本（或错误字符串）。
        """
        from agent_matrix.tools import execute_tool

        # 构建初始消息
        messages = [{"role": "system", "content": self.config.get('system_prompt', '')}]
        if history:
            for h in history:
                role = 'user' if h.get('role') == 'user' else 'assistant'
                messages.append({"role": role, "content": h.get('content', '')})
        messages.append({"role": "user", "content": user_query})

        last_text = ''
        for round_i in range(1, max_rounds + 1):
            msg = engine.chat_with_tools(messages, tools)
            if msg is None:
                logs.append(f'[ReAct #{round_i}] 工具调用返回空，退回普通对话')
                fallback = engine.ask(user_query)
                return fallback if not last_text else last_text

            choice_msg = msg.choices[0].message
            tool_calls = getattr(choice_msg, 'tool_calls', None)
            if choice_msg.content:
                last_text = choice_msg.content

            # 模型未请求工具 → 终态答复
            if not tool_calls:
                self._log(task_id, 'info', 'execution',
                           f'ReAct ended at round {round_i} (No More Tool Calls)')
                return last_text or ''

            # 把 assistant 的 tool_calls 消息追加回上下文
            messages.append({
                "role": "assistant",
                "content": choice_msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    } for tc in tool_calls
                ]
            })

            # 逐个执行工具，把结果作为 tool 消息回灌
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                self._log(task_id, 'info', 'tool_call', f'Call tool {name} args={args}')
                logs.append(f'[ReAct #{round_i}] 调用工具 {name}')
                result = execute_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result)[:4000]
                })

        # 达到轮次上限，做最后一次无工具收尾
        logs.append(f'[ReAct] Reached maximum rounds {max_rounds}, forced termination')
        final = engine.chat_with_tools(messages, tools)
        if final is not None:
            final_choice = final.choices[0].message
            if final_choice.content:
                return final_choice.content
        return last_text or _('(Tool loop reached limit, no final response generated)')

    def _build_query(self, task):
        """构造发给 LLM 的用户消息"""
        parts = [f"## 任务: {task.get('title', '')}"]

        description = task.get('description', '')
        if description:
            parts.append(f"\n{description}")

        input_data = task.get('input_data', {})
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(input_data, dict) and input_data:
            parts.append("\n### 输入参数:")
            for k, v in input_data.items():
                if isinstance(v, str) and len(v) > 500:
                    parts.append(f"- {k}: {v[:500]}...")
                else:
                    parts.append(f"- {k}: {v}")

        expected = task.get('expected_output', {})
        if isinstance(expected, str):
            try:
                expected = json.loads(expected)
            except (json.JSONDecodeError, TypeError):
                pass
        if expected:
            parts.append(f"\n### 期望输出:\n{json.dumps(expected, ensure_ascii=False, indent=2)}")

        parts.append("\n请严格按照要求执行，完成后输出结果。")
        return '\n'.join(parts)

    def _self_critique(self, response, task):
        """自检：先做规则初判，灰区(0.5~0.8)时再让 LLM 做结构化自评"""
        expected = task.get('expected_output', {})
        if isinstance(expected, str):
            try:
                expected = json.loads(expected)
            except (json.JSONDecodeError, TypeError):
                expected = {}

        # 1. 规则初判：检查输出长度和格式
        review_parts = []
        confidence = 0.85

        if len(response) < 50:
            review_parts.append(_("Output is too short (<50 characters)"))
            confidence = max(0.3, confidence - 0.3)
        else:
            review_parts.append(f"Output length is reasonable ({len(response)} characters)")

        if isinstance(expected, dict) and expected.get('fields'):
            # 检查是否包含期望字段
            matched = sum(1 for f in expected['fields'] if f in response.lower())
            field_ratio = matched / len(expected['fields'])
            if field_ratio < 0.5:
                review_parts.append(f"Low expected field match rate ({matched}/{len(expected['fields'])})")
                confidence = max(0.4, confidence - 0.2)

        if 'Error' in response or _('Error') in response or _('Failed"') in response:
            review_parts.append(_("Output contains error/failure information"))
            confidence = max(0.2, confidence - 0.3)

        confidence = round(confidence, 2)
        result = {
            'confidence': confidence,
            'review': '; '.join(review_parts),
            'issues': [],
            'suggestion': ''
        }

        # 2. 灰区触发 LLM 结构化自评（仅 0.5~0.8 之间，控制成本）
        if 0.5 <= confidence <= 0.8:
            llm_review = self._llm_critique(response, task)
            if llm_review:
                result.update(llm_review)

        return result

    def _llm_critique(self, response, task):
        """让 LLM 对输出做结构化自评，失败时返回 None 由规则结果兜底"""
        engine = self._get_engine()
        if not engine or not engine.is_ready():
            return None

        critique_prompt = (
            "你是严格的质量审查员。请评估下面的【任务】与【输出】是否达标，"
            "只输出纯 JSON（不要 markdown 代码块），格式：\n"
            '{"confidence_": 0.0-1.0 Floating Point Number, "issues": [_("Question 1"), ...], "suggestion": _("Improvement suggestions")}\n\n'
            f"【任务】{task.get('title', '')}\n{task.get('description', '')}\n\n"
            f"【输出】\n{response[:2000]}"
        )
        try:
            raw = engine.ask(critique_prompt, temperature=0.2)
            if not raw or raw.startswith('Error:'):
                return None
            import re as _re
            match = _re.search(r'\{[\s\S]*\}', raw)
            if not match:
                return None
            data = json.loads(match.group())
            conf = float(data.get('confidence', 0.85))
            conf = round(max(0.0, min(1.0, conf)), 2)
            issues = data.get('issues', []) or []
            suggestion = data.get('suggestion', '') or ''
            review = _('LLM Self-Evaluation: ') + (suggestion or _('Approved'))
            if issues:
                review += ' | 问题: ' + '; '.join(str(i) for i in issues)
            return {
                'confidence': conf,
                'review': review,
                'issues': issues,
                'suggestion': suggestion
            }
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"[{self.name}] LLM 自评解析失败，回退规则结果: {e}")
            return None

    def _log(self, task_id, level, log_type, message):
        if self.models:
            try:
                self.models.add_log(task_id, self.agent_id, level, log_type, message)
            except Exception:
                pass

    def _fail(self, error, logs, confidence=0.0):
        return {
            'status': 'failed',
            'response': error,
            'confidence': confidence,
            'self_review': '',
            'logs': logs + [f'[FAIL] {error}'],
            'failed': True
        }
