"""net_proxy 规则引擎 —— L0~L3 四级优先级链 + target_pattern 匹配。

方案 §7.4 原文：
  | 级 | 判据 | 结果 |
  | L0 | `VR_PROXY_ENABLED=0`（缺省视为开） | 全部 DIRECT（插件级 kill switch） |
  | L1 | 目标解析 IP 命中内网网段（§9.2 清单） | DIRECT（内网流量永不进代理） |
  | L2 | `proxy_rules` priority 升序首个 `enabled=1` 命中 | CHANNEL:<id> / DIRECT / DENY |
  | L3 | `default_policy` | DIRECT（缺省）/ FIRST_AVAILABLE（健康通道按 weight 加权随机） |

pattern 语义（`target_pattern`）：
    `*`              任意
    `*.example.com`  精确匹配自身及全部子域
    `example.com`    仅精确
    `.example.com`   仅子域
`caller` 精确匹配或 `*`。`region`/`profile_tags` 空为不限，非空须命中当前环境（§7.7）。

命中 `CHANNEL:<id>` 但该通道熔断 → 同 `usage_tags` 健康备选优先，其次按 L3 降级；不抛错。
命中 `DENY` → `resolve_proxy()` 返回 `{"action": "DENY", "proxies": {}}`，
`egress_request()` 抛 `PermissionError`。

本模块职责边界：**纯决策逻辑**。DB 读取经 models.py，通道 URL 构造经 channels.py；
本模块不建连接、不碰 Flask。L1 的内网判定复用 egress.py 的 `is_blocked_target()`，
但为避免 egress → rules → egress 的循环依赖，本模块**接受注入的判定函数**，
默认在函数内惰性导入（运行时才解析，模块加载期无环）。

⚠️ 保存校验的约定（用户已批准，语义 A 的防误配落点）：
`profile_tags` 非空时只允许 `cn` / `os` / `any` / `*`，其它值一律 400。
理由：`usage_tags` 装用途标签（llm/search/...），`profile_tags` 装画像标签（cn/os）；
填错列会让规则在语义 A 下**静默不命中且无报错**，必须在保存时拦住。
"""

import fnmatch
import os
import random

from . import channels as ch
from . import region_profile as rp

__all__ = [
    'RuleValidationError',
    'VALID_ACTIONS',
    'DEFAULT_POLICY_DIRECT',
    'DEFAULT_POLICY_FIRST_AVAILABLE',
    'VALID_DEFAULT_POLICIES',
    'is_proxy_enabled',
    'pattern_matches',
    'caller_matches',
    'rule_matches',
    'validate_rule_payload',
    'prepare_create_rule_fields',
    'prepare_update_rule_fields',
    'RuleDecision',
    'resolve',
]

# 规则动作（proxy_rules.action）
#   DIRECT             直连
#   DENY               明确拒绝
#   CHANNEL:<id>       走指定通道
VALID_ACTIONS = ('DIRECT', 'DENY')
_ACTION_CHANNEL_PREFIX = 'CHANNEL:'

# L3 缺省策略
DEFAULT_POLICY_DIRECT = 'DIRECT'
DEFAULT_POLICY_FIRST_AVAILABLE = 'FIRST_AVAILABLE'
VALID_DEFAULT_POLICIES = (DEFAULT_POLICY_DIRECT, DEFAULT_POLICY_FIRST_AVAILABLE)

# 内网阻断判定注入点（避免 egress ←→ rules 循环导入）
_BLOCKED_CHECKER = None

# 允许出现在 profile_tags 里的画像标签（语义 A 防误配；用户已批准）
VALID_PROFILE_TAGS = ('cn', 'os', rp.ANY, '*')


class RuleValidationError(ValueError):
    """规则字段校验失败。routes.py 捕获后转 400。"""


# ═══════════════════════════════════════════════════════════════════════════
# L0：插件级 kill switch
# ═══════════════════════════════════════════════════════════════════════════

