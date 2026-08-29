-- VeroRun 2FA plugin — initial schema (idempotent)
-- 注意：本插件不依赖 pgcrypto（未使用其函数），故不创建扩展，避免权限/信任问题。
-- 由 models.run_migration() 按语句拆分执行（分号分隔，忽略字符串内的分号）。

CREATE SCHEMA IF NOT EXISTS two_factor_auth;

SET search_path TO two_factor_auth, public;

CREATE TABLE IF NOT EXISTS user_totp (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    encrypted_secret    TEXT NOT NULL,
    secret_iv           TEXT NOT NULL,
    is_enabled          BOOLEAN DEFAULT FALSE,
    enabled_at         TIMESTAMP,
    recovery_code_hashes TEXT[] DEFAULT '{}',
    failed_attempts     INTEGER DEFAULT 0,
    locked_until        TIMESTAMP,
    last_verified_at    TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_totp_uid ON user_totp(user_id);
CREATE INDEX IF NOT EXISTS idx_user_totp_enabled ON user_totp(is_enabled);

-- 登录挑战令牌（密码/短信/邮箱第一因子通过后，2FA 校验前的中间态）
CREATE TABLE IF NOT EXISTS two_factor_challenges (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    challenge_id    VARCHAR(64) NOT NULL,
    method          VARCHAR(16) DEFAULT 'totp',
    expires_at      TIMESTAMP NOT NULL,
    consumed        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_2fa_challenge ON two_factor_challenges(challenge_id);
CREATE INDEX IF NOT EXISTS idx_2fa_challenge_user ON two_factor_challenges(user_id, consumed);

-- 记录 challenge 对应的登录入口 app_name，最终签发 SSO 会话时沿用，避免写死 platform 造成跨应用错配
ALTER TABLE two_factor_challenges ADD COLUMN IF NOT EXISTS app_name VARCHAR(32) NOT NULL DEFAULT 'platform';

-- 审计日志（安全插件必备）
CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    action      VARCHAR(50) NOT NULL,
    ip_address  VARCHAR(45),
    user_agent  TEXT,
    details     JSONB,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at);

-- 短期 setup_token：门户入口换取一次性短时效令牌，避免长期登录 JWT 明文进 URL（复审 follow-up #1）
CREATE TABLE IF NOT EXISTS setup_tokens (
    token       VARCHAR(64) PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_setup_tokens_user ON setup_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_setup_tokens_expires ON setup_tokens(expires_at);
