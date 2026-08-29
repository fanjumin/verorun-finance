#!/usr/bin/env python3
"""
Plugin Manager — 增强依赖解析器
=================================
拓扑排序、全局循环检测、依赖树查询。
"""

from typing import Dict, List, Set, Tuple, Optional
from .exceptions import PluginCircularDependencyError, PluginDependencyError


def build_dependency_graph(
    plugins: Dict[str, List[str]],
) -> Tuple[Dict[str, set], Dict[str, set]]:
    """构建依赖图

    Args:
        plugins: {identifier: [dep1, dep2, ...]}

    Returns:
        (graph, reverse_graph)
        - graph: {node: set(dependents)} — 依赖关系
        - reverse_graph: {node: set(dependents)} — 被依赖关系
    """
    graph: Dict[str, set] = {}
    reverse: Dict[str, set] = {}

    for pid in plugins:
        graph.setdefault(pid, set())
        reverse.setdefault(pid, set())

    for pid, deps in plugins.items():
        for dep in deps:
            graph.setdefault(pid, set()).add(dep)
            reverse.setdefault(dep, set()).add(pid)

    return graph, reverse


def topological_sort(
    plugins: Dict[str, List[str]],
) -> List[str]:
    """拓扑排序（Kahn 算法）

    返回安装/激活顺序（依赖优先）。
    检测到循环依赖时抛出 PluginCircularDependencyError。
    """
    graph, _ = build_dependency_graph(plugins)
    in_degree: Dict[str, int] = {n: 0 for n in graph}

    for node, deps in graph.items():
        in_degree[node] = len(deps)

    queue = [n for n, d in in_degree.items() if d == 0]
    result = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        # 减少所有依赖此节点的节点的入度
        for n in graph:
            if node in graph.get(n, set()):
                in_degree[n] -= 1
                if in_degree[n] == 0:
                    queue.append(n)

    if len(result) != len(graph):
        remaining = set(graph.keys()) - set(result)
        cycle_path = _find_cycle(graph, next(iter(remaining)))
        raise PluginCircularDependencyError(cycle_path)

    return result


def _find_cycle(graph: Dict[str, set], start: str) -> List[str]:
    """DFS 查找从 start 出发的循环路径"""
    visited: Set[str] = set()
    path: List[str] = []
    path_set: Set[str] = set()

    def dfs(node: str) -> bool:
        if node in path_set:
            # 找到循环：截取循环部分
            cycle_start = path.index(node)
            cycle_path = path[cycle_start:] + [node]
            return True
        if node in visited:
            return False
        visited.add(node)
        path.append(node)
        path_set.add(node)

        for neighbor in graph.get(node, set()):
            if dfs(neighbor):
                return True

        path.pop()
        path_set.discard(node)
        return False

    dfs(start)
    # fallback: 如果 DFS 没找到（理论上不会），用两跳检测
    for n in graph.get(start, set()):
        dep_info_n = graph.get(n, set())
        if start in dep_info_n:
            return [start, n, start]
    return [start, '(cycle)']


def get_dependency_tree(
    identifier: str,
    plugins: Dict[str, List[str]],
    max_depth: int = 10,
) -> dict:
    """获取插件的依赖树（递归，含循环保护）"""
    visited: Set[str] = set()

    def _build(node_id: str, depth: int = 0) -> Optional[dict]:
        if depth > max_depth:
            return {'id': node_id, 'error': 'max_depth_exceeded'}
        if node_id in visited:
            return {'id': node_id, 'cycle': True}
        visited.add(node_id)
        deps = plugins.get(node_id, [])
        children = [_build(d, depth + 1) for d in deps]
        return {'id': node_id, 'dependencies': [c for c in children if c is not None]}

    result = _build(identifier)
    return result or {'id': identifier, 'dependencies': []}


def get_dependents_tree(
    identifier: str,
    reverse_plugins: Dict[str, List[str]],
    max_depth: int = 10,
) -> dict:
    """获取被哪些插件依赖（递归）"""
    visited: Set[str] = set()

    def _build(node_id: str, depth: int = 0) -> Optional[dict]:
        if depth > max_depth:
            return {'id': node_id, 'error': 'max_depth_exceeded'}
        if node_id in visited:
            return {'id': node_id, 'cycle': True}
        visited.add(node_id)
        dependents = reverse_plugins.get(node_id, [])
        children = [_build(d, depth + 1) for d in dependents]
        return {'id': node_id, 'depended_by': [c for c in children if c is not None]}

    result = _build(identifier)
    return result or {'id': identifier, 'depended_by': []}
