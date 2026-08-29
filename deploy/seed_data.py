#!/usr/bin/env python3
"""
VeroRun — Standalone seed data injector.

Usage:
    python3 seed_data.py                      # auto-detect .env in current dir
    python3 seed_data.py --env /path/to/.env  # explicit .env path
    python3 seed_data.py --sqlite /path/to/db # force SQLite mode
"""

import os, sys, hashlib, secrets, json, argparse

# ── Admin credentials ─────────────────────────────────────────────────
# Default super admin username is administrator; if no password is explicitly provided, a random 10-character one is generated.
# Credentials are printed once in the install output; ask the user to save them immediately.
import secrets as _secrets
ADMIN_USERNAME = os.environ.get("VR_ADMIN_USERNAME", "administrator")
# Generate a random 10-character password when none is explicitly provided (printed once in install output; save it immediately)
ADMIN_PASSWORD = os.environ.get("VR_ADMIN_PASSWORD") or _secrets.token_urlsafe(8)
ADMIN_DISPLAY  = "Administrator"

# ── Seed data ─────────────────────────────────────────────────────────

DEFAULT_PLUGIN_PRODUCTS = [
    {"plugin_key": "site_domains",  "name": "Site Domains",  "category": "core",  "price_month_fen": 0,  "price_year_fen": 0,  "sort_order": 1, "is_featured": 0},
]

DEFAULT_QUOTAS = [
    {"target_type": "global", "daily_limit": 100000, "rate_limit": 30},
    {"target_type": "user",   "daily_limit": 20,     "rate_limit": 5},
    {"target_type": "agent",  "daily_limit": 1000,   "rate_limit": 60},
]


# ======================================================================
# Helpers
# ======================================================================

def parse_env(env_path: str) -> dict:
    """Parse a .env file into a dict."""
    config = {}
    if not os.path.exists(env_path):
        print(f"[WARN] .env not found: {env_path}")
        return config
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            config[key.strip()] = val
    return config


def hash_password(password: str, iterations: int = 600000) -> str:
    """PBKDF2-SHA256 hash with random salt, matches auth-center format."""
    salt = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2:sha256:{iterations}:{salt}:{pw_hash}"


# ======================================================================
# Database abstraction
# ======================================================================

class SeedDB:
    """Minimal database wrapper supporting both PostgreSQL and SQLite."""

    def __init__(self, env: dict, sqlite_path: str = None):
        self.env = env
        self.sqlite_path = sqlite_path
        self.conn = None
        self._connect()

    def _connect(self):
        pg_host = self.env.get("PG_HOST", "")
        if pg_host and not self.sqlite_path:
            # PostgreSQL mode
            import psycopg2
            self.conn = psycopg2.connect(
                host=pg_host,
                port=int(self.env.get("PG_PORT", 5432)),
                dbname=self.env.get("PG_DB", "appdb"),
                user=self.env.get("PG_USER", "app"),
                password=self.env.get("PG_PASSWORD", ""),
            )
            self.conn.autocommit = True
            self._db_type = "postgresql"
            self._param_style = "%s"
        else:
            # SQLite mode
            import sqlite3
            _default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "x7k2m9a4.db")
            db_path = self.sqlite_path or self.env.get("DB_PATH") or _default_db
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
            self.conn = sqlite3.connect(db_path)
            self.conn.row_factory = sqlite3.Row
            # 审计 M-7 修复：explicitly enable autocommit (isolation_level=None), aligned with the PG mode
            # autocommit, so already-seeded data is not lost if main() raises an exception mid-way.
            self.conn.isolation_level = None
            self._db_type = "sqlite"
            self._param_style = "?"

    def close(self):
        if self.conn:
            self.conn.close()

    def execute(self, sql: str, params: tuple = None):
        cur = self.conn.cursor()
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return cur

    def insert_on_conflict(self, table: str, data: dict, conflict_col: str = None):
        """Idempotent insert — insert or skip on conflict."""
        cols = ", ".join(data.keys())
        placeholders = ", ".join([self._param_style] * len(data))
        if self._db_type == "postgresql":
            conflict = conflict_col or "id"
            sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO NOTHING"
        else:
            sql = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"
        self.execute(sql, tuple(data.values()))

    def table_exists(self, name: str) -> bool:
        if self._db_type == "postgresql":
            cur = self.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                (name,)
            )
        else:
            cur = self.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,)
            )
        return cur.fetchone() is not None


# ======================================================================
# Seed functions
# ======================================================================

def seed_base_plan(db: SeedDB):
    """Seed the free base plan."""
    db.insert_on_conflict("base_plans", {
        "plan_key": "free",
        "name": "Free",
        "description": "Free entry plan with 20 API calls per day",
        "daily_limit": 20,
    }, conflict_col="plan_key")
    print("  [OK] base_plan: free")


