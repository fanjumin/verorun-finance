#!/usr/bin/env python3
"""Enterprise Verification Plugin — OCR 识别 + AI 自动审核"""
import os
import json
import re
from plugin_manager.logger import get_plugin_logger
from .plugin_i18n import t

logger = get_plugin_logger('enterprise_verify')

# 与 plugin.json agents 声明一致的 model_policy（§3）
# 解析优先级：explicit → 策略内 provider+model；tier → system_config model_tier_{tier}；
#             inherit/fallback → 全局默认（§3.4）。
_OCR_MODEL_POLICY = {
    'strategy': 'explicit',
    'provider': 'siliconflow',
    'model': 'deepseek-ai/DeepSeek-OCR',
    'allow_user_override': True,
    'fallback': 'inherit',
}
_AUDIT_MODEL_POLICY = {
    'strategy': 'explicit',
    'provider': 'siliconflow',
    'model': 'deepseek-ai/DeepSeek-V3',
    'allow_user_override': True,
    'fallback': 'inherit',
}

_PROMPT_CACHE = {}


def _load_prompt(filename: str) -> str:
    """读取 agents/ 下的提示词文件（带缓存）"""
    if filename in _PROMPT_CACHE:
        return _PROMPT_CACHE[filename]
    path = os.path.join(os.path.dirname(__file__), 'agents', filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            _PROMPT_CACHE[filename] = f.read().strip()
    except OSError as e:
        logger.error(f'Failed to load prompt file {path}: {e}')
        _PROMPT_CACHE[filename] = ''
    return _PROMPT_CACHE[filename]


def _resolve_model_args(model_policy: dict) -> dict:
    """按 model_policy 三层策略解析模型参数（§3.4，委托 agent_matrix.model_resolver 统一实现）。"""
    from agent_matrix.model_resolver import resolve_model_args
    return resolve_model_args(model_policy)


def _get_config(key: str, default=''):
    """优先 PluginManager，回退到主系统 system_config 表"""
    try:
        import flask
        pm = flask.current_app.extensions.get('plugin_manager')
        if pm:
            cfg = pm.get_config('enterprise_verify') or {}
            if key in cfg:
                val = cfg[key]
                return str(val) if not isinstance(val, str) else val
    except Exception:
        pass
    # 回退旧方法
    from models import get_db
    with get_db() as conn:
        row = conn.execute("SELECT value FROM system_config WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default


def _get_siliconflow_api_key() -> str:
    """获取硅基流动 API Key"""
    key = _get_config('siliconflow_api_key', '')
    if not key:
        key = os.environ.get('SILICONFLOW_API_KEY', '')
    return key


def ocr_business_license(image_base64: str) -> dict:
    """
    调用硅基流动 DeepSeek-OCR 识别营业执照
    返回: {company_name, reg_num, address, legal_person, registered_capital, business_scope}
    """
    api_key = _get_siliconflow_api_key()
    if not api_key:
        raise RuntimeError(t('SiliconFlow API Key not configured (system_config.siliconflow_api_key)'))

    if ',' in image_base64:
        image_base64 = image_base64.split(',', 1)[1]

    from agent_matrix.engine import get_gateway
    gw = get_gateway()
    kwargs = _resolve_model_args(_OCR_MODEL_POLICY)
    prompt = _load_prompt('ocr_prompt.md')
    resp = gw.chat(
        messages=[{
            'role': 'user',
            'content': [
                {
                    'type': 'image_url',
                    'image_url': {
                        'url': f'data:image/jpeg;base64,{image_base64}',
                        'detail': 'high',
                    }
                },
                {
                    'type': 'text',
                    'text': prompt,
                }
            ],
        }],
        temperature=0.1,
        max_tokens=2048,
        module='enterprise_verify',
        **kwargs,
    )

    text = resp.choices[0].message.content.strip()
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if json_match:
        result = json.loads(json_match.group())
    else:
        result = json.loads(text)

    fields = ['company_name', 'reg_num', 'legal_person', 'address', 'registered_capital', 'business_scope']
    for f in fields:
        result.setdefault(f, '')

    return result


def _validate_tax_id(tax_id: str) -> bool:
    """简单校验统一社会信用代码格式（18位字母数字）"""
    if not tax_id:
        return False
    tax_id = tax_id.strip().upper()
    return bool(re.match(r'^[0-9A-HJ-NPQRTUWXY]{18}$', tax_id))


def auto_audit(company_name: str, tax_id: str) -> dict:
    """
    AI 自动审核企业认证
    返回: {decision: 'approve'|'pending', confidence: float, reason: str}
    """
    if not company_name or not tax_id:
        return {'decision': 'pending', 'confidence': 0.0, 'reason': t('Company name or tax number is empty')}

    if not _validate_tax_id(tax_id):
        return {'decision': 'pending', 'confidence': 0.0, 'reason': t('Incorrect format for Unified Social Credit Code')}

    api_key = _get_siliconflow_api_key()
    if api_key:
        try:
            from agent_matrix.engine import get_gateway
            gw = get_gateway()
            kwargs = _resolve_model_args(_AUDIT_MODEL_POLICY)
            prompt = (_load_prompt('audit_prompt.md')
                      .replace('{company_name}', company_name)
                      .replace('{tax_id}', tax_id))
            resp = gw.chat(
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0.1,
                max_tokens=512,
                response_format={'type': 'json_object'},
                module='enterprise_verify',
                **kwargs,
            )
            text = resp.choices[0].message.content.strip()
            result = json.loads(text)
            confidence = result.get('confidence', 0.0)
            if confidence >= 0.8 and result.get('decision') == 'approve':
                return {'decision': 'approved', 'confidence': confidence, 'reason': result.get('reason', t('AI Review Passed'))}
            return {'decision': 'pending', 'confidence': confidence, 'reason': result.get('reason', t('AI Review Uncertain, Requires Manual Verification'))}
        except Exception as e:
            logger.warning(f'Auto audit failed, falling back to format check: {e}')

    return {'decision': 'approved', 'confidence': 0.85, 'reason': t('Format validated and automatically certified')}
