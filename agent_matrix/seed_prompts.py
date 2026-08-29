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

# (slug, name, prompt_type, domain, tags, task_triggers, file_path, bind_agent_slugs)
# 覆盖当前 11 个角色对应的 prompt；bind_agent_slugs 对应的 Agent 不存在时自动跳过。
# 旧角色（cms/user/automation/health_check/supply_chain）已不存在，其 prompt 不再入池。
PROMPT_SEEDS = [
    ('master-base', 'Master Agent Role Base', 'system', 'orchestration',
     '["task_decomposition","orchestration","master_agent"]', '["composite","decompose"]',
     'master_prompt.md', ['athena']),
    ('content-role', 'Content Agent Role Base', 'system', 'content',
     '["content_creation"]', '[]',
     'sub_content_prompt.md', ['content']),
    ('builder-role', 'Builder Agent Role Base', 'system', 'site_builder',
     '["website_building","site_generation"]', '[]',
     'sub_builder_prompt.md', ['builder']),
    ('finance-role', 'Finance Agent Role Base', 'system', 'finance',
     '["finance","analysis"]', '[]',
     'sub_finance_prompt.md', ['finance']),
    ('ops-role', 'Ops Agent Role Base', 'system', 'ops',
     '["automation","health_monitor","workflow"]', '[]',
     'sub_ops_prompt.md', ['ops']),
    ('chatbot-role', 'Chatbot Agent Role Base', 'system', 'service',
     '["chatbot","user_service"]', '[]',
     'sub_chatbot_prompt.md', ['service']),
    ('business-role', 'Business Agent Role Base', 'system', 'business',
     '["business","planning"]', '[]',
     'sub_business_prompt.md', ['business']),
    ('discuss-planner', 'Discussion Planner Role', 'system', 'general',
     '["discussion_planner","discussion"]', '[]',
     'discuss_planner.md', []),
    ('discuss-reviewer', 'Discussion Reviewer Role', 'system', 'general',
     '["discussion_reviewer","discussion"]', '[]',
     'discuss_reviewer.md', []),
    ('discuss-decider', 'Discussion Decider Role', 'system', 'general',
     '["discussion_decider","discussion"]', '[]',
     'discuss_decider.md', []),
]


def seed_prompts():
    """将 .md 提示词迁移到 agent_prompts 表并建立 default 绑定（幂等）。"""
    sys.path.insert(0, os.path.join(BASE_DIR, '..', 'auth-center', 'models'))
    from database import get_db

    with get_db() as conn:
        inserted = 0
        bound = 0
        for seed in PROMPT_SEEDS:
            slug, name, prompt_type, domain, tags, triggers, fname, bind_slugs = seed
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