def is_proxy_enabled() -> bool:
    """L0 判定：`VR_PROXY_ENABLED=0` 关闭全部代理；**缺省视为开启**（§7.4）。

    取值约定（与 deploy 环境变量风格一致）：
        '0' / 'false' / 'no' / 'off'（忽略大小写与空白） → False
        其它（含未设置）                                  → True
    """
    raw = os.environ.get('VR_PROXY_ENABLED')
    if raw is None:
        return True
    return raw.strip().lower() not in ('0', 'false', 'no', 'off')


# ═══════════════════════════════════════════════════════════════════════════
# pattern 匹配（§7.4 四种语义）
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_target(target) -> str:
    """目标归一化为纯 host（小写、去端口、去 scheme/路径）。"""
    if not target:
        return ''
    host = str(target).strip().lower()
    if '://' in host:
        host = host.split('://', 1)[1]
    # 去 userinfo
    if '@' in host:
        host = host.rsplit('@', 1)[1]
    # 去路径/查询/片段
    for sep in ('/', '?', '#'):
        if sep in host:
            host = host.split(sep, 1)[0]
    # 去端口（IPv6 字面量 [::1]:8080 保守处理）
    if host.startswith('['):
        end = host.find(']')
        if end != -1:
            return host[1:end]
        return host
    if ':' in host:
        host = host.rsplit(':', 1)[0]
    return host


def pattern_matches(pattern, target) -> bool:
    """`target_pattern` 是否命中目标（§7.4 四种语义）。

    | pattern          | 命中对象                         |
    |------------------|----------------------------------|
    | `*`              | 任意                             |
    | `*.example.com`  | `example.com` 自身 及 全部子域   |
    | `example.com`    | 仅 `example.com` 精确            |
    | `.example.com`   | 仅子域（不含 `example.com` 自身）|

    target 允许是完整 URL / host:port / host，内部统一归一化为小写纯 host。
    pattern 大小写不敏感（运维常写成大写域名）。
    """
    if pattern is None:
        return False
    pat = str(pattern).strip().lower()
    if not pat:
        return False
    host = _normalize_target(target)
    if not host:
        return False
    if pat == '*':
        return True

    if pat.startswith('*.'):
        # 精确自身 + 全部子域
        base = pat[2:]
        if not base:
            return False
        return host == base or host.endswith('.' + base)

    if pat.startswith('.'):
        # 仅子域
        base = pat[1:]
        if not base:
            return False
        return host.endswith('.' + base)

    # 精确匹配；同时保留通配符能力（fnmatch），
    # 使 `api.*.com` 这类中间通配也能按直觉工作
    if '*' in pat or '?' in pat or '[' in pat:
        return fnmatch.fnmatchcase(host, pat)
    return host == pat


def caller_matches(pattern, caller) -> bool:
    """`caller` 精确匹配或 `*`（§7.4）。大小写不敏感。"""
    if pattern is None:
        return True  # 未限制
    pat = str(pattern).strip().lower()
    if pat in ('', '*'):
        return True
    return pat == (str(caller or '').strip().lower())


def _region_matches(rule_region) -> bool:
    """规则 region 空为不限；非空须等于当前 profile。"""
    val = (rule_region or '').strip().lower()
    if val in ('', rp.ANY, '*'):
        return True
    return val == rp.current_profile()


