#!/usr/bin/env python3
"""
Node Handlers — 工作流节点类型实现
===================================
所有节点类型的处理函数，注册到 WorkflowEngine。

节点类型列表：
  - ai_agent:      调用 智能体（系统/用户）
  - data_collect:  数据采集（RSS/API）
  - ai_process:    AI 加工内容
  - condition:     条件判断
  - approval:      审批节点
  - publish:       发布到多平台
  - notify:        通知（邮件/Webhook/站内）
  - wait:          等待/延时
  - sub_workflow:  子工作流
  - market_check:  市场数据检查
  - http_request:  HTTP API 调用
  - script:        执行自定义脚本

@package orchestrator
"""

from i18n import _
import os, sys, json, time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, '..', 'auth-center'))
sys.path.insert(0, os.path.join(BASE_DIR, '..'))

from . import models as m
from .safe_eval import safe_eval
from .workflow_engine import ApprovalRequiredError

# ============================================================
# 智能体 节点 — 调用 智能体（系统/用户）
# ============================================================

def handle_ai_agent(node_def: dict, input_data: dict) -> dict:
    """
    智能体 节点处理器。
    配置:
      - agent_type: 'system' | 'user'
      - agent_id: 可选
      - prompt: 要发送给 Agent 的提示词
      - model: 可选，覆盖模型
    """
    config = node_def.get('config', {})
    prompt = config.get('prompt', '')
    agent_type = config.get('agent_type', 'system')
    agent_id = config.get('agent_id')

    if not prompt:
        return {'error': _('Prompt cannot be empty'), 'success': False}

    # 获取 Agent 配置
    if agent_type == 'system':
        agent = m.get_default_system_agent()
        if not agent:
            return {'error': _('System Agent not configured'), 'success': False}
        api_key_ref = agent.get('api_key_ref', 'dashscope_text_key')
        model = config.get('model', agent.get('model', 'qwen-turbo'))
        provider = agent.get('provider', 'dashscope')
    else:
        # 用户 Agent - 从 agents 表查询实际配置
        if not agent_id:
            return {'error': _('User Agent did not specify agent_id'), 'success': False}
        # 查询 agents 表获取实际 Agent 配置
        agent = _get_agent_from_db(agent_id)
        if agent:
            api_key_ref = agent.get('api_key_ref', 'dashscope_text_key')
            model = config.get('model', agent.get('model', 'qwen-turbo'))
            provider = agent.get('provider', 'dashscope')
        else:
            # Agent 不存在时使用默认配置并记录日志
            import logging
            logging.getLogger('orchestrator.nodes').warning(f"Agent {agent_id} not found in database, using defaults")
            api_key_ref = 'dashscope_text_key'
            model = config.get('model', 'qwen-turbo')
            provider = 'dashscope'

    # 从 system_config 获取 API Key
    api_key = _get_api_key(api_key_ref)
    if not api_key:
        return {'error': f'API Key [{api_key_ref}] not configured', 'success': False}

    # 调用 DashScope API
    timeout = config.get('timeout', 120)
    result = _call_dashscope(api_key, model, prompt, timeout=timeout)
    return result


def _get_api_key(key_ref: str) -> str:
    """从 system_config 表获取 API Key"""
    with m.get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_config WHERE key=%s", (key_ref,)
        ).fetchone()
        return row['value'] if row else ''


def _get_agent_from_db(agent_id: int) -> dict:
    """从 agents 表查询 Agent 配置"""
    try:
        with m.get_db() as conn:
            row = conn.execute(
                "SELECT api_key_ref, model, provider FROM agents WHERE id=%s",
                (agent_id,)
            ).fetchone()
            if row:
                return {'api_key_ref': row['api_key_ref'], 'model': row['model'], 'provider': row['provider']}
    except Exception:
        pass
    return {}


