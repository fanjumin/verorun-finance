#!/usr/bin/env python3
"""Site Builder — Core Engine

Responsibilities:
1. Parse user requirements → structured plan
2. Execute build DAG (brand → theme → nav → pages → documents)
3. Support minimal modification (incremental update of individual blocks)
"""

import os, json, re, logging
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


class SiteBuilderEngine:
    """Site building core engine"""

    def __init__(self, models_module=None):
        self._models = models_module
        self._llm_engine = None
        self._master_agent = None
        self._pm_id = None

    # ── LLM Calls ──────────────────────────────────────

    def _get_master_agent(self):
        """Get Master Agent configuration (cached)"""
        if self._master_agent is not None:
            return self._master_agent
        from agent_matrix import models as m
        agents = m.list_agents(role_type='master', active_only=True)
        if not agents:
            raise RuntimeError('No available Master Agent')
        self._master_agent = agents[0]
        self._pm_id = self._master_agent.get('provider_model_id')
        return self._master_agent

    def _get_ai_engine(self):
        """Get AIEngine instance (cached)"""
        if self._llm_engine is not None:
            return self._llm_engine
        from agent_matrix.engine import UnifiedLLM
        master = self._get_master_agent()
        self._llm_engine = UnifiedLLM(master)
        return self._llm_engine

    def _call_llm(self, system_prompt: str, user_message: str, temperature: float = 0.3, max_tokens: int = 2000) -> str:
        """Call LLM, return raw text"""
        engine = self._get_ai_engine()
        if self._pm_id is None:
            self._get_master_agent()
        return engine.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            provider_model_id=self._pm_id,
            temperature=temperature,
            max_tokens=max_tokens,
            module='site_builder'
        )

    def _call_llm_json(self, system_prompt: str, user_message: str) -> dict:
        """Call LLM and parse JSON response"""
        raw = self._call_llm(system_prompt, user_message, temperature=0.3)
        if not raw or not isinstance(raw, str):
            logger.error(f'LLM returned empty or non-string response: {type(raw)}')
            raise ValueError('LLM returned empty response')
        # Extract JSON
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        # Try markdown code block
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', raw)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        logger.warning(f'Failed to parse LLM JSON: {raw[:200]}')
        raise ValueError('LLM response could not be parsed as JSON')

    # ── Keyword Replacement ────────────────────────────

    def _fill_prompt(self, template: str, **kwargs) -> str:
        """Replace {keywords} in prompt template with actual values"""
        result = template
        for key, val in kwargs.items():
            if isinstance(val, list):
                val = ', '.join(str(v) for v in val)
            result = result.replace('{' + key + '}', str(val))
        return result

    # ── Phase 1: Parse User Requirement ────────────────

    def parse_requirement(self, prompt_template: dict, user_input: str) -> dict:
        """Parse user input, extract structured information

        Returns:
            {
                "brand_name": "...",
                "tagline": "...",
                "core_services": [...],
                "target_audience": "...",
                "style_preference": "...",
                "special_requirements": "..."
            }
        """
        defaults = prompt_template.get('defaults', {})
        prompts = prompt_template.get('prompts', {})
        parse_prompt = prompts.get('parse', '')

        # Use keyword replacement to build final prompt
        filled_prompt = self._fill_prompt(
            parse_prompt,
            行业=defaults.get('industry', 'General'),
            用户输入=user_input,
        )

        try:
            result = self._call_llm_json(filled_prompt, user_input)
            return result
        except Exception as e:
            logger.warning(f'Parse requirement failed, using defaults: {e}')
            return {
                'brand_name': defaults.get('site_name', 'My Website'),
                'tagline': defaults.get('tone', ''),
                'style_preference': defaults.get('style', 'Modern'),
                'target_audience': '',
                'special_requirements': '',
            }

    # ── Phase 2: Generate Plan Preview ─────────────────

    def generate_plan(self, prompt_template: dict, parsed: dict, user_input: str) -> dict:
        """Generate complete site build plan (preview only, no execution)

        Returns:
            {
                "brand": {...},
                "theme": {...},
                "navigation": {...},
                "footer": {...},
                "pages": {"home": [...], "about": [...], ...},
                "documents": [{"slug": "...", "title": "...", "content": "..."}],
                "summary": "Plan summary text"
            }
        """
        defaults = prompt_template.get('defaults', {})
        prompts = prompt_template.get('prompts', {})
        pages = prompt_template.get('pages', [])
        documents = prompt_template.get('documents', [])

        # Extract keyword values
        brand_name = parsed.get('brand_name', 'My Website')
        industry = defaults.get('industry', 'General')
        target_audience = parsed.get('target_audience', defaults.get('target_audience', 'Visitors'))
        style = parsed.get('style_preference', defaults.get('style', 'Modern'))
        page_names = [p['name'] for p in pages]
        doc_names = [d['name'] for d in documents]

        plan = {'summary': ''}

        # Pre-fill prompts for Brand, Navigation, Footer
        brand_prompt = self._fill_prompt(
            prompts.get('brand', ''),
            品牌名称=brand_name,
            行业=industry,
            目标受众=target_audience,
            风格偏好=style,
        )
        nav_prompt = self._fill_prompt(
            prompts.get('navigation', ''),
            品牌名称=brand_name,
            行业=industry,
            目标受众=target_audience,
            页面列表=page_names,
        )
        footer_prompt = self._fill_prompt(
            prompts.get('footer', ''),
            品牌名称=brand_name,
            行业=industry,
            文档列表=doc_names,
        )

        # Run all LLM calls in parallel via ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {}

            # Submit independent LLM calls
            futures['brand'] = executor.submit(self._call_llm_json, brand_prompt, f'Brand name: {brand_name}')
            futures['navigation'] = executor.submit(self._call_llm_json, nav_prompt, f'Pages list: {page_names}')
            futures['footer'] = executor.submit(self._call_llm_json, footer_prompt, f'Documents list: {doc_names}')

            plan['pages'] = {}
            for page in pages:
                page_id = page['id']
                page_name = page['name']
                page_prompt_key = f'page_{page_id}'
                page_prompt = prompts.get(page_prompt_key, '')
                if not page_prompt:
                    plan['pages'][page_id] = {'sections': []}
                    continue
                filled = self._fill_prompt(
                    page_prompt,
                    品牌名称=brand_name,
                    行业=industry,
                    目标受众=target_audience,
                    风格偏好=style,
                )
                futures[f'page_{page_id}'] = executor.submit(
                    self._call_llm_json, filled, f'Generate page: {page_name}')

            plan['documents'] = []
            for doc in documents:
                doc_id = doc['id']
                doc_name = doc['name']
                doc_prompt_key = f'doc_{doc_id}'
                doc_prompt = prompts.get(doc_prompt_key, '')
                if not doc_prompt:
                    continue
                filled = self._fill_prompt(
                    doc_prompt,
                    品牌名称=brand_name,
                    行业=industry,
                )
                futures[f'doc_{doc_id}'] = executor.submit(
                    self._call_llm, filled, f'Generate document: {doc_name}',
                    temperature=0.3, max_tokens=3000
                )

            # Collect results
            for key, future in futures.items():
                try:
                    result = future.result()
                    if key == 'brand':
                        plan['brand'] = result
                    elif key == 'navigation':
                        plan['navigation'] = result
                    elif key == 'footer':
                        plan['footer'] = result
                    elif key.startswith('page_'):
                        page_id = key.replace('page_', '')
                        plan['pages'][page_id] = result
                    elif key.startswith('doc_'):
                        doc_id = key.replace('doc_', '')
                        doc_name = next((d['name'] for d in documents if d['id'] == doc_id), doc_id)
                        plan['documents'].append({
                            'slug': doc_id,
                            'title': doc_name,
                            'content': result,
                        })
                except Exception as e:
                    logger.warning(f'{key} generation failed: {e}')
                    if key == 'brand':
                        plan['brand'] = {'site_name': brand_name, 'tagline': '', 'brand_story': ''}
                    elif key == 'navigation':
                        plan['navigation'] = {'nav_items': []}
                    elif key == 'footer':
                        plan['footer'] = {'footer_groups': []}
                    elif key.startswith('page_'):
                        page_id = key.replace('page_', '')
                        plan['pages'].setdefault(page_id, {'sections': []})
                    # documents with errors are omitted

        # Build summary
        plan['summary'] = self._build_summary(brand_name, pages, plan)
        return plan

    def _build_summary(self, brand_name: str, pages: list, plan: dict) -> str:
        """Build plan summary text"""
        lines = [
            f'Brand: {brand_name}',
            '',
            'Page Structure:',
        ]
        for page in pages:
            page_id = page['id']
            page_data = plan.get('pages', {}).get(page_id, {})
            section_count = len(page_data.get('sections', []))
            lines.append(f'  - {page["name"]} ({section_count} sections)')

        lines.append('')
        lines.append('Legal Documents:')
        for doc in plan.get('documents', []):
            lines.append(f'  - {doc["title"]}')

        lines.append('')
        lines.append('Please confirm the above plan, or tell me what needs to be adjusted.')
        lines.append('Reply "execute" to start building, or describe the changes needed.')
        return '\n'.join(lines)

    # ── Phase 3: Execute Build ─────────────────────────

    def execute_plan(self, plan: dict, prompt_template: dict, draft=False) -> dict:
        """Execute build plan, write to database step by step

        DAG flow:
        1. Brand settings
        2. Theme configuration (depends on brand colors)
        3. Navigation + Footer
        4. Page content (parallel generation per page)
        5. Legal documents

        draft: if True, writes to draft area (is_published=0 / draft_json)
        """
        from .generators.brand import BrandGenerator
        from .generators.navigation import NavigationGenerator
        from .generators.pages import PageGenerator
        from .generators.theme import ThemeGenerator

        results = {}
        pages = prompt_template.get('pages', [])
        documents = prompt_template.get('documents', [])

        # Step 1: Brand settings
        try:
            BrandGenerator.apply(plan.get('brand', {}), draft=draft)
            results['brand'] = 'ok'
        except Exception as e:
            results['brand'] = str(e)
            logger.error(f'Brand apply failed: {e}')

        # Step 2: Theme configuration
        try:
            ThemeGenerator.apply_theme(plan.get('brand', {}), draft=draft)
            results['theme'] = 'ok'
        except Exception as e:
            results['theme'] = str(e)
            logger.error(f'Theme apply failed: {e}')

        # Step 3: Navigation + Footer
        try:
            NavigationGenerator.apply_nav(plan.get('navigation', {}), draft=draft)
            results['navigation'] = 'ok'
        except Exception as e:
            results['navigation'] = str(e)
            logger.error(f'Navigation apply failed: {e}')

        try:
            NavigationGenerator.apply_footer(plan.get('footer', {}), draft=draft)
            results['footer'] = 'ok'
        except Exception as e:
            results['footer'] = str(e)
            logger.error(f'Footer apply failed: {e}')

        try:
            NavigationGenerator.apply_footer_articles(documents, draft=draft)
            results['footer_articles'] = 'ok'
        except Exception as e:
            results['footer_articles'] = str(e)
            logger.error(f'Footer articles apply failed: {e}')

        # Step 4: Page content
        results['pages'] = {}
        for page in pages:
            page_id = page['id']
            page_data = plan.get('pages', {}).get(page_id, {})
            if not page_data:
                continue
            try:
                sections = page_data.get('sections', [])
                if sections:
                    PageGenerator.apply_page_blocks(page_id, sections, draft=draft)
                else:
                    PageGenerator.apply_page_text(page_id, page_data, draft=draft)
                results['pages'][page_id] = 'ok'
            except Exception as e:
                results['pages'][page_id] = str(e)
                logger.error(f'Page {page_id} apply failed: {e}')

        # Step 5: Legal documents
        results['documents'] = {}
        for doc in plan.get('documents', []):
            try:
                PageGenerator.apply_document(
                    doc['slug'],
                    doc['title'],
                    doc['content'],
                    draft=draft
                )
                results['documents'][doc['slug']] = 'ok'
            except Exception as e:
                results['documents'][doc['slug']] = str(e)
                logger.error(f'Document {doc["slug"]} apply failed: {e}')

        # Stats
        total_ok = sum(
            1 for v in results.values()
            if v == 'ok' or (isinstance(v, dict) and all(vv == 'ok' for vv in v.values()))
        )
        results['_summary'] = {
            'total_steps': 5,
            'succeeded': total_ok,
            'pages_count': len(pages),
            'docs_count': len(documents),
            'mode': 'draft' if draft else 'production',
        }
        return results

    # ── Minimal Modification ───────────────────────────

    def modify_block(self, user_message: str, page: str = 'home') -> dict:
        """Minimal edit: analyze user intent, locate specific block, execute modification

        Returns:
            {
                "action": "modify_block" | "add_block" | "delete_block" | "unknown",
                "target": {"block_id": N},
                "changes": {"title": "...", ...},
                "old_value": "...",
                "new_value": "..."
            }
        """
        from .generators.pages import PageGenerator

        # Get current page summary
        page_summary = PageGenerator.get_page_summary(page)
        if not page_summary:
            return {'action': 'unknown', 'error': 'Page has no blocks'}

        # Build modification context
        modify_prompt = f"""You are a website content editor. The current page [{page}] has the following blocks:

{json.dumps(page_summary, ensure_ascii=False, indent=2)}

User request: {user_message}

Determine which block the user wants to modify. Only output the fields that need to be changed.
**Do NOT regenerate the entire page, only output the delta.**

Return JSON:
{{
  "action": "modify_block" | "add_block" | "delete_block" | "reorder" | "unknown",
  "block_id": number (required for modify_block/delete_block),
  "changes": {{"title": "New Title", "content": "New Content"}} (only for modify_block, include only changed fields)
}}

If the user's request cannot be mapped to a specific block, set action to "unknown" and explain why."""

        try:
            result = self._call_llm_json(modify_prompt, user_message)
        except Exception as e:
            return {'action': 'unknown', 'error': str(e)}

        action = result.get('action', 'unknown')

        if action == 'modify_block':
            block_id = result.get('block_id')
            changes = result.get('changes', {})
            if block_id and changes:
                # Get old value（主库 cms_blocks，经内部 API）
                old = None
                try:
                    from .internal_client import get_page_blocks
                    for b in get_page_blocks(page):
                        if b.get('id') == block_id:
                            old = {'title': b.get('title', ''), 'content': b.get('content', '')}
                            break
                except Exception:
                    pass

                success = PageGenerator.modify_block(block_id, changes)
                return {
                    'action': 'modify_block',
                    'block_id': block_id,
                    'changes': changes,
                    'old_value': (old or {}).get('title', ''),
                    'new_value': changes.get('title', ''),
                    'success': success,
                }

        elif action == 'delete_block':
            block_id = result.get('block_id')
            if block_id:
                from .internal_client import delete_block as _delete_block, clear_cache
                _delete_block(block_id)
                clear_cache()
                return {
                    'action': 'delete_block',
                    'block_id': block_id,
                    'success': True,
                }

        return {'action': action, 'error': result.get('reason', 'Could not locate block to modify')}

    # ── Subscription Check ─────────────────────────────

    @staticmethod
    def check_access(user_id: int = None) -> tuple:
        """Check if user has site building access

        Returns:
            (allowed: bool, message: str)
        """
        # AI base is always free, no check needed
        # Build execution is controlled by subscription module
        # Keep open; actual restrictions handled at routes layer
        return True, ''