def rule_matches(rule, target, caller, usage_tags=None) -> bool:
    """单条规则是否命中（§7.4 全部维度）。

    Args:
        rule: 规则行（dict / PgRow）。
        target: 目标 URL 或 host。
        caller: 调用方标识。
        usage_tags: 调用方声明的用途标签（可选）；规则非空时要求有交集。
    """
    if not rule:
        return False
    if not rule.get('enabled'):
        return False
    if not pattern_matches(rule.get('target_pattern'), target):
        return False
    if not caller_matches(rule.get('caller'), caller):
        return False
    if not _region_matches(rule.get('region')):
        return False
    if not rp.matches_any_profile(rule.get('profile_tags')):
        return False
    rule_tags = ch.parse_tags(rule.get('usage_tags'))
    if rule_tags:
        have = ch.normalize_tags(usage_tags)
        if not have:
            return False
        if not set(rule_tags) & set(have):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# 保存校验（§7.4 + 语义 A 防误配）
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_action(action) -> str:
    """归一化并校验 action。返回大写形态。"""
    raw = str(action or '').strip().upper()
    if not raw:
        raise RuleValidationError('action 不能为空')
    if raw in VALID_ACTIONS:
        return raw
    if raw.startswith(_ACTION_CHANNEL_PREFIX):
        cid = raw[len(_ACTION_CHANNEL_PREFIX):].strip()
        if not cid.isdigit() or int(cid) <= 0:
            raise RuleValidationError(
                'CHANNEL 动作的通道 id 非法：%s（应为正整数）' % cid)
        return '%s%d' % (_ACTION_CHANNEL_PREFIX, int(cid))
    raise RuleValidationError(
        'action 非法：%s（可选 %s 或 CHANNEL:<id>）'
        % (action, '/'.join(VALID_ACTIONS)))


def _check_profile_tags(raw_tags) -> str:
    """校验并序列化 profile_tags。

    **语义 A 防误配（用户已批准的分工）**：非空时每一项都必须是 `cn`/`os`/`any`/`*`。
    用途标签（llm/search/...）属于 `usage_tags`，误填此处会让规则在运行时
    **静默不命中**（region_profile 语义 A），故在保存层直接 400 拦住。
    """
    tags = ch.normalize_tags(raw_tags)
    bad = [t for t in tags if t not in VALID_PROFILE_TAGS]
    if bad:
        raise RuleValidationError(
            'profile_tags 只能是 %s，检测到非法值 %s；'
            '用途标签请填在 usage_tags（llm/search 等）'
            % ('/'.join(VALID_PROFILE_TAGS), ', '.join(bad)))
    return ch.dump_tags(tags)


def validate_rule_payload(payload, require_all=True):
    """校验并归一化规则入参，返回干净 dict（键为 proxy_rules 列名）。

    Raises:
        RuleValidationError: 任一字段非法；routes.py 转 400。
    """
    if not isinstance(payload, dict):
        raise RuleValidationError('请求体应为 JSON 对象')

    out = {}

    # ── priority ──
    if require_all or 'priority' in payload:
        raw = payload.get('priority')
        if raw in (None, ''):
            out['priority'] = 100
        else:
            try:
                prio = int(str(raw).strip())
            except (ValueError, AttributeError):
                raise RuleValidationError('priority 应为整数')
            if prio < 0:
                raise RuleValidationError('priority 不得小于 0')
            out['priority'] = prio

    # ── target_pattern ──
    if require_all or 'target_pattern' in payload:
        pat = str(payload.get('target_pattern') or '').strip()
        if not pat:
            raise RuleValidationError('target_pattern 不能为空')
        if len(pat) > 512:
            raise RuleValidationError('target_pattern 超长（上限 512 字符）')
        out['target_pattern'] = pat

    # ── caller ──
    if require_all or 'caller' in payload:
        out['caller'] = str(payload.get('caller') or '*').strip() or '*'

    # ── region ──
    if require_all or 'region' in payload:
        region = str(payload.get('region') or '').strip().lower()
        if region and region not in rp.VALID_CHANNEL_REGIONS:
            raise RuleValidationError(
                'region 非法：%s（可选空 / %s）'
                % (region, '/'.join(rp.VALID_CHANNEL_REGIONS)))
        out['region'] = region

    # ── profile_tags（防误配）──
    if require_all or 'profile_tags' in payload:
        out['profile_tags'] = _check_profile_tags(payload.get('profile_tags'))

    # ── usage_tags（自由标签，不限集合）──
    if require_all or 'usage_tags' in payload:
        out['usage_tags'] = ch.dump_tags(payload.get('usage_tags'))

    # ── action ──
    if require_all or 'action' in payload:
        out['action'] = _normalize_action(payload.get('action'))

    # ── note ──
    if require_all or 'note' in payload:
        note = str(payload.get('note') or '').strip()
        if len(note) > 500:
            raise RuleValidationError('note 超长（上限 500 字符）')
        out['note'] = note

    # ── enabled ──
    if require_all or 'enabled' in payload:
        raw = payload.get('enabled', True)
        if isinstance(raw, bool):
            out['enabled'] = raw
        elif isinstance(raw, str):
            low = raw.strip().lower()
            if low in ('1', 'true', 'yes', 'on'):
                out['enabled'] = True
            elif low in ('0', 'false', 'no', 'off', ''):
                out['enabled'] = False
            else:
                raise RuleValidationError('enabled 应为布尔值')
        elif isinstance(raw, int) and raw in (0, 1):
            out['enabled'] = bool(raw)
        else:
            raise RuleValidationError('enabled 应为布尔值')

    return out