def seed_plugin_products(db: SeedDB):
    """Seed initial plugin product catalog (site_domains only)."""
    for p in DEFAULT_PLUGIN_PRODUCTS:
        db.insert_on_conflict("plugin_products", p, conflict_col="plugin_key")
        print(f"  [OK] plugin_product: {p['plugin_key']}")


def seed_site_domains(db: SeedDB, deploy_domain: str = None, brand: str = None):
    """Seed the 3 default site_domains records (www/agent/platform).

    Aligns with auth-center/models/database.py migration defaults; idempotent
    via ON CONFLICT (full_domain) DO NOTHING.
    """
    deploy_domain = deploy_domain or "localhost"
    brand = brand or "VeroRun 维洛智能"
    defaults = [
        ("www",      f"www.{deploy_domain}",      f"{brand} 官网",      'default', 1, 1),
        ("agent",    f"agent.{deploy_domain}",    f"{brand} 管理后台",  'default', 1, 2),
        ("platform", f"platform.{deploy_domain}", f"{brand} 用户中心",  'default', 1, 3),
    ]
    for sub, full, name, template, pub, sort in defaults:
        db.insert_on_conflict("site_domains", {
            "site_config_id": 1,
            "subdomain": sub,
            "full_domain": full,
            "display_name": name,
            "template": template,
            "is_published": pub,
            "sort_order": sort,
        }, conflict_col="full_domain")
        print(f"  [OK] site_domain: {full}")


def seed_admin_user(db: SeedDB, username: str = None, password: str = None):
    """Create or update the admin user (no phone required)."""
    # 审计 L7：credentials are passed via parameters; no longer mutate module-level variables with global
    username = username or ADMIN_USERNAME
    password = password or ADMIN_PASSWORD
    pw_hash = hash_password(password)

    if db._db_type == "postgresql":
        cur = db.execute(
            "SELECT id FROM users WHERE username = %s",
            (username,)
        )
    else:
        cur = db.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,)
        )

    row = cur.fetchone()
    if row:
        user_id = row[0]
        # 审计 M19：the UPDATE branch no longer sets password_changed_at to NULL.
        # The old logic reset the force-password-change flag on every seed, repeatedly forcing admins to change passwords.
        db.execute(
            "UPDATE users SET username = %s, display_name = %s, password_hash = %s, is_admin = 1, active = 1 WHERE id = %s"
            if db._db_type == "postgresql" else
            "UPDATE users SET username = ?, display_name = ?, password_hash = ?, is_admin = 1, active = 1 WHERE id = ?",
            (username, ADMIN_DISPLAY, pw_hash, user_id)
        )
        print(f"  [OK] admin user updated (id={user_id})")
    else:
        cur = db.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin, active) "
            "VALUES (%s, %s, %s, 1, 1) RETURNING id"
            if db._db_type == "postgresql" else
            "INSERT INTO users (username, display_name, password_hash, is_admin, active) "
            "VALUES (?, ?, ?, 1, 1)",
            (username, ADMIN_DISPLAY, pw_hash)
        )
        if db._db_type == "postgresql":
            user_id = cur.fetchone()[0]
        else:
            user_id = cur.lastrowid
        print(f"  [OK] admin user created (id={user_id})")

    return user_id


def seed_quotas(db: SeedDB):
    """Seed default usage quotas (idempotent: skip if target_type+target_id already exists)."""
    for q in DEFAULT_QUOTAS:
        target_type = q["target_type"]
        target_id = q.get("target_id", 0)
        if db._db_type == "postgresql":
            cur = db.execute(
                "SELECT 1 FROM usage_quotas WHERE target_type = %s AND target_id = %s",
                (target_type, target_id)
            )
        else:
            cur = db.execute(
                "SELECT 1 FROM usage_quotas WHERE target_type = ? AND target_id = ?",
                (target_type, target_id)
            )
        if cur.fetchone():
            print(f"  [SKIP] quota: {target_type} (already exists)")
        else:
            db.insert_on_conflict("usage_quotas", q)
            print(f"  [OK] quota: {target_type}")


def seed_admin_profile(db: SeedDB, user_id: int, username: str = None):
    """Create admin_profiles row for the admin user."""
    # 审计 L7：username is passed via parameter
    username = username or ADMIN_USERNAME
    db.insert_on_conflict("admin_profiles", {
        "user_id": user_id,
        "role": "super_admin",
        "permissions": '["users","content","finance","system","matrix","admins"]',
        "real_name": username,
        "notes": "Initial Super Admin",
    }, conflict_col="user_id")
    print(f"  [OK] admin profile created (user_id={user_id})")


