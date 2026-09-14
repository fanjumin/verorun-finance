#!/usr/bin/env python3
"""
Agent Matrix — Prompt 迁移脚本 (Dynamic Prompt System)
=====================================================
将 agent_matrix/prompts/*.md 迁移到 agent_prompts 表（V1 版本），
并为对应的 Agent 建立 default 绑定。

幂等性：
  - ON CONFLICT (slug, version) DO NOTHING，可安全重复执行
  - 绑定：同 (agent_id, prompt_id, binding_type) 已存在则跳过
  - 缺失的 prompt 文件直接跳过，不影响现有行为

迁移内容为 .md 文件原文（逐字节），保证 Agent 行为不变。
"""
import json, os, sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(BASE_DIR, 'prompts')

# (slug, name, prompt_type, domain, tags, task_triggers, file_path, bind_agent_slugs, editions)
# editions: None = 所有版本；['research-desktop'] / ['finance-desktop'] 等 = 仅该版（与 agent_matrix.models.current_edition() 一致）。
# 未列出的版本跳过该 prompt 迁移（科研版只迁移 master-base + 5 领域 prompt，其余版跳过 research prompt）。
# 覆盖当前 11 个角色对应的 prompt；bind_agent_slugs 对应的 Agent 不存在时自动跳过。
# 旧角色（cms/user/automation/health_check/supply_chain）已不存在，其 prompt 不再入池。
PROMPT_SEEDS = [
    ('master-base', 'Master Agent Role Base', 'system', 'orchestration',
     '["task_decomposition","orchestration","master_agent"]', '["composite","decompose"]',
     'master_prompt.md', ['athena'], None),
    ('content-role', 'Content Agent Role Base', 'system', 'content',
     '["content_creation"]', '[]',
     'sub_content_prompt.md', ['content'], ['finance-desktop', 'standard']),
    ('builder-role', 'Builder Agent Role Base', 'system', 'site_builder',
     '["website_building","site_generation"]', '[]',
     'sub_builder_prompt.md', ['builder'], ['finance-desktop', 'standard']),
    ('finance-role', 'Finance Agent Role Base', 'system', 'finance',
     '["finance","analysis"]', '[]',
     'sub_finance_prompt.md', ['finance'], ['finance-desktop', 'standard']),
    ('ops-role', 'Ops Agent Role Base', 'system', 'ops',
     '["automation","health_monitor","workflow"]', '[]',
     'sub_ops_prompt.md', ['ops'], ['finance-desktop', 'standard']),
    ('chatbot-role', 'Chatbot Agent Role Base', 'system', 'service',
     '["chatbot","user_service"]', '[]',
     'sub_chatbot_prompt.md', ['service'], ['finance-desktop', 'standard']),
    ('business-role', 'Business Agent Role Base', 'system', 'business',
     '["business","planning"]', '[]',
     'sub_business_prompt.md', ['business'], ['finance-desktop', 'standard']),
    ('discuss-planner', 'Discussion Planner Role', 'system', 'general',
     '["discussion_planner","discussion"]', '[]',
     'discuss_planner.md', [], None),
    ('discuss-reviewer', 'Discussion Reviewer Role', 'system', 'general',
     '["discussion_reviewer","discussion"]', '[]',
     'discuss_reviewer.md', [], None),
    ('discuss-decider', 'Discussion Decider Role', 'system', 'general',
     '["discussion_decider","discussion"]', '[]',
     'discuss_decider.md', [], None),
    # 科研桌面版（research-desktop）5 个领域 Sub Agent（v2.0 编排：athena + 5，见 deploy/editions/research-desktop.yaml）
    ('veroscholar-role', 'VeroScholar Research Hub Role', 'system', 'orchestration',
     '["literature","research","orchestration","review"]', '[]',
     'sub_veroscholar_prompt.md', ['veroscholar'], ['research-desktop']),
    ('lit-scout-role', 'Literature Scout Role', 'system', 'literature',
     '["literature_search","literature_review","citation"]', '["search"]',
     'sub_lit_scout_prompt.md', ['lit_scout'], ['research-desktop']),
    ('method-architect-role', 'Method Architect Role', 'system', 'methodology',
     '["experiment_design","hypothesis","statistics"]', '[]',
     'sub_method_architect_prompt.md', ['method_architect'], ['research-desktop']),
    ('thesis-smith-role', 'Thesis Smith Role', 'system', 'writing',
     '["paper_writing","abstract","related_work"]', '[]',
     'sub_thesis_smith_prompt.md', ['thesis_smith'], ['research-desktop']),
    ('peer-auditor-role', 'Peer Auditor Role', 'system', 'review',
     '["peer_review","novelty","ethics","audit"]', '[]',
     'sub_peer_auditor_prompt.md', ['peer_auditor'], ['research-desktop']),
]


