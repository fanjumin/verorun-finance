-- Email Plugin — v1.0.0 initial migration
-- =========================================
-- 说明：email_sent 表位于独立 PG schema: email（§9.1 单库多 Schema 架构）。
--       应用前需设置搜索路径：SET search_path TO email;
--       幂等：与 models.init_email_db() 保持一致，重复执行不报错。

CREATE TABLE IF NOT EXISTS email_sent (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_addr       TEXT NOT NULL,
    to_addr         TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body_text       TEXT,
    body_html       TEXT,
    in_reply_to     BIGINT,
    sent_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_sent_from ON email_sent(from_addr);
CREATE INDEX IF NOT EXISTS idx_email_sent_sent_at ON email_sent(sent_at);
