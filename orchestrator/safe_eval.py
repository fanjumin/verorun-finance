#!/usr/bin/env python3
"""安全表达式评估器 - 使用 AST 解析限制允许的操作"""
import ast

# P1-F10: 表达式规模限制
MAX_AST_NODES = 200
MAX_AST_DEPTH = 20
MAX_EXPR_LENGTH = 2000
# VR-REL-005: 容器（字符串/列表等）乘法结果上限，防内存放大 DoS
MAX_MULT_RESULT = 100000


def _operand_magnitude(node, local_vars: dict):
    """估算乘法操作数：返回 (是否容器, 数量级)。未知类型保守按上限处理。"""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, (str, bytes, list, dict, set, tuple)):
            return True, max(len(v), 1)
        if isinstance(v, int):
            return False, abs(v)
        return False, 1
    if isinstance(node, ast.Name) and node.id in local_vars:
        v = local_vars[node.id]
        if isinstance(v, (str, bytes, list, dict, set, tuple)):
            return True, max(len(v), 1)
        if isinstance(v, int):
            return False, abs(v)
    return True, MAX_MULT_RESULT

def safe_eval(expression: str, local_vars: dict) -> bool:
    """
    安全地评估布尔表达式
    
    允许的操作：
    - 比较运算符: <, <=, >, >=, ==, !=, in, not in, is, is not
    - 逻辑运算符: and, or, not
    - 算术运算符: +, -, *, /, %, //, **
    - 内置常量: True, False, None
    - 访问 local_vars 中的变量
    - 幂运算指数限制：<= 100
    
    禁止的操作：
    - 函数调用
    - 属性访问（除了基本类型的 __eq__ 等）
    - 下标访问
    - lambda 表达式
    - 导入
    - 大数运算（移位、超大幂次）
    """
    # P1-F10: 表达式长度上限
    if len(expression) > MAX_EXPR_LENGTH:
        raise ValueError(f"Expression too long ({len(expression)} > {MAX_EXPR_LENGTH})")
    
    try:
        # 解析表达式为 AST
        tree = ast.parse(expression, mode='eval')
        
        # P1-F10: 节点数限制
        node_count = 0
        
        # 遍历 AST 节点进行安全检查
        for node in ast.walk(tree):
            node_count += 1
            if node_count > MAX_AST_NODES:
                raise ValueError(f"Expression too complex ({node_count} nodes > {MAX_AST_NODES})")
            
            # 禁止函数调用
            if isinstance(node, ast.Call):
                raise ValueError(f"Function calls are not allowed: {ast.dump(node)}")
            
            # 禁止属性访问（防止 __class__.__base__ 等攻击）
            if isinstance(node, ast.Attribute):
                raise ValueError(f"Attribute access is not allowed: {ast.dump(node)}")
            
            # 禁止下标访问
            if isinstance(node, ast.Subscript):
                raise ValueError(f"Subscript access is not allowed: {ast.dump(node)}")
            
            # 禁止 lambda 表达式
            if isinstance(node, ast.Lambda):
                raise ValueError(f"Lambda expressions are not allowed")
            
            # 禁止导入
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                raise ValueError(f"Imports are not allowed")
            
            # 禁止复杂表达式
            if isinstance(node, (ast.DictComp, ast.SetComp, ast.ListComp, ast.GeneratorExp)):
                raise ValueError(f"Comprehensions are not allowed")
            
            # P1-F10: 禁止移位运算（防止 1<<999999999）
            if isinstance(node, ast.LShift) or isinstance(node, ast.RShift):
                raise ValueError(f"Bit shift operations are not allowed")
            
            # P1-F10: 限制幂运算指数（防止 9**9**9）
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
                raise ValueError(f"Power operations are not allowed")

            # VR-REL-005: 限制 容器 * 整数 乘法，防 'a'*10**8 内存放大
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                l_is_container, l_mag = _operand_magnitude(node.left, local_vars)
                r_is_container, r_mag = _operand_magnitude(node.right, local_vars)
                if l_is_container != r_is_container:
                    container_len = l_mag if l_is_container else r_mag
                    multiplier = r_mag if l_is_container else l_mag
                    if container_len * multiplier > MAX_MULT_RESULT:
                        raise ValueError("Multiplication result too large")
        
        # 检查危险的标识符
        forbidden_names = [
            '__class__', '__base__', '__subclasses__', '__globals__', '__builtins__',
            '__dict__', '__getattr__', '__setattr__', '__reduce__', '__reduce_ex__',
            '__getattribute__', '__bases__', '__mro__', '__init__', 'eval', 'exec',
            'compile', 'open', '__import__', 'getattr', 'setattr', 'hasattr', 'delattr'
        ]
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                raise ValueError(f"Forbidden name: {node.id}")
        
        # P1-F10: AST 深度限制
        _max_depth = 0
        def _get_depth(n, d=0):
            nonlocal _max_depth
            _max_depth = max(_max_depth, d)
            for child in ast.iter_child_nodes(n):
                _get_depth(child, d + 1)
        _get_depth(tree)
        if _max_depth > MAX_AST_DEPTH:
            raise ValueError(f"Expression too deeply nested ({_max_depth} > {MAX_AST_DEPTH})")
        
        # 使用空的 __builtins__ 进行评估
        result = eval(compile(tree, '<string>', 'eval'), {"__builtins__": {}}, local_vars)
        # VR-REL-005: 结果尺寸兜底校验（防御动态变量乘法绕过静态检查）
        if isinstance(result, (str, bytes, list, dict, set, tuple)) and len(result) > MAX_MULT_RESULT:
            raise ValueError("Evaluation result too large")
        return bool(result)
    
    except SyntaxError as e:
        raise ValueError(f"Invalid expression syntax: {e}")
    except Exception as e:
        raise ValueError(f"Expression evaluation error: {e}")
