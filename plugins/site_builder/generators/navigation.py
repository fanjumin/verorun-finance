#!/usr/bin/env python3
"""Navigation & Footer Generator — Write LLM nav/footer data into unified design_tokens"""

from ..site_settings.models import get_tokens, save_tokens


class NavigationGenerator:
    """Main navigation & footer generator (unified token version)"""

    @staticmethod
    def apply_nav(nav_data: dict, site_key='platform', draft=False):
        """Write main navigation to design_tokens.navigation.items

        nav_data expected fields:
            nav_items: [{"title": "...", "url": "/...", "icon": "", "children": [...]}, ...]
        draft: if True, writes to draft_json instead of production
        """
        from ..site_settings.models import save_draft_tokens, get_draft_tokens, get_tokens, DEFAULT_TOKENS

        items = nav_data.get('nav_items', [])
        formatted = []
        for i, item in enumerate(items):
            formatted.append({
                'id': i + 1,
                'title': item.get('title', ''),
                'url': item.get('url', '/'),
                'icon': item.get('icon', ''),
                'target': item.get('target', '_self'),
                'children': item.get('children', []),
            })

        if draft:
            current = get_draft_tokens(site_key)
            if current is None:
                current = dict(DEFAULT_TOKENS)
        else:
            tokens = get_tokens(site_key)
            current = tokens['token_json']
        current['navigation']['items'] = formatted
        if draft:
            save_draft_tokens(site_key, current)
        else:
            save_tokens(site_key, current, generated_by='ai', prompt_id=None)
        print(f'[SiteBuilder] ✅ Navigation applied via design_tokens: {len(formatted)} items')

    @staticmethod
    def apply_footer(footer_data: dict, site_key='platform', draft=False):
        """Write footer groups to design_tokens.footer.sections

        footer_data expected fields:
            footer_groups: [{"group_name": "...", "links": [{"title": "...", "url": "..."}]}]
        draft: if True, writes to draft_json instead of production
        """
        from ..site_settings.models import save_draft_tokens, get_draft_tokens, get_tokens, DEFAULT_TOKENS

        groups = footer_data.get('footer_groups', [])
        sections = []
        for group in groups:
            sections.append({
                'name': group.get('group_name', ''),
                'links': group.get('links', []),
            })

        if draft:
            current = get_draft_tokens(site_key)
            if current is None:
                current = dict(DEFAULT_TOKENS)
        else:
            tokens = get_tokens(site_key)
            current = tokens['token_json']
        current['footer']['sections'] = sections
        if draft:
            save_draft_tokens(site_key, current)
        else:
            save_tokens(site_key, current, generated_by='ai', prompt_id=None)
        print(f'[SiteBuilder] ✅ Footer applied via design_tokens: {len(sections)} groups')

    @staticmethod
    def apply_footer_articles(documents: list, site_key='platform', draft=False):
        """Write footer legal document links to design_tokens.footer.articles

        documents: [{"id": "privacy_policy", "name": "Privacy Policy"}, ...]
        draft: if True, writes to draft_json instead of production
        """
        from ..site_settings.models import save_draft_tokens, get_draft_tokens, get_tokens, DEFAULT_TOKENS

        articles = []
        for doc in documents:
            slug = doc.get('id', '')
            name = doc.get('name', '')
            if slug and name:
                articles.append({
                    'title': name,
                    'url': f'/page/{slug}',
                })

        if draft:
            current = get_draft_tokens(site_key)
            if current is None:
                current = dict(DEFAULT_TOKENS)
        else:
            tokens = get_tokens(site_key)
            current = tokens['token_json']
        current['footer']['articles'] = articles
        if draft:
            save_draft_tokens(site_key, current)
        else:
            save_tokens(site_key, current, generated_by='ai', prompt_id=None)
        print(f'[SiteBuilder] ✅ Footer articles applied via design_tokens: {len(articles)}')
