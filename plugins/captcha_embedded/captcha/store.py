"""Redis store for challenge state + IP rate limiting + stats

Falls back to in-memory dict when Redis is unavailable (local dev)."""
import time
from ..config import CAPTCHA_TTL, RATE_LIMIT_TTL, MAX_FAILS

_redis = None
_memory = {}  # in-memory fallback: {key: {field: value, ...}, ...}


def _get_store():
    """Try Redis; fall back to in-memory dict."""
    global _redis
    if _redis is None:
        try:
            import redis
            _redis = redis.Redis.from_url(
                'redis://127.0.0.1:6379/0',
                decode_responses=True,
                socket_connect_timeout=2,
            )
            _redis.ping()  # verify connection
        except Exception:
            _redis = False  # signal: use in-memory
    return _redis


def _is_redis():
    return _get_store() not in (None, False)


# ── Challenge store ──────────────────────────────────────

def save_challenge(token: str, target_x: int, y_position: int,
                   image_id: str, piece_w: int, piece_h: int) -> bool:
    key = f"captcha:{token}"
    data = {
        "target_x": str(target_x),
        "y_position": str(y_position),
        "image_id": image_id,
        "pw": str(piece_w),
        "ph": str(piece_h),
        "used": "0",
        "created_at": str(int(time.time())),
    }
    if _is_redis():
        try:
            _redis.hset(key, mapping=data)
            _redis.expire(key, CAPTCHA_TTL)
            return True
        except Exception:
            return False
    else:
        _memory[key] = data
        _memory[f"{key}:expire"] = time.time() + CAPTCHA_TTL
        return True


def consume_challenge(token: str) -> dict | None:
    key = f"captcha:{token}"
    if _is_redis():
        try:
            data = _redis.hgetall(key)
            if not data or data.get("used", "0") == "1":
                return None
            _redis.hset(key, "used", "1")
            _redis.expire(key, 10)
            return data
        except Exception:
            return None
    else:
        raw = _memory.get(key)
        if not raw or raw.get("used", "0") == "1":
            return None
        if time.time() > _memory.get(f"{key}:expire", 0):
            _memory.pop(key, None)
            _memory.pop(f"{key}:expire", None)
            return None
        raw["used"] = "1"
        return raw


def peek_challenge(token: str) -> dict | None:
    key = f"captcha:{token}"
    if _is_redis():
        try:
            data = _redis.hgetall(key)
            if not data or data.get("used", "0") == "1":
                return None
            return data
        except Exception:
            return None
    else:
        raw = _memory.get(key)
        if not raw or raw.get("used", "0") == "1":
            return None
        if time.time() > _memory.get(f"{key}:expire", 0):
            _memory.pop(key, None)
            _memory.pop(f"{key}:expire", None)
            return None
        return raw


# ── Rate limiter ─────────────────────────────────────────

def check_rate_limit(ip: str) -> dict:
    key = f"rate:{ip}"
    if _is_redis():
        try:
            current = int(_redis.get(key) or 0)
            if current >= MAX_FAILS:
                ttl = _redis.ttl(key)
                return {"allowed": False, "remaining": 0,
                        "reset_after": max(0, ttl)}
            return {"allowed": True, "remaining": MAX_FAILS - current - 1,
                    "reset_after": RATE_LIMIT_TTL}
        except Exception:
            return {"allowed": True, "remaining": MAX_FAILS, "reset_after": 0}
    else:
        entry = _memory.get(key, {})
        now = time.time()
        expire = _memory.get(f"{key}:expire", 0)
        if now > expire:
            _memory[key] = {"count": 0}
            _memory[f"{key}:expire"] = now + RATE_LIMIT_TTL
        count = _memory[key].get("count", 0)
        return {"allowed": count < MAX_FAILS,
                "remaining": max(0, MAX_FAILS - count - 1),
                "reset_after": RATE_LIMIT_TTL}


def record_fail(ip: str):
    key = f"rate:{ip}"
    if _is_redis():
        try:
            _redis.incr(key)
            _redis.expire(key, RATE_LIMIT_TTL)
        except Exception:
            pass
    else:
        now = time.time()
        expire = _memory.get(f"{key}:expire", 0)
        if now > expire:
            _memory[key] = {"count": 0}
            _memory[f"{key}:expire"] = now + RATE_LIMIT_TTL
        _memory[key]["count"] = _memory[key].get("count", 0) + 1


def check_ip_blocked(ip: str) -> bool:
    if _is_redis():
        try:
            return bool(_redis.sismember("blocklist:ips", ip))
        except Exception:
            return False
    return False  # no blocklist in memory mode


# ── Stats ────────────────────────────────────────────────

def record_stat(passed: bool, risk_score: float, ip: str):
    if _is_redis():
        try:
            hour_key = time.strftime("stats:%Y%m%d%H")
            _redis.hincrby(hour_key, "total", 1)
            if passed:
                _redis.hincrby(hour_key, "passed", 1)
            _redis.hincrbyfloat(hour_key, "risk_sum", risk_score)
            _redis.expire(hour_key, 86400)
            if not passed:
                _redis.zincrby("stats:ip_fails", 1, ip)
        except Exception:
            pass
    # in-memory mode: skip stats


def get_stats() -> dict:
    if _is_redis():
        try:
            hour_key = time.strftime("stats:%Y%m%d%H")
            h = _redis.hgetall(hour_key)
            total = int(h.get("total", 0))
            passed = int(h.get("passed", 0))
            risk_sum = float(h.get("risk_sum", 0))
            top_ips = _redis.zrevrange("stats:ip_fails", 0, 9, withscores=True)
            return {
                "total_requests": total,
                "pass_rate": round(passed / max(total, 1), 4),
                "avg_risk": round(risk_sum / max(total, 1), 4),
                "ip_fails": [{"ip": ip, "fails": int(score)} for ip, score in top_ips],
                "last_hour": total,
            }
        except Exception:
            return {"total_requests": 0, "pass_rate": 0, "avg_risk": 0,
                    "ip_fails": [], "last_hour": 0}
    return {"total_requests": 0, "pass_rate": 0, "avg_risk": 0,
            "ip_fails": [], "last_hour": 0}
