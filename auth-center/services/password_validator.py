#!/usr/bin/env python3
"""Password validation utility — used by both backend API and admin routes.

IAM v2 Standards:
  - Minimum 10 characters, maximum 128 characters
  - At least 3 of 4 categories: uppercase, lowercase, digit, special
  - No common weak passwords
"""

import re
import secrets
import string

PASSWORD_POLICY = {
    'min_length': 10,
    'max_length': 128,
    'require_upper': True,
    'require_lower': True,
    'require_digit': True,
    'require_special': True,
    'min_categories': 3,
}

# Common weak passwords to reject
WEAK_PASSWORDS = {
    '12345678', 'password', 'Password1', 'Password123', 'Admin123!',
    'admin123', 'test1234', 'qwerty123', 'abc12345', 'passw0rd',
    '1234567890', 'qwertyuiop', 'asdfghjkl', 'zxcvbnm',
    'Password1', 'Passw0rd', 'Password10',
    'letmein', 'welcome', 'admin123', 'test1234',
    'abc1234567', '0000000000', '1111111111',
}

SPECIAL_CHARS = set("!@#$%^&*(),.?\":{}|<>_+-=[]~`/;")


def _count_categories(password: str) -> dict:
    """Count how many of the 4 character categories are present.

    Returns dict with keys: has_upper, has_lower, has_digit, has_special.
    """
    return {
        'has_upper': bool(re.search(r'[A-Z]', password)),
        'has_lower': bool(re.search(r'[a-z]', password)),
        'has_digit': bool(re.search(r'[0-9]', password)),
        'has_special': any(c in SPECIAL_CHARS for c in password),
    }


def validate_password(password: str) -> dict:
    """Validate password against IAM v2 standards.

    Returns {'valid': bool, 'errors': [str]}.
    """
    errors = []

    if not password:
        errors.append('密码不能为空')
        return {'valid': False, 'errors': errors}

    # Length checks
    if len(password) < PASSWORD_POLICY['min_length']:
        errors.append(f'密码长度至少{PASSWORD_POLICY["min_length"]}位')

    if len(password) > PASSWORD_POLICY['max_length']:
        errors.append(f'密码长度不能超过{PASSWORD_POLICY["max_length"]}位')

    # Category checks
    cats = _count_categories(password)
    categories_matched = sum(cats.values())
    if categories_matched < PASSWORD_POLICY['min_categories']:
        errors.append(f'密码至少包含{PASSWORD_POLICY["min_categories"]}类字符（大写字母、小写字母、数字、特殊字符）')

    # Individual category requirements (for error specificity)
    if not cats['has_upper'] and categories_matched >= PASSWORD_POLICY['min_categories']:
        pass  # "at least 3 of 4" — upper can be the missing one
    elif not cats['has_lower'] and categories_matched >= PASSWORD_POLICY['min_categories']:
        pass
    elif not cats['has_digit'] and categories_matched >= PASSWORD_POLICY['min_categories']:
        pass
    elif not cats['has_special'] and categories_matched >= PASSWORD_POLICY['min_categories']:
        pass

    # Check against weak passwords (case insensitive)
    if password.lower() in {w.lower() for w in WEAK_PASSWORDS}:
        errors.append('密码过于简单，请更换')

    return {
        'valid': len(errors) == 0,
        'errors': errors,
    }


def get_password_strength(password: str) -> dict:
    """Evaluate password strength on a 0-4 scale.

    Returns {'score': int (0-4), 'label': str, 'categories': int}.
    Score = number of categories matched (upper, lower, digit, special)
            + 1 bonus if length >= 12, capped at 4.
    Labels: 0=极弱, 1=弱, 2=中等, 3=强, 4=极强
    """
    if not password:
        return {'score': 0, 'label': '极弱', 'categories': 0}

    cats = _count_categories(password)
    categories_matched = sum(cats.values())

    # Base score: number of categories matched
    score = categories_matched

    # Bonus point if length >= 12
    if len(password) >= 12:
        score += 1

    # Cap at 4
    score = min(score, 4)

    labels = {0: '极弱', 1: '弱', 2: '中等', 3: '强', 4: '极强'}

    return {
        'score': score,
        'label': labels[score],
        'categories': categories_matched,
    }


def generate_strong_password(length: int = 16) -> str:
    """Generate a cryptographically random password that complies with IAM v2.

    Guarantees:
      - At least one character from each category (upper, lower, digit, special)
      - Total length at least `length` characters
      - Uses secrets module for cryptographic randomness

    Returns the plain text password string.
    """
    # Ensure minimum length meets policy
    if length < PASSWORD_POLICY['min_length']:
        length = PASSWORD_POLICY['min_length']

    # Character pools
    uppers = string.ascii_uppercase
    lowers = string.ascii_lowercase
    digits = string.digits
    specials = "!@#$%^&*(),.?\":{}|<>_+-=[]~`/;"

    # Guarantee at least one from each category
    guaranteed = [
        secrets.choice(uppers),
        secrets.choice(lowers),
        secrets.choice(digits),
        secrets.choice(specials),
    ]

    # Fill remaining length from combined pool
    all_chars = uppers + lowers + digits + specials
    remaining_length = length - len(guaranteed)
    remaining = [secrets.choice(all_chars) for _ in range(remaining_length)]

    # Shuffle to avoid predictable prefix pattern
    combined = guaranteed + remaining
    secrets.SystemRandom().shuffle(combined)

    return ''.join(combined)


def get_password_rules_text() -> str:
    """Return human-readable rules text for UI display."""
    parts = []
    parts.append(f'至少{PASSWORD_POLICY["min_length"]}位，最多{PASSWORD_POLICY["max_length"]}位')
    parts.append(f'至少包含{PASSWORD_POLICY["min_categories"]}类字符（大写字母、小写字母、数字、特殊字符）')
    return '密码要求：' + '、'.join(parts)