def _call_dashscope(api_key: str, model: str, prompt: str, timeout: int = 120) -> dict:
    """调用阿里云 DashScope API"""
    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个专业的AI助手。请严格按要求完成任务。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4096
    }).encode('utf-8')

    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            token_usage = data.get('usage', {})

            return {
                'success': True,
                'content': content,
                'model': model,
                'tokens': token_usage
            }
    except Exception as e:
        return {'error': str(e), 'success': False}


# ============================================================
# 数据采集节点 — 调用内容工厂
# ============================================================

def handle_data_collect(node_def: dict, input_data: dict) -> dict:
    """
    数据采集节点处理器。
    配置:
      - source_ids: [int] 采集源 ID 列表
      - max_per_source: int
      - keywords: [str]
    """
    config = node_def.get('config', {})
    source_ids = config.get('source_ids', [])

    if not source_ids:
        return {'error': _('No data source specified'), 'success': False}

    # 尝试导入内容工厂采集器
    try:
        sys.path.insert(0, os.path.join(BASE_DIR, '..'))
        from services.content_factory import run_collection
    except ImportError:
        return _mock_collect(source_ids)

    results = []
    for sid in source_ids:
        try:
            result = run_collection(source_id=sid, max_items=config.get('max_per_source', 10))
            results.append({
                'source_id': sid,
                'success': True,
                'items_count': result.get('total', 0)
            })
        except Exception as e:
            results.append({
                'source_id': sid,
                'success': False,
                'error': str(e)
            })

    return {'success': True, 'results': results}


def _mock_collect(source_ids: list) -> dict:
    """模拟采集（用于测试或无内容工厂时）"""
    return {
        'success': True,
        'results': [{'source_id': sid, 'success': True, 'items_count': 5}
                     for sid in source_ids],
        '_mock': True
    }


# ============================================================
# AI 加工节点 — 调用 DashScope 加工内容
# ============================================================

def handle_ai_process(node_def: dict, input_data: dict) -> dict:
    """
    AI 内容加工节点。
    配置:
      - instruction: 加工指令
      - fields: ['title', 'summary', 'body', 'keywords']
      - input_from: 前置节点输出字段
    """
    config = node_def.get('config', {})
    instruction = config.get('instruction', _('Interpret and analyze the following content, output a Chinese summary'))
    fields = config.get('fields', ['title', 'summary', 'body', 'keywords'])

    # 获取输入内容（从前置节点或上下文）
    context = input_data.get('context', {})
    input_content = ''

    # 查找输入源
    for key, value in input_data.items():
        if key.startswith('node_') and 'output' in key:
            if isinstance(value, dict):
                if 'content' in value:
                    input_content = value['content']
                elif 'results' in value:
                    input_content = json.dumps(value['results'], ensure_ascii=False)[:3000]
                elif 'body' in value:
                    input_content = value['body']

    if not input_content:
        input_content = config.get('default_input', _('(No input content)'))

    prompt = f"""{instruction}

原始内容:
{input_content[:8000]}

请以 JSON 格式输出，包含字段: {json.dumps(fields, ensure_ascii=False)}
"""

    result = _call_dashscope(
        _get_api_key('dashscope_text_key'),
        config.get('model', 'qwen-turbo'),
        prompt
    )

    if result.get('success'):
        content = result['content']
        # 尝试解析 JSON 输出
        parsed = _try_parse_json(content)
        if parsed:
            result['parsed'] = parsed

    return result


def _try_parse_json(text: str) -> dict:
    """尝试从文本中提取并解析 JSON"""
    import re
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取 JSON 代码块
    match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    return None


# ============================================================
# 条件判断节点
# ============================================================

