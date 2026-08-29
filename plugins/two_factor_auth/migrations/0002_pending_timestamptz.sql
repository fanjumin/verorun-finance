-- 0002: 待确认密钥挂 setup_tokens + 时间列统一 TIMESTAMPTZ（时区安全）
-- P0-3: 重绑定中途放弃不降级既有 2FA
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS pending_secret TEXT;
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS pending_iv TEXT;

-- P1-4: 时间比较统一 UTC，避免受 PG 服务器时区影响。
-- B2 修复：仅在列仍为 timestamp without time zone 时才执行类型转换；
-- 已为 timestamptz 时跳过，避免重复执行 "USING ... AT TIME ZONE 'UTC'"
-- 把既有 timestamptz 先转成 naive-UTC 再按服务器本地时区二次解释，导致数据偏移。
DO $$
DECLARE col_type TEXT;
BEGIN
    SELECT data_type INTO col_type FROM information_schema.columns
      WHERE table_schema='two_factor_auth' AND table_name='two_factor_challenges' AND column_name='expires_at';
    IF col_type = 'timestamp without time zone' THEN
        ALTER TABLE two_factor_challenges ALTER COLUMN expires_at TYPE TIMESTAMPTZ USING expires_at AT TIME ZONE 'UTC';
    END IF;

    SELECT data_type INTO col_type FROM information_schema.columns
      WHERE table_schema='two_factor_auth' AND table_name='setup_tokens' AND column_name='expires_at';
    IF col_type = 'timestamp without time zone' THEN
        ALTER TABLE setup_tokens ALTER COLUMN expires_at TYPE TIMESTAMPTZ USING expires_at AT TIME ZONE 'UTC';
    END IF;

    SELECT data_type INTO col_type FROM information_schema.columns
      WHERE table_schema='two_factor_auth' AND table_name='user_totp' AND column_name='locked_until';
    IF col_type = 'timestamp without time zone' THEN
        ALTER TABLE user_totp ALTER COLUMN locked_until TYPE TIMESTAMPTZ USING locked_until AT TIME ZONE 'UTC';
    END IF;

    SELECT data_type INTO col_type FROM information_schema.columns
      WHERE table_schema='two_factor_auth' AND table_name='user_totp' AND column_name='enabled_at';
    IF col_type = 'timestamp without time zone' THEN
        ALTER TABLE user_totp ALTER COLUMN enabled_at TYPE TIMESTAMPTZ USING enabled_at AT TIME ZONE 'UTC';
    END IF;

    SELECT data_type INTO col_type FROM information_schema.columns
      WHERE table_schema='two_factor_auth' AND table_name='user_totp' AND column_name='last_verified_at';
    IF col_type = 'timestamp without time zone' THEN
        ALTER TABLE user_totp ALTER COLUMN last_verified_at TYPE TIMESTAMPTZ USING last_verified_at AT TIME ZONE 'UTC';
    END IF;
END $$;
