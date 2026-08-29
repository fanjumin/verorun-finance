#!/usr/bin/env python3
"""
i18n AI Translator — 用通义千问 Qwen 批量翻译
用法: python i18n/translate.py
流程:
  1. 读取 zh-CN.yml（中文原文）
  2. 读取 en.yml（已有翻译）
  3. 找出缺失条目 → 分批调用 DashScope Qwen
  4. 写入 en.yml
  5. 同步到 DB
"""
import sys, os, yaml, json, re, requests, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'auth-center'))

I18N_DIR = os.path.dirname(os.path.abspath(__file__))
ZH_FILE = os.path.join(I18N_DIR, 'zh-CN.yml')
EN_FILE = os.path.join(I18N_DIR, 'en.yml')

BATCH_SIZE = 30

def get_dashscope_key():
    try:
        from models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key='dashscope_text_key'"
            ).fetchone()
        if row and row['value']:
            return row['value']
    except Exception:
        pass
    return os.environ.get('DASHSCOPE_API_KEY', '')


def load_yaml(path):
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def yaml_quote(s):
    """安全引用 YAML 值/键（处理含特殊字符的字符串）"""
    if not s:
        return "''"
    if any(c in s for c in (':', '{', '}', '%', '#', '"', "'", '\n', '[', '>')):
        return "'" + s.replace("'", "''") + "'"
    return s


def save_yaml(path, data, header=''):
    with open(path, 'w', encoding='utf-8') as f:
        if header:
            f.write(header + '\n\n')
        for k, v in data.items():
            val = str(v) if v is not None else ''
            f.write(f'{yaml_quote(k)}: {yaml_quote(val)}\n')
    print(f'[i18n] Saved {len(data)} entries to {path}')


def call_qwen(api_key, entries):
    """调用通义千问翻译"""
    prompt_text = 'Translate the following Chinese phrases to natural English. Return ONLY a JSON object with the same keys and their English translations. No markdown, no explanations.\n\n'
    prompt_text += json.dumps({k: v for k, v in entries}, ensure_ascii=False)

    resp = requests.post(
        'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': 'qwen-turbo',
            'messages': [
                {'role': 'system', 'content': 'You are a professional translator. Output ONLY valid JSON. No markdown, no code fences. Just the JSON object.'},
                {'role': 'user', 'content': prompt_text}
            ],
            'temperature': 0.2,
            'max_tokens': 4096,
        },
        timeout=120
    )
    result = resp.json()

    if 'choices' not in result:
        raise RuntimeError(f'API error: {result.get("message", result.get("error", str(result)))}')

    content = result['choices'][0]['message']['content']
    content = re.sub(r'^```(?:json)?\s*', '', content.strip())
    content = re.sub(r'\s*```$', '', content)

    brace_start = content.find('{')
    brace_end = content.rfind('}')
    if brace_start >= 0 and brace_end > brace_start:
        content = content[brace_start:brace_end + 1]

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        print(f'[i18n] WARNING: JSON parse failed, raw: {content[:300]}')
        # fallback: 行级解析
        result = {}
        for line in content.strip().split('\n'):
            if ':' in line:
                k, _, v = line.partition(':')
                result[k.strip().strip('"\'')] = v.strip().strip('"\'').strip(',')
        return result


def main():
    api_key = get_dashscope_key()
    if not api_key:
        print('[i18n] ERROR: DashScope API Key not found!')
        sys.exit(1)

    zh = load_yaml(ZH_FILE)
    en = load_yaml(EN_FILE)

    print(f'[i18n] zh-CN: {len(zh)} entries')
    print(f'[i18n] en:    {len(en)} entries')

    # 找出缺失的条目
    missing = []
    for k, v in zh.items():
        if k not in en or not en[k] or en[k] == k:
            missing.append((k, v))

    if not missing:
        print('[i18n] All translations are up to date!')
        seed_to_db(en)
        return

    print(f'[i18n] Missing translations: {len(missing)}')

    total = len(missing)
    new_en = dict(en)
    success_count = 0

    for i in range(0, total, BATCH_SIZE):
        batch = missing[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (total - 1) // BATCH_SIZE + 1
        print(f'[i18n] Batch {batch_num}/{total_batches} ({len(batch)} entries)...', end=' ', flush=True)

        try:
            result = call_qwen(api_key, batch)
            for k, v in result.items():
                if v and v != k:
                    new_en[k] = v
                    success_count += 1
            print(f'OK ({len(result)} translated)')
        except Exception as e:
            print(f'FAIL: {e}')
            for k, v in batch:
                if k not in new_en or not new_en[k]:
                    new_en[k] = v  # keep original as fallback

        if i + BATCH_SIZE < total:
            time.sleep(1)

    unchanged = sum(1 for k in en if k in new_en and en[k] == new_en[k])
    print(f'[i18n] Unchanged: {unchanged}, New: {success_count}')

    header = '# English Translation File (auto-generated by Qwen)'
    save_yaml(EN_FILE, new_en, header)

    seed_to_db(new_en)


def seed_to_db(translations):
    try:
        from i18n import seed_from_yaml
        c = seed_from_yaml('en')
        print(f'[i18n] Seeded {c} entries to DB (en)')
        c = seed_from_yaml('zh-CN')
        print(f'[i18n] Seeded {c} entries to DB (zh-CN)')
    except Exception as e:
        print(f'[i18n] DB seed skipped: {e}')


if __name__ == '__main__':
    main()
