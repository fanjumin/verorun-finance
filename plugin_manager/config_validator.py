#!/usr/bin/env python3
"""
Plugin Manager — 配置 JSON Schema 校验
========================================
基于 jsonschema 库校验插件配置，支持自定义错误消息。

如果系统未安装 jsonschema，会自动降级为基本类型检查。
"""

from typing import Dict, Any, List, Tuple, Optional

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


def validate_config(
    config: Dict[str, Any],
    schema: Dict[str, Any],
) -> List[str]:
    """校验配置是否符合 JSON Schema

    Args:
        config: 用户提交的配置 dict
        schema: plugin.json 中的 settings_schema（JSON Schema draft-07）

    Returns:
        错误消息列表。空列表 = 校验通过。
    """
    if not schema or not isinstance(schema, dict):
        return []

    errors: List[str] = []

    # ── 有 jsonschema 库 → 使用完整校验 ──
    if HAS_JSONSCHEMA:
        try:
            validator = jsonschema.Draft7Validator(schema)
            for error in validator.iter_errors(config):
                path = ' → '.join(str(p) for p in error.absolute_path) or '(root)'
                errors.append(f'{path}: {error.message}')
        except jsonschema.SchemaError as e:
            errors.append(f'Schema 无效: {e.message}')
        return errors

    # ── 无 jsonschema 库 → 基础校验 ──
    errors = _basic_validate(config, schema)
    return errors


def _basic_validate(
    config: Dict[str, Any],
    schema: Dict[str, Any],
) -> List[str]:
    """无 jsonschema 库时的降级校验"""
    errors: List[str] = []
    props = schema.get('properties', {})
    required = schema.get('required', [])

    # 检查必填字段
    for field in required:
        if field not in config or config[field] is None:
            label = props.get(field, {}).get('title', field)
            errors.append(f'{label} ({field}): 必填字段缺失')

    # 检查字段类型
    for field, value in config.items():
        prop = props.get(field)
        if not prop:
            continue  # schema 中没有定义的字段，接受（宽松模式）
        expected_type = prop.get('type', 'string')
        type_error = _check_type(field, value, expected_type)
        if type_error:
            errors.append(type_error)

    return errors


def _check_type(field: str, value: Any, expected: str) -> Optional[str]:
    """单字段类型检查"""
    if value is None:
        return None

    type_map = {
        'string': str,
        'number': (int, float),
        'integer': int,
        'boolean': bool,
        'array': list,
        'object': dict,
    }

    py_type = type_map.get(expected)
    if py_type and not isinstance(value, py_type):
        return f'{field}: 应为 {expected} 类型，收到 {type(value).__name__}'

    # 针对 number 的特殊处理：整数也可以赋值给 number 字段
    if expected == 'number' and isinstance(value, int):
        return None

    return None


def coerce_config(
    config: Dict[str, Any],
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    """类型强制转换（字符串 → 数字/布尔等）

    例如用户在前端表单输入的都是字符串，
    根据 schema 定义转换为正确类型。
    """
    if not schema or not isinstance(schema, dict):
        return config

    props = schema.get('properties', {})
    result = dict(config)

    for field, value in result.items():
        prop = props.get(field)
        if not prop or not isinstance(value, str):
            continue
        target = prop.get('type', 'string')
        try:
            if target == 'integer':
                result[field] = int(value)
            elif target == 'number':
                result[field] = float(value)
            elif target == 'boolean':
                if value.lower() in ('true', '1', 'yes'):
                    result[field] = True
                elif value.lower() in ('false', '0', 'no'):
                    result[field] = False
        except (ValueError, TypeError):
            pass  # 转换失败则保留原值

    return result