def seed_admin_subscription(db: SeedDB, user_id: int):
    """Create a free subscription for the admin user."""
    db.insert_on_conflict("user_subscriptions", {
        "user_id": user_id,
        "plan_key": "free",
        "status": "active",
        "daily_limit": 20,
    }, conflict_col="user_id")
    print(f"  [OK] admin subscription: free (user_id={user_id})")


def seed_ai_system_config(db: SeedDB):
    """Seed default AI runtime configuration (idempotent; never overwrites admin tweaks).

    - model_tier_standard points to the deepseek/deepseek-v4-flash provider_model id,
      resolved by name (NOT a hardcoded id, which would be fragile across installs).
    - ai_text_provider / ai_text_model / cleaner_ai_model give tier/cleaner pipelines a
      working default when the operator has not explicitly configured one
      (BUG-8 tier resolution / BUG-7 cleaner pipeline regression fix).
    """
    if not db.table_exists("system_config"):
        print("  [SKIP] system_config table not found")
        return
    if not db.table_exists("provider_models") or not db.table_exists("providers"):
        print("  [SKIP] provider_models/providers table not found")
        return

    # Resolve deepseek-v4-flash provider_model id by name (stable across installs).
    if db._db_type == "postgresql":
        cur = db.execute(
            "SELECT pm.id FROM provider_models pm "
            "JOIN providers p ON pm.provider_id = p.id "
            "WHERE p.slug = %s AND pm.model_name = %s",
            ("deepseek", "deepseek-v4-flash")
        )
    else:
        cur = db.execute(
            "SELECT pm.id FROM provider_models pm "
            "JOIN providers p ON pm.provider_id = p.id "
            "WHERE p.slug = ? AND pm.model_name = ?",
            ("deepseek", "deepseek-v4-flash")
        )
    row = cur.fetchone()
    tier_value = str(row[0]) if row else None
    if not row:
        print("  [WARN] deepseek/deepseek-v4-flash provider_model not found; model_tier_standard NOT seeded")

    seeds = {
        "model_tier_standard": tier_value,
        "ai_text_provider": "deepseek",
        "ai_text_model": "deepseek-v4-flash",
        "cleaner_ai_model": "deepseek-v4-flash",
    }
    for key, value in seeds.items():
        if value is None:
            continue
        db.insert_on_conflict("system_config", {
            "key": key,
            "value": value,
        }, conflict_col="key")
        print(f"  [OK] system_config: {key}={value}")


# ======================================================================
# Main
# ======================================================================

def main():
    parser = argparse.ArgumentParser(description="VeroRun seed data injector")
    parser.add_argument("--env", default=None, help="Path to .env file")
    parser.add_argument("--sqlite", default=None, help="Force SQLite mode with explicit path")
    parser.add_argument("--admin-user", default=None, help="Admin username (overrides env var)")
    parser.add_argument("--admin-pass", default=None, help="Admin password (overrides env var)")
    args = parser.parse_args()

    # 审计 L7：credentials are resolved into local variables and passed to seed functions (no more global)
    username = args.admin_user or ADMIN_USERNAME
    password = args.admin_pass or ADMIN_PASSWORD

    # Locate .env
    env_path = args.env
    if not env_path:
        for candidate in [".env", "../.env", os.path.join(os.path.dirname(__file__), "..", ".env")]:
            if os.path.exists(candidate):
                env_path = os.path.abspath(candidate)
                break
    if not env_path:
        env_path = os.path.abspath(".env")

    print(f"[i] Loading config from: {env_path}")
    env = parse_env(env_path)

    db = SeedDB(env, sqlite_path=args.sqlite)

    # Verify required tables exist
    required = ["users", "base_plans", "plugin_products", "usage_quotas", "user_subscriptions", "admin_profiles", "site_domains"]
    missing = [t for t in required if not db.table_exists(t)]
    if missing:
        print(f"[FAIL] Tables not found: {', '.join(missing)}")
        print("       Run database migrations first, or verify .env config.")
        print("       Try: sudo bash deploy/install.sh seed   (re-runs migration + seed)")
        db.close()
        sys.exit(1)

    deploy_domain = env.get("DEPLOY_DOMAIN") or "localhost"
    deploy_brand = env.get("DEPLOY_BRAND") or "VeroRun 维洛智能"

    print("[i] Seeding data...")
    seed_base_plan(db)
    seed_plugin_products(db)
    seed_site_domains(db, deploy_domain, deploy_brand)
    user_id = seed_admin_user(db, username, password)
    seed_admin_profile(db, user_id, username)
    seed_quotas(db)
    seed_admin_subscription(db, user_id)
    seed_ai_system_config(db)

    db.conn.commit()
    db.close()

    print(f"\n[OK] Seed data injected successfully.")
    print(f"     Admin account: {username} / <password saved to install output above>")
    print(f"     Plugins seeded: {len(DEFAULT_PLUGIN_PRODUCTS)}")


if __name__ == "__main__":
    main()
