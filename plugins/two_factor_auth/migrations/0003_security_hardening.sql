-- 0003: 安全加固（第三方审计 F-01 / F-02 / F-06 修复）
-- 由 models.run_migrations_with_lock() 以 advisory lock 串行化执行 + schema_migrations 记录。

-- F-01: setup_token 一次性 init + 绑定生成时 IP/UA（使用处宽松比对，不一致即作废）
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS init_used BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS ip_address VARCHAR(45);
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS user_agent TEXT;

-- F-02: setup/verify 确认码失败锁定（与 challenge_verify / setup_disable 对齐）
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;

-- F-06: challenge 绑定生成时设备（verify 时宽松比对 IP/UA）
ALTER TABLE two_factor_challenges ADD COLUMN IF NOT EXISTS ip_address VARCHAR(45);
ALTER TABLE two_factor_challenges ADD COLUMN IF NOT EXISTS user_agent TEXT;
