#!/usr/bin/env python3
"""Static Site Generator — generate .html from CMS posts for nginx direct serve.

Usage:
    python staticgen.py post <slug>         # generate single post
    python staticgen.py category <slug>     # generate category page
    python staticgen.py all                  # generate all public posts + categories
    python staticgen.py legal <slug>         # generate legal page

Static files written to: STATIC_DIR (default: platform/static_content/)
nginx config: try_files $uri.html ... → falls through to Flask if not found
"""
import os
import sys
import json
import logging

logger = logging.getLogger(__name__)

# ── Config ──
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static_content')
MARKDOWN_AVAILABLE = False
try:
    import mistune
    MARKDOWN_AVAILABLE = True
    _md = mistune.create_markdown(escape=False, hard_wrap=True)
except ImportError:
    try:
        import markdown as _md_lib
        MARKDOWN_AVAILABLE = True

        def _md_convert(text):
            return _md_lib.markdown(text, extensions=['fenced_code', 'tables', 'codehilite'])
        _md = _md_convert
    except ImportError:
        logger.warning("Markdown library not available (install mistune or markdown). "
                       "Markdown posts will be rendered as-is.")


def _settle_app():
    """Get Flask app context for template rendering."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app
    return app.app


def _ensure_dir(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _render_markdown(content: str) -> str:
    """Convert Markdown to HTML if library available."""
    if not MARKDOWN_AVAILABLE:
        return f'<pre style="white-space:pre-wrap">{content}</pre>'
    if isinstance(_md, type(lambda: None)):
        return _md(content)
    return _md(content)


def _get_db():
    from models import get_db
    return get_db()


def generate_post(slug: str) -> dict:
    """Generate static HTML for a single post. Returns {path, ok, error}."""
    app = _settle_app()
    with app.app_context():
        from models.cms import get_post_by_slug, get_categories
        post = get_post_by_slug(slug)
        if not post:
            return {'path': '', 'ok': False, 'error': f'Post not found: {slug}'}

        # Convert MD to HTML if needed
        content = post.get('content', '')
        if post.get('content_format') == 'markdown':
            content = _render_markdown(content)
            post = dict(post)
            post['content'] = content

        # Determine URL path for file output
        cat_slug = _category_slug(post.get('category', ''))
        if not cat_slug:
            return {'path': '', 'ok': False, 'error': f'Unknown category: {post.get("category")}'}

        if post.get('category') == '法律合规':
            url_path = f'legal/{slug}'
        elif post.get('category') == '产品动态':
            url_path = f'insights/{slug}'
        else:
            url_path = f'docs/{cat_slug}/{slug}'

        # Render with template
        from flask import render_template
        html = render_template('docs_detail.html', post=post)

        # Write static file
        file_path = os.path.join(STATIC_DIR, f'{url_path}.html')
        _ensure_dir(file_path)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(html)
        logger.info(f"Generated: {file_path}")
        return {'path': file_path, 'ok': True, 'error': None}


def generate_category(cat_slug: str) -> dict:
    """Generate static HTML for a category listing page."""
    app = _settle_app()
    with app.app_context():
        from models.cms import get_categories, get_posts
        cats = get_categories(active_only=True, audience='public')
        matched = [c for c in cats if c.get('slug') == cat_slug]
        if not matched:
            return {'path': '', 'ok': False, 'error': f'Category not found: {cat_slug}'}
        cat = matched[0]
        posts = get_posts(category=cat['name'], audience='public', limit=50)

        from flask import render_template
        html = render_template('docs_list.html', category=cat, posts=posts, all_categories=cats)

        file_path = os.path.join(STATIC_DIR, f'docs/{cat_slug}/index.html')
        _ensure_dir(file_path)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(html)
        logger.info(f"Generated: {file_path}")
        return {'path': file_path, 'ok': True, 'error': None}


def generate_docs_index() -> dict:
    """Generate static docs index page."""
    app = _settle_app()
    with app.app_context():
        from models.cms import get_categories
        cats = get_categories(active_only=True, audience='public')
        from flask import render_template
        html = render_template('docs_index.html', categories=cats)
        file_path = os.path.join(STATIC_DIR, 'docs/index.html')
        _ensure_dir(file_path)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(html)
        logger.info(f"Generated: {file_path}")
        return {'path': file_path, 'ok': True, 'error': None}


def generate_all() -> list:
    """Generate all public posts and category pages."""
    results = []
    app = _settle_app()
    with app.app_context():
        from models.cms import get_all_posts, get_categories

        # Generate docs index
        r = generate_docs_index()
        results.append(r)

        # Generate category pages
        cats = get_categories(active_only=True, audience='public')
        for cat in cats:
            r = generate_category(cat['slug'])
            results.append(r)

        # Generate individual posts
        posts = get_all_posts(audience='public', status_filter='published', limit=200)
        for post in posts:
            r = generate_post(post['slug'])
            results.append(r)

    ok = sum(1 for r in results if r['ok'])
    fail = sum(1 for r in results if not r['ok'])
    logger.info(f"Static generation complete: {ok} ok, {fail} failed")
    return results


def _category_slug(cat_name: str) -> str:
    """Map category name to slug."""
    mapping = {
        '快速入门': 'getting-started',
        'Agent 开发': 'agent-dev',
        '金融分析': 'finance',
        '最佳实践': 'best-practices',
        '产品动态': 'insights',
        '帮助中心': 'help',
        '法律合规': 'legal',
        'content_factory': 'content-factory',
        '产品更新': 'updates',
        '技术文档': 'tech-docs',
        '市场洞察': 'market-insights',
    }
    return mapping.get(cat_name, '')


# ── CLI ──

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if len(sys.argv) < 2:
        print("Usage: python staticgen.py [post <slug>|category <slug>|legal <slug>|all]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == 'post' and len(sys.argv) >= 3:
        r = generate_post(sys.argv[2])
        print(f"{'OK' if r['ok'] else 'FAIL'}: {r['path'] or r['error']}")
    elif cmd == 'category' and len(sys.argv) >= 3:
        r = generate_category(sys.argv[2])
        print(f"{'OK' if r['ok'] else 'FAIL'}: {r['path'] or r['error']}")
    elif cmd == 'legal' and len(sys.argv) >= 3:
        r = generate_post(sys.argv[2])
        print(f"{'OK' if r['ok'] else 'FAIL'}: {r['path'] or r['error']}")
    elif cmd == 'all':
        results = generate_all()
        ok = sum(1 for r in results if r['ok'])
        fail = sum(1 for r in results if not r['ok'])
        print(f"Done: {ok} ok, {fail} failed")
        for r in results:
            if not r['ok']:
                print(f"  FAIL: {r.get('error', 'unknown')}")
    else:
        print("Unknown command")
        sys.exit(1)