def prepare_create_rule_fields(payload, valid_channel_ids=None):
    """建规则：校验后补齐必填键。

    Args:
        valid_channel_ids: 若提供，则校验 `CHANNEL:<id>` 指向的通道确实存在
            （避免建出永远无法解析的死规则）。
    """
    fields = validate_rule_payload(payload, require_all=True)
    fields.setdefault('priority', 100)
    fields.setdefault('caller', '*')
    fields.setdefault('region', '')
    fields.setdefault('profile_tags', '[]')
    fields.setdefault('usage_tags', '[]')
    fields.setdefault('note', '')
    fields.setdefault('enabled', True)
    _check_channel_action_target(fields.get('action'), valid_channel_ids)
    return fields


def prepare_update_rule_fields(payload, valid_channel_ids=None):
    """改规则：只返回本次确实要改的键。"""
    fields = validate_rule_payload(payload, require_all=False)
    _check_channel_action_target(fields.get('action'), valid_channel_ids)
    return fields


def _check_channel_action_target(action, valid_channel_ids):
    if not action or not str(action).startswith(_ACTION_CHANNEL_PREFIX):
        return
    if valid_channel_ids is None:
        return
    cid = int(str(action)[len(_ACTION_CHANNEL_PREFIX):])
    if cid not in set(valid_channel_ids):
        raise RuleValidationError('action 指向的通道不存在：%d' % cid)


# ═══════════════════════════════════════════════════════════════════════════
# 决策
# ═══════════════════════════════════════════════════════════════════════════

class RuleDecision(dict):
    """决策结果。dict 子类，便于直接 JSON 序列化。

    键：
        action:  'DIRECT' / 'DENY' / 'CHANNEL:<id>'
        proxies: requests proxies dict（DIRECT / DENY 时为 {}）
        channel: 命中的通道行（DIRECT / DENY 时为 None）
        level:   命中层级（'L0'/'L1'/'L2'/'L3'/'FALLBACK'），便于排障
        reason:  人类可读原因，进审计日志
    """

    def __init__(self, action, proxies=None, channel=None, level='', reason=''):
        super().__init__(
            action=action,
            proxies=proxies or {},
            channel=channel,
            level=level,
            reason=reason,
        )

    @property
    def action(self):
        return self['action']

    @property
    def proxies(self):
        return self['proxies']

    @property
    def channel(self):
        return self['channel']


def _direct(level, reason):
    return RuleDecision('DIRECT', {}, None, level, reason)


def _deny(level, reason):
    return RuleDecision('DENY', {}, None, level, reason)