def handle_condition(node_def: dict, input_data: dict) -> dict:
    """
    条件判断节点（内置实现，重载 WorkflowEngine 默认）。
    配置:
      - expression: 条件表达式 (如 'output.value > 0.05')
      - branches: [{'label': _('Rise'), 'expression': '> 0'}, ...]
    """
    config = node_def.get('config', {})
    expression = config.get('expression', 'true')
    branches = config.get('branches', [])

    # 收集所有上下文变量用于条件评估
    local_vars = {}
    local_vars.update(input_data.get('context', {}))
    for key, val in input_data.items():
        if key.startswith('node_'):
            if isinstance(val, dict):
                local_vars.update(val)

    try:
        result = safe_eval(expression, local_vars)
    except Exception as e:
        return {
            'passed': True,
            'condition_result': True,
            'error': str(e),
            'expression': expression
        }

    # 检查分支匹配
    matched_branch = None
    for branch in branches:
        try:
            _expr = branch.get('expression', '')
            if bool(safe_eval(_expr, local_vars)):
                matched_branch = branch.get('label', 'unknown')
                break
        except Exception:
            continue

    return {
        'passed': result,
        'condition_result': result,
        'matched_branch': matched_branch,
        'expression': expression
    }


# ============================================================
# 发布节点 — 多平台发布
# ============================================================

def handle_publish(node_def: dict, input_data: dict) -> dict:
    """
    发布节点。支持平台: cms, skill, social。
    配置:
      - platforms: ['cms', 'skill', 'social']
      - content_source: 从哪个前置节点获取内容
    """
    config = node_def.get('config', {})
    platforms = config.get('platforms', ['cms'])
    content = input_data.get('content', '')

    # 从前置节点查找内容
    for key, val in input_data.items():
        if key.startswith('node_'):
            if isinstance(val, dict):
                content = val.get('content', val.get('parsed', val.get('body', content)))
                if isinstance(content, dict):
                    content = json.dumps(content, ensure_ascii=False)

    results = {}
    for platform in platforms:
        try:
            if platform == 'cms':
                results[platform] = _publish_to_cms(config, content)
            elif platform == 'skill':
                results[platform] = _publish_to_skill(config, content)
            elif platform == 'social':
                results[platform] = _publish_to_social(config, content)
            else:
                results[platform] = {'success': False, 'error': f'Unknown platform: {platform}'}
        except Exception as e:
            results[platform] = {'success': False, 'error': str(e)}

    return {
        'success': any(r.get('success') for r in results.values()),
        'results': results
    }


def _publish_to_cms(config: dict, content: str) -> dict:
    """发布到 CMS"""
    try:
        from models.cms import upsert_post
        title = config.get('title', _('Auto Publish'))
        category = config.get('category', 'content_factory')
        slug = f'auto-{config.get("workflow_instance_id", "wf")}-{int(time.time())}'

        post_id = upsert_post(
            title=title,
            content=content,
            category=category,
            slug=slug,
            author=_('Automation System')
        )
        return {'success': True, 'post_id': post_id, 'slug': slug}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def _publish_to_skill(config: dict, content: str) -> dict:
    """推送为 Skill"""
    from services.content_factory.skill_pusher import push_to_skill
    result = push_to_skill(
        processed_id=config.get('processed_id', 0),
        title=config.get('title', _('Auto Skill')),
        description=config.get('description', '由工作流自动生成'),
        content=content,
        category=config.get('category', 'automation')
    )
    return result


def _publish_to_social(config: dict, content: str) -> dict:
    """发布到社交媒体 — 对接各平台发布 API"""
    import logging
    logger = logging.getLogger('orchestrator.nodes')
    platforms = config.get('platforms', [])
    results = {}
    all_success = True

    for platform in platforms:
        try:
            platform_key = f'{platform.upper()}_API_KEY'
            api_key = os.environ.get(platform_key, '')
            if not api_key:
                results[platform] = {'success': False, 'error': f'API key not configured for {platform}'}
                all_success = False
                continue
            results[platform] = _publish_to_platform(platform, api_key, content)
        except Exception as e:
            logger.warning(f"Publish to {platform} failed: {e}")
            results[platform] = {'success': False, 'error': str(e)}
            all_success = False

    return {
        'success': all_success,
        'platforms': platforms,
        'results': results
    }


