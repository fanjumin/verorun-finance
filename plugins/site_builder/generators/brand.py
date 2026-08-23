#!/usr/bin/env python3
"""Brand/Visual Generator — Write LLM brand data into unified design_tokens"""

from ..site_settings.models import get_tokens, save_tokens


class BrandGenerator:
    """Brand settings generator (unified token version)"""

    @staticmethod
    def apply(brand_data: dict, site_key='platform', draft=False):
        """Write brand data into design_tokens.brand

        Expected brand_data fields:
            site_name, slogan, industry, brand_story, company_name, contact_email
        draft: if True, writes to draft_json instead of production
        """
        from ..site_settings.models import save_draft_tokens

        if draft:
            # Read current draft as base, update, save back to draft_json
            from ..site_settings.models import get_draft_tokens
            current = get_draft_tokens(site_key)
            if current is None:
                from ..site_settings.models import DEFAULT_TOKENS
                current = dict(DEFAULT_TOKENS)
        else:
            tokens = get_tokens(site_key)
            current = tokens['token_json']

        current['brand'].update({
            'site_name': brand_data.get('site_name', ''),
            'slogan': brand_data.get('slogan', '') or brand_data.get('tagline', ''),
            'industry': brand_data.get('industry', ''),
            'brand_story': brand_data.get('brand_story', ''),
            'company_name': brand_data.get('company_name', ''),
            'contact_email': brand_data.get('contact_email', ''),
        })
        current['seo'].update({
            'title': brand_data.get('seo_title', '') or brand_data.get('site_name', ''),
            'description': brand_data.get('seo_desc', '') or brand_data.get('brand_story', '')[:160],
        })
        current['footer'].update({
            'copyright': brand_data.get('copyright_text', '') or brand_data.get('copyright', ''),
            'icp_number': brand_data.get('icp_number', ''),
            'security_number': brand_data.get('security_number', ''),
        })

        if draft:
            save_draft_tokens(site_key, current)
            print(f'[SiteBuilder] ✅ Brand settings saved to draft')
        else:
            save_tokens(site_key, current, generated_by='ai', prompt_id=None)
            print(f'[SiteBuilder] ✅ Brand settings applied via design_tokens')

    @staticmethod
    def apply_colors(colors_data: dict, site_key='platform', draft=False):
        """Write color scheme to design_tokens.colors"""
        from ..site_settings.models import save_draft_tokens

        if draft:
            from ..site_settings.models import get_draft_tokens
            current = get_draft_tokens(site_key)
            if current is None:
                from ..site_settings.models import DEFAULT_TOKENS
                current = dict(DEFAULT_TOKENS)
        else:
            tokens = get_tokens(site_key)
            current = tokens['token_json']

        color_map = {
            'primary': 'primary', 'secondary': 'secondary', 'accent': 'accent',
            'primary_color': 'primary', 'secondary_color': 'secondary', 'accent_color': 'accent',
            'background': 'background', 'text_primary': 'text_primary', 'text_secondary': 'text_secondary',
        }
        for k, v in colors_data.items():
            mapped = color_map.get(k, k)
            if mapped in current['colors']:
                current['colors'][mapped] = v

        if draft:
            save_draft_tokens(site_key, current)
        else:
            save_tokens(site_key, current, generated_by='ai', prompt_id=None)
        print(f'[SiteBuilder] ✅ Colors applied via design_tokens')
