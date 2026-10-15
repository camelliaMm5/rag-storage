-- ============================================================
-- 迁移 001：订单收件人字段 + Agent 执行轨迹表
-- 适用：已用旧版 init.sql 建库的环境（幂等，可重复执行）
--   docker exec -i qa_agent_pgsql psql -U user -d agent < migrations/001_add_recipient_agent_traces.sql
-- ============================================================

-- 1) shop.orders.recipient：此前 AI 下单提取了收件人却无处可存，被静默丢弃
ALTER TABLE shop.orders ADD COLUMN IF NOT EXISTS recipient VARCHAR(64);

-- 2) Agent 执行轨迹（可观测性）
CREATE TABLE IF NOT EXISTS customer_service.agent_traces (
    id                BIGSERIAL PRIMARY KEY,
    conversation_id   VARCHAR(36),
    user_id           VARCHAR(128),
    user_input        TEXT,
    route             VARCHAR(32),
    extract           TEXT,
    agent_nodes       JSONB DEFAULT '[]',
    tool_calls        JSONB DEFAULT '[]',
    final_content     TEXT,
    status            VARCHAR(16) DEFAULT 'ok',
    error             TEXT,
    duration_ms       INTEGER,
    llm_calls         INTEGER DEFAULT 0,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traces_conv    ON customer_service.agent_traces(conversation_id);
CREATE INDEX IF NOT EXISTS idx_traces_created ON customer_service.agent_traces(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_traces_status  ON customer_service.agent_traces(status);