def _publish_to_platform(platform: str, api_key: str, content: str) -> dict:
    """单平台发布"""
    import urllib.request as ur
    import logging
    logger = logging.getLogger('orchestrator.nodes')

    platform_endpoints = {
        'weixin': 'https://api.weixin.qq.com/cgi-bin/message/custom/send',
        'weibo': 'https://api.weibo.com/2/statuses/update.json',
        'toutiao': 'https://developer.toutiao.com/api/v2/article/post',
    }

    endpoint = platform_endpoints.get(platform)
    if not endpoint:
        return {'success': False, 'error': f'Unknown platform: {platform}'}

    payload = json.dumps({'content': content[:2000]}).encode('utf-8')
    req = ur.Request(endpoint, data=payload, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    try:
        with ur.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return {'success': True, 'platform': platform, 'response': data}
    except Exception as e:
        logger.warning(f"Publish to {platform} API failed: {e}")
        return {'success': False, 'error': str(e), 'platform': platform}


# ============================================================
# 通知节点
# ============================================================

def handle_notify(node_def: dict, input_data: dict) -> dict:
    """
    通知节点。
    配置:
      - channels: ['email', 'webhook', 'notification']
      - title: 通知标题
      - message: 通知内容（支持模板）
      - webhook_url: webhook URL
      - email_to: 收件人
    """
    config = node_def.get('config', {})
    channels_raw = config.get('channels', ['notification'])

    # 类型安全：统一转为列表
    if isinstance(channels_raw, str):
        # 逗号分隔的字符串 "notification,email" 或单个字符串 "notification"
        channels = [c.strip() for c in channels_raw.split(',') if c.strip()]
    elif isinstance(channels_raw, list):
        channels = channels_raw
    else:
        channels = ['notification']

    title = config.get('title', _('Workflow Notification'))
    message = config.get('message', '')

    # 模板变量替换
    ctx = input_data.get('context', {})
    for k, v in ctx.items():
        if isinstance(v, str):
            message = message.replace(f'{{{{{k}}}}}', v)
            title = title.replace(f'{{{{{k}}}}}', v)

    results = {}
    for channel in channels:
        try:
            if channel == 'notification':
                results[channel] = _send_notification(title, message)
            elif channel == 'webhook':
                results[channel] = _send_webhook(config.get('webhook_url', ''), title, message)
            elif channel == 'email':
                results[channel] = _send_email(config.get('email_to', ''), title, message)
            else:
                results[channel] = {'success': False, 'error': f'Unknown channel: {channel}'}
        except Exception as e:
            results[channel] = {'success': False, 'error': str(e)}

    return {
        'success': any(r.get('success') for r in results.values()),
        'results': results
    }


def _send_notification(title: str, message: str) -> dict:
    """发送站内通知 — 写入 notifications 表并尝试发送 Webhook"""
    try:
        import logging
        logger = logging.getLogger('orchestrator.nodes')
        # 写入站内通知
        with m.get_db() as conn:
            conn.execute(
                "INSERT INTO notifications (title, content, type, created_at) VALUES (%s, %s, %s, %s)",
                (title, message[:500], 'workflow', m.now_str())
            )
        # 尝试发送 Webhook
        webhook_url = os.environ.get('WORKFLOW_WEBHOOK_URL', '')
        if webhook_url:
            _send_webhook(webhook_url, title, message)
        return {'success': True, 'title': title, 'message': message[:100]}
    except Exception as e:
        logger.warning(f"Notification send failed: {e}")
        return {'success': False, 'error': str(e), 'title': title}


def _send_webhook(url: str, title: str, message: str) -> dict:
    """发送 Webhook（含 SSRF 防护）"""
    if not url:
        return {'success': False, 'error': _('Webhook URL is empty')}
    # P1-F11: SSRF 防护 — 校验目标地址
    from orchestrator.workflow_engine import _validate_target_url
    try:
        _validate_target_url(url)
    except ValueError as e:
        return {'success': False, 'error': f'SSRF blocked: {e}'}
    body = json.dumps({
        'title': title,
        'message': message,
        'timestamp': m.now_str(),
        'source': 'verorun-orchestrator'
    }).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=10) as resp:
        return {'success': resp.status < 400, 'status_code': resp.status}