def _blocked_check(target):
    """L1：目标是否命中内网阻断网段。判定实现由 egress.py 提供。

    惰性解析注入点，避免 egress ←→ rules 循环导入；若 egress 尚未就绪，
    保守返回 False（不阻断），并由 egress.py 在执行前最终校验兜底（§9.2）。
    """
    global _BLOCKED_CHECKER
    checker = _BLOCKED_CHECKER
    if checker is None:
        try:
            from .egress import is_blocked_target as checker  # noqa
        except Exception:
            return False
    try:
        return bool(checker(target))
    except Exception:
        return False


def set_blocked_checker(fn):
    """测试注入用：显式设置 L1 判定函数。"""
    global _BLOCKED_CHECKER
    _BLOCKED_CHECKER = fn


def _weighted_pick(candidates):
    """按 weight 加权随机挑一个；weight<=0 视为 1。候选为空返回 None。"""
    if not candidates:
        return None
    weights = []
    for c in candidates:
        try:
            w = int(c.get('weight') or 1)
        except (TypeError, ValueError):
            w = 1
        weights.append(w if w > 0 else 1)
    total = sum(weights)
    if total <= 0:
        return candidates[0]
    point = random.uniform(0, total)
    acc = 0.0
    for cand, w in zip(candidates, weights):
        acc += w
        if point <= acc:
            return cand
    return candidates[-1]


def _select_channel(channel_id, channels, healthy, usage_tags, default_policy):
    """L2 命中 CHANNEL:<id> 后的通道选择（§7.4）。

    返回 (channel_or_None, note)；note 为人类可读的降级说明。
    全程不抛错。

    1. 指定通道健康 → 用它；
    2. 否则**同 usage_tags 的健康备选优先**（按 weight 加权随机）；
    3. 再否则按 L3 降级（DIRECT 或 FIRST_AVAILABLE）。
    """
    by_id = {c['id']: c for c in channels}
    target_ch = by_id.get(channel_id)
    healthy_ids = {c['id'] for c in healthy}

    if target_ch is not None and channel_id in healthy_ids:
        return target_ch, ''

    # 备选：与指定通道 usage_tags 有交集的健康通道
    if target_ch is not None:
        want = set(ch.parse_tags(target_ch.get('usage_tags')))
        if want:
            same_tag = [c for c in healthy
                        if set(ch.parse_tags(c.get('usage_tags'))) & want]
            if same_tag:
                return _weighted_pick(same_tag), '备用通道（同 usage_tags）'

    # 降级到 L3
    if default_policy == DEFAULT_POLICY_FIRST_AVAILABLE:
        picked = _weighted_pick(healthy)
        if picked is not None:
            return picked, '指定通道不可用，降级到 FIRST_AVAILABLE'
        return None, '指定通道不可用，且无健康通道 → DIRECT'

    return None, '指定通道不可用，default_policy=DIRECT → DIRECT'