def _detect_edition():
    """轻量 edition 判定（与 agent_matrix.models.current_edition() 语义一致）。

    不 import agent_matrix.models，避免其模块级数据库副作用在迁移脚本上下文触发异常。
    读取 VR_EDITION → RELEASE_EDITION → DEPLOY_TYPE，归一化：edu/research→research-desktop、pro/finance→finance-desktop、其余→standard。
    """
    e = (os.getenv('VR_EDITION') or os.getenv('RELEASE_EDITION')
         or os.getenv('DEPLOY_TYPE') or '').strip().lower()
    if e in ('edu', 'research', 'research-desktop'):
        return 'research-desktop'
    if e in ('pro', 'finance', 'finance-desktop'):
        return 'finance-desktop'
    return e or 'standard'


def seed_prompts():
    """将 .md 提示词迁移到 agent_prompts 表并建立 default 绑定（幂等）。

    按当前 edition（current_edition()）过滤 PROMPT_SEEDS：
    editions=None 的 prompt 全版本迁移；editions 含当前版才迁移，否则跳过。
    """
    sys.path.insert(0, os.path.join(BASE_DIR, '..', 'auth-center', 'models'))
    from database import get_db
    edition = _detect_edition()

    with get_db() as conn:
        inserted = 0
        bound = 0
        for seed in PROMPT_SEEDS:
            slug, name, prompt_type, domain, tags, triggers, fname, bind_slugs, editions = seed
            if editions is not None and edition not in editions:
                print(f'[SeedPrompts] Skip prompt (edition {edition}): {slug}')
                continue
            fpath = os.path.join(PROMPTS_DIR, fname)
            if not os.path.exists(fpath):
                print(f'[SeedPrompts] Skip missing file: {fname}')
                continue
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()

            row = conn.execute(
                "SELECT id FROM agent_prompts WHERE slug=%s "
                "ORDER BY version DESC LIMIT 1",
                (slug,)
            ).fetchone()
            if row:
                prompt_id = row['id']
            else:
                prompt_id = conn.execute("""
                    INSERT INTO agent_prompts
                    (name, slug, version, content, prompt_type, domain, tags, task_triggers)
                    VALUES (%s,%s,1,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (name, slug, content, prompt_type, domain, tags, triggers)).fetchone()['id']
                inserted += 1
                print(f'[SeedPrompts] Insert prompt: {slug} (v1)')

            # 建立 default 绑定
            for agent_slug in bind_slugs:
                agent = conn.execute(
                    "SELECT id FROM agent_matrix WHERE slug=%s AND is_active=1",
                    (agent_slug,)
                ).fetchone()
                if not agent:
                    continue
                exists = conn.execute("""
                    SELECT 1 FROM agent_prompt_bindings
                    WHERE agent_id=%s AND prompt_id=%s AND binding_type='default'
                """, (agent['id'], prompt_id)).fetchone()
                if not exists:
                    conn.execute("""
                        INSERT INTO agent_prompt_bindings
                        (agent_id, prompt_id, binding_type, condition, priority)
                        VALUES (%s,%s,'default','',0)
                    """, (agent['id'], prompt_id))
                    bound += 1
                    print(f'[SeedPrompts] Bind default: agent={agent_slug} → prompt={slug}')

        conn.commit()
        print(f'[SeedPrompts] Done: {inserted} prompts inserted, {bound} bindings created')


if __name__ == '__main__':
    sys.path.insert(0, BASE_DIR)
    sys.path.insert(0, os.path.join(BASE_DIR, '..', 'auth-center', 'models'))
    from database import get_db
    seed_prompts()