def _send_email(email_to: str, title: str, message: str) -> dict:
    """发送邮件（调用 Email 插件服务）"""
    if not email_to:
        return {'success': False, 'error': _('Email_to is empty')}
    try:
        from plugins.email.services import send_email as plugin_send_email
        ok, msg = plugin_send_email(
            to_addr=email_to,
            subject=title,
            body_text=message,
        )
        return {'success': ok, 'message': msg}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ============================================================
# 市场检查节点
# ============================================================

def handle_market_check(node_def: dict, input_data: dict) -> dict:
    """
    市场数据检查节点。
    配置:
      - symbol: '000001.SH' 等
      - metric: 'change_pct' | 'volume' | 'price'
      - operator: '>' | '<' | '>=' | '<='
      - threshold: float
    """
    config = node_def.get('config', {})
    symbol = config.get('symbol', '000001.SH')
    metric = config.get('metric', 'change_pct')
    operator = config.get('operator', '>')
    threshold = config.get('threshold', 0)

    # 尝试从 TradeMind API 获取实时数据
    try:
        market_data = _get_market_data(symbol)
        value = market_data.get(metric, 0)

        operators = {
            '>': lambda a, b: a > b,
            '<': lambda a, b: a < b,
            '>=': lambda a, b: a >= b,
            '<=': lambda a, b: a <= b,
            '==': lambda a, b: a == b,
        }
        triggered = operators.get(operator, lambda a, b: False)(value, threshold)

        return {
            'success': True,
            'symbol': symbol,
            'metric': metric,
            'value': value,
            'threshold': threshold,
            'triggered': triggered,
            'data': market_data
        }
    except Exception as e:
        return {'success': False, 'error': str(e), 'triggered': False}


def _get_market_data(symbol: str) -> dict:
    """获取市场数据（模拟实现，可替换为真实 API）"""
    # Tencent 行情 API
    url = f"https://qt.gtimg.cn/q={symbol}"
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode('gbk')
            # 解析腾讯格式
            import re
            match = re.search(r'~([^~]+)~([^~]+)~([^~]+)~([^~]+)~([^~]+)~([^~]+)~([^~]+)', text)
            if match:
                return {
                    'name': match.group(2),
                    'price': float(match.group(3)),
                    'change_pct': float(match.group(4).replace('%', '')),
                    'volume': int(match.group(6)) if match.group(6).isdigit() else 0
                }
    except Exception:
        pass
    # API 失败时抛出异常，由工作流引擎按 on_error 策略处理
    raise RuntimeError(f"Market data API unavailable for symbol: {symbol}")


# ============================================================
# 审批节点 — Human-in-the-Loop（对齐 Temporal Signal 模式）
# ============================================================

def handle_approval(node_def: dict, input_data: dict) -> dict:
    """
    审批节点处理器。
    配置:
      - approver_role:    'admin'（任意管理员）| 'super_admin' | 'operator'
      - approver_ids:     指定审批人用户 ID 列表（可选，优先于角色）
      - timeout_minutes:  审批超时（0=使用全局 APPROVAL_TIMEOUT_HOURS，默认 72h）
      - message:          审批说明（可选）

    通过抛出 ApprovalRequiredError 使节点进入 waiting_approval，
    工作流实例暂停，等待管理员在实例详情中通过/拒绝。
    """
    cfg = node_def.get('config', {})
    role = cfg.get('approver_role', 'admin')
    if role not in ('admin', 'super_admin', 'operator'):
        raise ValueError(f'Invalid approver_role: {role}')

    # 消息内嵌 approval_required 关键字，兼容引擎既有字符串检测路径
    raise ApprovalRequiredError(
        f'approval_required (role={role}, timeout_minutes={cfg.get("timeout_minutes", 0)}, '
        f'approver_ids={cfg.get("approver_ids", [])}, message={cfg.get("message", "")})'
    )


