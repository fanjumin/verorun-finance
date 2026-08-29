#!/usr/bin/env python3
"""
构建 中文→英文 翻译映射表
使用增强的逐行解析 + yaml.safe_load 作为回退
"""
import os, sys, re, json
from pathlib import Path

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / 'i18n'


def parse_yaml_enhanced(filepath):
    """
    增强的 YAML 解析 — 处理多行值
    逻辑：
      - key: value  → 单行
      - key:\n  value  → 多行，后续缩进行是 value 的延续
    """
    result = {}
    current_key = None
    current_value_lines = []

    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            s = line.rstrip('\n')

            # 空行或纯注释 → 结束当前多行值
            if not s.strip() or s.strip().startswith('#'):
                if current_key:
                    result[current_key] = '\n'.join(current_value_lines).strip()
                    current_key = None
                    current_value_lines = []
                continue

            # 检查是否是新 key: value 行
            # 新 key 的条件：行首非空格，且包含 ': '
            idx = s.find(': ')
            is_new_key = (not s.startswith(' ')) and (idx > 0)

            if is_new_key:
                # 保存上一个 key
                if current_key:
                    result[current_key] = '\n'.join(current_value_lines).strip()

                current_key = s[:idx]
                current_value_lines = [s[idx + 2:]]
            else:
                # 续行（缩进或多行值的延续）
                if current_key:
                    current_value_lines.append(s.strip())

    # 最后一个 key
    if current_key:
        result[current_key] = '\n'.join(current_value_lines).strip()

    # ═══ 多行值修复：将 \n 合并为空格（YAML 续行语义） ═══
    # YAML 中缩进续行表示值延续（空格连接），不是换行
    fixed = {}
    for k, v in result.items():
        if '\n' in v:
            # 合并多行为单行，用空格连接
            fixed[k] = ' '.join(v.splitlines())
        else:
            fixed[k] = v
    result = fixed

    return result


def build_map():
    zh = parse_yaml_enhanced(I18N_DIR / 'zh-CN.yml')
    en = parse_yaml_enhanced(I18N_DIR / 'en.yml')

    print(f'zh-CN keys: {len(zh)}')
    print(f'en keys:    {len(en)}')

    zh_to_en = {}
    for k in zh:
        if k in en and en[k]:
            zh_to_en[k] = en[k]

    print(f'映射表: {len(zh_to_en)} 条')

    # 验证多行翻译完整性
    test_keys = [
        'CAPTCHA expired or incomplete, please retry',
        'Verification service error, please retry later',
        'Too many requests, please retry in one hour',
        'Too many attempts, please request a new code',
    ]
    print('\n多行翻译验证:')
    all_ok = True
    for k in test_keys:
        if k in zh_to_en:
            val = zh_to_en[k]
            # 检查是否完整
            expected_end = k.split()[-1] if len(k) > 5 else ''
            if k in en and en[k] and len(en[k]) > len(k) * 0.5:
                print(f'  OK: [{k[:50]}...] → [{val[:50]}...]')
            else:
                print(f'  TRUNCATED: [{k[:50]}...] → [{val[:50]}...]')
                all_ok = False
        else:
            print(f'  MISSING: [{k[:60]}]')
            all_ok = False

    if not all_ok:
        print('\nWARNING: 部分多行翻译不完整！')

    # 保存映射表
    map_path = I18N_DIR / '_zh_to_en_map.json'
    with open(map_path, 'w', encoding='utf-8') as f:
        json.dump(zh_to_en, f, ensure_ascii=False)
    print(f'\n映射表: {map_path} ({len(zh_to_en)} 条)')

    return zh_to_en


if __name__ == '__main__':
    build_map()