def resolve(target, caller, usage_tags=None, *,
            rules=None, channels=None, healthy=None, default_policy=None,
            blocked_checker=None):
    """四级优先级链决策（§7.4 核心）。

    Args:
        target: 目标 URL 或主机名。
        caller: 调用方标识（如 'veroscholar'）。
        usage_tags: 调用方声明的用途标签（可选）。
        rules: 预取的规则列表（省略则从 models 读取）；便于测试注入。
        channels: 预取的通道列表（省略则从 models 读取）。
        healthy: 预取的健康通道（省略则由 models.pick_healthy_channels 计算）。
        default_policy: L3 策略（省略则读插件 config `default_policy`）。
        blocked_checker: L1 判定函数（省略则用注入点 / egress 实现）。

    Returns:
        RuleDecision
    """
    # ── L0：kill switch ──
    if not is_proxy_enabled():
        return _direct('L0', 'VR_PROXY_ENABLED=0，插件级关闭')

    # ── L1：内网目标永不进代理 ──
    checker = blocked_checker or _BLOCKED_CHECKER
    if checker is not None:
        try:
            blocked = bool(checker(target))
        except Exception:
            blocked = False
    else:
        blocked = _blocked_check(target)
    if blocked:
        return _direct('L1', '目标命中内网阻断网段，直连')

    # ── 惰性取数 ──
    if rules is None or channels is None or healthy is None or default_policy is None:
        from . import models as m
        if rules is None:
            rules = [dict(r) for r in m.list_rules(enabled_only=True)]
        if channels is None:
            channels = [dict(c) for c in m.list_channels(enabled_only=True)]
        if healthy is None:
            healthy = [dict(c) for c in m.pick_healthy_channels()]
        if default_policy is None:
            default_policy = _config_default_policy()

    policy = str(default_policy or DEFAULT_POLICY_DIRECT).strip().upper()
    if policy not in VALID_DEFAULT_POLICIES:
        policy = DEFAULT_POLICY_DIRECT

    # ── L2：priority 升序首个命中 ──
    for rule in rules:
        try:
            hit = rule_matches(rule, target, caller, usage_tags)
        except Exception:
            hit = False
        if not hit:
            continue
        action = str(rule.get('action') or '').strip().upper()
        rid = rule.get('id')

        if action == 'DIRECT':
            return _direct('L2', '规则 #%s 命中 → DIRECT' % rid)

        if action == 'DENY':
            return _deny('L2', '规则 #%s 命中 → DENY' % rid)

        if action.startswith(_ACTION_CHANNEL_PREFIX):
            cid_raw = action[len(_ACTION_CHANNEL_PREFIX):]
            if not cid_raw.isdigit():
                continue
            chosen, note = _select_channel(
                int(cid_raw), channels, healthy, usage_tags, policy)
            if chosen is None:
                return _direct(
                    'L2', '规则 #%s → CHANNEL:%s %s' % (rid, cid_raw, note))
            proxies = ch.build_proxies(chosen)
            if not proxies:
                return _direct(
                    'L2', '规则 #%s → CHANNEL:%s 无法构造 proxies（凭据或字段异常）'
                    % (rid, cid_raw))
            reason = '规则 #%s 命中 → CHANNEL:%d' % (rid, chosen['id'])
            if note:
                reason += '（%s）' % note
            return RuleDecision(
                'CHANNEL:%d' % chosen['id'], proxies, chosen, 'L2', reason)

    # ── L3：default_policy ──
    if policy == DEFAULT_POLICY_FIRST_AVAILABLE:
        picked = _weighted_pick(healthy)
        if picked is None:
            return _direct('L3', 'FIRST_AVAILABLE 但无健康通道 → DIRECT')
        proxies = ch.build_proxies(picked)
        if not proxies:
            return _direct('L3', 'FIRST_AVAILABLE 选中通道无法构造 proxies → DIRECT')
        return RuleDecision(
            'CHANNEL:%d' % picked['id'], proxies, picked, 'L3',
            'default_policy=FIRST_AVAILABLE 加权随机选中')

    return _direct('L3', 'default_policy=DIRECT')


def _config_default_policy():
    """读插件 config 的 `default_policy`；不可得时回退 DIRECT。

    经 plugin_manager 读取（与 analytics/__init__.py 同款先例）；
    不在本模块缓存，避免与运行期配置变更脱节。
    """
    try:
        from flask import current_app
    except Exception:
        return DEFAULT_POLICY_DIRECT
    try:
        pm = current_app.extensions.get('plugin_manager')
        if not pm:
            return DEFAULT_POLICY_DIRECT
        cfg = pm.get_config('net_proxy') or {}
        val = str(cfg.get('default_policy') or DEFAULT_POLICY_DIRECT).strip().upper()
        return val if val in VALID_DEFAULT_POLICIES else DEFAULT_POLICY_DIRECT
    except Exception:
        return DEFAULT_POLICY_DIRECT