# ============================================================
# 子工作流节点 — 对齐 Airflow TriggerDagRunOperator / Temporal ChildWorkflow
# ============================================================

def make_sub_workflow_handler(engine):
    """创建子工作流节点处理器（闭包捕获 engine 引用）"""
    def handle_sub_workflow(node_def: dict, input_data: dict) -> dict:
        cfg = node_def.get('config', {})
        child_id = int(cfg.get('workflow_id') or 0)
        if child_id <= 0:
            raise ValueError('子工作流节点：必须配置 workflow_id')

        context = input_data.get('context', {}) or {}
        chain = list(context.get('__subflow_chain__', []))

        # 递归防护：防 A→B→A 环
        if child_id in chain:
            raise ValueError(f'子工作流递归检测失败：工作流 #{child_id} 已在执行链 {chain} 中')
        # 深度限制
        if len(chain) >= 5:
            raise ValueError('子工作流深度超限（最多嵌套 5 层）')

        child_wf = m.get_workflow(child_id)
        if not child_wf or not child_wf.get('is_active'):
            raise ValueError(f'子工作流 #{child_id} 不存在或已停用')

        # 传入子流程的初始上下文（含递归链），子流程可继续嵌套
        ctx = dict(context)
        ctx['__subflow_chain__'] = chain + [child_id]

        child_inst_id = engine.run_workflow(
            child_id,
            trigger_type='sub_workflow',
            trigger_config={'parent_instance_id': input_data.get('_instance_id', 0)},
            initial_context=ctx
        )

        # 轮询子实例直至终结（有界等待：子实例自身 timeout_minutes + 5 分钟兜底）
        timeout_sec = int(child_wf.get('timeout_minutes', 60)) * 60 + 300
        deadline = time.time() + timeout_sec
        inst = None
        status = 'pending'
        while time.time() < deadline:
            inst = m.get_workflow_instance(child_inst_id)
            status = inst['status'] if inst else 'unknown'
            if status in ('completed', 'failed', 'cancelled', 'timeout'):
                break
            time.sleep(2)
        else:
            raise RuntimeError(f'等待子工作流 #{child_id} 执行超时')

        if status != 'completed':
            err = (inst or {}).get('error_message', '') or status
            raise RuntimeError(f'子工作流 #{child_id} 执行失败：{err}')

        return {
            'success': True,
            'child_instance_id': child_inst_id,
            'child_status': status,
            'child_output': m.from_json((inst or {}).get('context_data', '{}'))
        }
    return handle_sub_workflow


# ============================================================
# 脚本节点 — 对齐 Airflow BashOperator / Temporal Activity
# ============================================================

def run_script_safely(script_path: str, script_args: list = None,
                      timeout: int = 300) -> dict:
    """安全执行 scripts/ 目录下的 Python 脚本（路径白名单 + 超时），供 Cron/DAG 共用。"""
    import subprocess
    SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts')
    real_script = os.path.realpath(os.path.join(SCRIPTS_DIR, os.path.basename(script_path)))
    if not real_script.startswith(os.path.realpath(SCRIPTS_DIR)):
        return {'success': False, 'error': f'脚本路径被拒绝：{script_path}'}
    if not os.path.isfile(real_script):
        return {'success': False, 'error': f'脚本不存在：{script_path}'}
    try:
        result = subprocess.run(
            [sys.executable, real_script] + list(script_args or []),
            capture_output=True, text=True, timeout=timeout
        )
        return {
            'success': result.returncode == 0,
            'stdout': result.stdout[-2000:],
            'stderr': result.stderr[-1000:],
            'returncode': result.returncode
        }
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': f'脚本执行超时（{timeout}s）'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def _run_builtin_script(func, ctx: dict, cfg: dict, timeout: int = 120) -> dict:
    """在守护线程中执行内置脚本，超时抛错。

    说明：内置脚本在进程内执行，无法强制 kill 挂起的线程，
    因此内置脚本必须保证在 timeout 内自行返回（纯计算/快速 IO）。
    """
    import threading
    box = {}
    def target():
        try:
            box['result'] = func(ctx, cfg)
        except Exception as e:
            box['error'] = str(e)
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RuntimeError(f'内置脚本执行超时（{timeout}s）')
    if 'error' in box:
        raise RuntimeError(box['error'])
    return box['result']


