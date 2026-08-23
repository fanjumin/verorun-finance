#!/usr/bin/env python3
"""Theme / Visual System Generator — Write LLM color schemes into unified design_tokens"""

import os
import json
from ..site_settings.models import get_tokens, save_tokens
from ..site_settings.token_renderer import render_css_variables


class ThemeGenerator:
    """Theme generator (unified token version)"""

    THEMES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'themes')

    @staticmethod
    def apply_theme(theme_data: dict, site_key='platform'):
        """Generate theme colors from brand data, write to design_tokens

        Expected theme_data fields:
            site_name, primary_color, secondary_color, accent_color, font_preference, tone
        """
        primary = theme_data.get('primary_color', '#2563eb')
        secondary = theme_data.get('secondary_color', '#1e40af')
        accent = theme_data.get('accent_color', '#7c3aed')
        font = theme_data.get('font_preference', 'sans-serif')
        site_name = theme_data.get('site_name', '')

        # Infer color scheme from style preference
        tone = theme_data.get('tone', '')
        is_dark = 'dark' in tone.lower()

        tokens = get_tokens(site_key)
        current = tokens['token_json']

        if is_dark:
            current['colors'].update({
                'primary': primary,
                'secondary': secondary,
                'accent': accent,
                'background': '#0f172a',
                'surface': '#1e293b',
                'text_primary': '#f1f5f9',
                'text_secondary': '#94a3b8',
                'border': '#334155',
            })
        else:
            current['colors'].update({
                'primary': primary,
                'secondary': secondary,
                'accent': accent,
                'background': '#ffffff',
                'surface': '#f7fafc',
                'text_primary': '#1a202c',
                'text_secondary': '#718096',
                'border': '#e2e8f0',
            })

        # Font
        font_map = {
            'serif': "'Noto Serif SC', 'Georgia', serif",
            'sans-serif': "'Inter', 'PingFang SC', 'Microsoft YaHei', sans-serif",
            'mono': "'JetBrains Mono', 'Fira Code', monospace",
        }
        heading_font = font_map.get(font, font_map['sans-serif'])
        current['typography'].update({
            'heading_font': heading_font,
            'body_font': heading_font,
        })

        if draft:
            save_draft_tokens(site_key, current)
        else:
            save_tokens(site_key, current, generated_by='ai', prompt_id=None)
        print(f'[SiteBuilder] ✅ Theme applied via design_tokens ({"dark" if is_dark else "light"})')

        # Also write CSS file to themes/ directory (compatibility with legacy theme system)
        try:
            theme_slug = 'ai_generated'
            theme_dir = os.path.join(ThemeGenerator.THEMES_DIR, theme_slug)
            os.makedirs(theme_dir, exist_ok=True)

            css = render_css_variables(current)
            css_path = os.path.join(theme_dir, 'tokens.css')
            with open(css_path, 'w', encoding='utf-8') as f:
                f.write(css)

            theme_config = {
                'name': site_name or 'AI Generated',
                'version': '1.0.0',
                'description': f"AI-generated design tokens for {site_name}",
                'variables': {
                    'preset': 'dark' if is_dark else 'light',
                    'font_scale': 1.0,
                    'border_radius': 8,
                    '--primary-color': primary,
                    '--secondary-color': secondary,
                    '--accent-color': accent,
                    '--primary-light': ThemeGenerator._lighten(primary, 0.2),
                    '--primary-dark': ThemeGenerator._darken(primary, 0.2),
                }
            }
            theme_json_path = os.path.join(theme_dir, 'theme.json')
            with open(theme_json_path, 'w', encoding='utf-8') as f:
                json.dump(theme_config, f, ensure_ascii=False, indent=2)
            print(f'[SiteBuilder] ✅ Theme CSS + theme.json written to {theme_dir}')
        except Exception as e:
            print(f'[SiteBuilder] ⚠️ Theme file write failed (non-critical): {e}')

    @staticmethod
    def _lighten(hex_color: str, factor: float) -> str:
        hex_color = hex_color.lstrip('#')
        if len(hex_color) < 3:
            return '#ffffff'
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        r = min(255, int(r + (255 - r) * factor))
        g = min(255, int(g + (255 - g) * factor))
        b = min(255, int(b + (255 - b) * factor))
        return f"#{r:02x}{g:02x}{b:02x}"

    @staticmethod
    def _darken(hex_color: str, factor: float) -> str:
        hex_color = hex_color.lstrip('#')
        if len(hex_color) < 3:
            return '#000000'
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        r = max(0, int(r * (1 - factor)))
        g = max(0, int(g * (1 - factor)))
        b = max(0, int(b * (1 - factor)))
        return f"#{r:02x}{g:02x}{b:02x}"