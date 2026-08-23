"""HMAC token generation and verification"""
import hashlib, hmac, time, json, base64

from ..config import get_secret_key, HASH_ALGO, CAPTCHA_TTL


def generate_token(target_x: int, y_position: int, image_id: str,
                   piece_w: int, piece_h: int) -> str:
    """Generate an HMAC-signed challenge token.
    
    Payload: {x, y, img, pw, ph, exp}
    Signature: HMAC-SHA256(payload, SECRET_KEY)
    Token: base64(payload).base64(signature)
    """
    secret = get_secret_key()  # 延迟校验：仅实际生成时才要求 CAPTCHA_SECRET_KEY
    payload = {
        "x": target_x,
        "y": y_position,
        "img": image_id,
        "pw": piece_w,
        "ph": piece_h,
        "exp": int(time.time()) + CAPTCHA_TTL,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")

    sig = hmac.new(
        secret.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).hexdigest()[:32]

    return f"{payload_b64}.{sig}"


def verify_token(token: str) -> dict | None:
    """Verify and decode an HMAC token. Returns payload dict or None."""
    try:
        parts = token.rsplit(".", 1)
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        secret = get_secret_key()  # 延迟校验

        # Verify signature
        expected_sig = hmac.new(
            secret.encode(),
            payload_b64.encode(),
            hashlib.sha256
        ).hexdigest()[:32]

        if not hmac.compare_digest(sig, expected_sig):
            return None

        # Decode payload (add padding back)
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_json = base64.urlsafe_b64decode(payload_b64).decode()
        payload = json.loads(payload_json)

        # Check expiry
        if payload.get("exp", 0) < time.time():
            return None

        return payload
    except Exception:
        return None