def _script_check_new_posts(ctx: dict, cfg: dict) -> dict:
    """检查最近发布的新文章（模板 '定时全站静态生成' 使用）"""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'auth-center'))
        from models.cms import get_posts
        limit = int(cfg.get('limit', 50))
        posts = get_posts(published_only=True, limit=limit) or []
        return {
            'success': True,
            'new_count': len(posts),
            'slugs': [p.get('slug', '') for p in posts]
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def _script_generate_static_incremental(ctx: dict, cfg: dict) -> dict:
    """增量生成全站静态页（复用 main_site.staticgen.generate_all）"""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main_site'))
        from staticgen import generate_all
        results = generate_all() or []
        ok = sum(1 for r in results if r.get('ok'))
        fail = sum(1 for r in results if not r.get('ok'))
        return {'success': fail == 0, 'ok': ok, 'fail': fail}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# 内置脚本注册表（白名单，禁止任意路径执行）
BUILTIN_SCRIPTS = {
    'check_new_posts': _script_check_new_posts,
    'generate_static_incremental': _script_generate_static_incremental,
}


def handle_script(node_def: dict, input_data: dict) -> dict:
    """
    脚本节点处理器。
    配置:
      - script:          内置脚本名（lang=builtin）或 scripts/ 目录下的文件名
      - lang:            'builtin' | 'python' | 'shell'
      - args:            参数列表（subprocess 路径）
      - timeout_seconds: 超时秒数（默认 120s）
    """
    cfg = node_def.get('config', {})
    name = cfg.get('script', '')
    lang = cfg.get('lang', 'builtin')
    timeout = int(cfg.get('timeout_seconds') or 120)

    if not name:
        raise ValueError('脚本节点：必须配置 script 名称')

    if lang == 'builtin':
        if name not in BUILTIN_SCRIPTS:
            raise ValueError(f'未知的内置脚本：{name}')
        return _run_builtin_script(BUILTIN_SCRIPTS[name],
                                   input_data.get('context', {}) or {}, cfg, timeout)

    if lang in ('python', 'shell'):
        res = run_script_safely(name, cfg.get('args', []), timeout)
        if not res.get('success'):
            raise RuntimeError(res.get('error', '脚本执行失败'))
        return res

    raise ValueError(f'不支持的脚本语言：{lang}')


# ============================================================
# 节点处理器注册表
# ============================================================

NODE_HANDLERS = {
    'ai_agent': handle_ai_agent,
    'data_collect': handle_data_collect,
    'ai_process': handle_ai_process,
    'condition': handle_condition,
    'publish': handle_publish,
    'notify': handle_notify,
    'market_check': handle_market_check,
    # wait / http_request 由 WorkflowEngine 内置处理
    'approval': handle_approval,
    'sub_workflow': None,  # 需 engine 引用，在 register_all 中工厂注册
    'script': handle_script,
}


def register_all(engine):
    """将所有节点处理器注册到工作流引擎"""
    for node_type, handler in NODE_HANDLERS.items():
        if handler is None:
            continue
        engine.register_node_handler(node_type, handler)
    # 子工作流处理器需要 engine 引用（闭包），单独工厂注册
    engine.register_node_handler('sub_workflow', make_sub_workflow_handler(engine))
