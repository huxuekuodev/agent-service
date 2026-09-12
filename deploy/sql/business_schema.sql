-- ============================================================================
-- Deer Agent 业务库表结构 v2（与 LangGraph checkpoint 库分离）
--
-- 目标库：agent_business（checkpoint 库只保留 langgraph checkpoint 表）
-- 适用：PostgreSQL 14+（gen_random_uuid() 内置；pg_cron 可选，用于分区/清理定时任务）
-- 特性：幂等建表 / 逻辑删除 / messages 按月 RANGE 分区 / JWT 令牌表 / 全文检索 / 可增量迁移
--
-- 执行：
--   createdb agent_business
--   psql -d agent_business -f deploy/sql/business_schema.sql
--
-- 决策基线：
--   1) messages 为长期真相（checkpoint 可被压缩/清理，消息不丢）；
--   2) 独立业务库；monitor 三表一并迁入；
--   3) 真实用户 + 账号密码 + JWT（access/refresh，refresh 落库可撤销）；
--   4) 全部实体逻辑删除；会话绑定 checkpoint_thread_id，清理时同步删 checkpoint；
--   5) messages 按月分区；session_events 保留 30 天（清理函数已提供，后端定时任务暂不实现）。
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------- 迁移记录
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE schema_migrations IS '已执行的表结构变更版本（增量迁移用）';

-- ---------------------------------------------------------------- 通用触发器
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
COMMENT ON FUNCTION set_updated_at() IS '更新行时自动刷新 updated_at';

-- ---------------------------------------------------------------- 用户
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      TEXT        NOT NULL,
    display_name  TEXT        NOT NULL DEFAULT '',
    email         TEXT,
    phone         TEXT,
    password_hash TEXT,
    status        TEXT        NOT NULL DEFAULT 'active',
    roles         TEXT[]      NOT NULL DEFAULT ARRAY['user']::TEXT[],
    last_login_at TIMESTAMPTZ,
    meta          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ,
    CONSTRAINT ck_users_status CHECK (status IN ('active', 'disabled', 'locked'))
);
COMMENT ON TABLE  users IS '用户表（账号密码 + JWT 登录）';
COMMENT ON COLUMN users.password_hash IS 'Argon2id/bcrypt 哈希；纯第三方登录可为空';
COMMENT ON COLUMN users.roles         IS '角色数组（user/admin/...）';
COMMENT ON COLUMN users.deleted_at    IS '逻辑删除；NULL 表示有效';

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_active ON users (lower(username)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_active    ON users (lower(email))    WHERE deleted_at IS NULL AND email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_phone_active    ON users (phone)           WHERE deleted_at IS NULL AND phone IS NOT NULL;

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- 用户身份（多登录方式扩展位）
CREATE TABLE IF NOT EXISTS user_identities (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    provider     TEXT        NOT NULL,
    provider_uid TEXT        NOT NULL,
    credential   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_identities UNIQUE (provider, provider_uid)
);
COMMENT ON TABLE  user_identities IS '外部身份映射（local/wechat/dingtalk/oidc…）';
COMMENT ON COLUMN user_identities.credential IS '凭证元数据，禁止存明文密码';

CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities (user_id);
DROP TRIGGER IF EXISTS trg_user_identities_updated_at ON user_identities;
CREATE TRIGGER trg_user_identities_updated_at BEFORE UPDATE ON user_identities FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- JWT 令牌（refresh 落库可撤销）
CREATE TABLE IF NOT EXISTS user_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    jti        TEXT        NOT NULL,
    token_hash TEXT        NOT NULL,
    token_type TEXT        NOT NULL DEFAULT 'refresh',
    issued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    user_agent TEXT,
    client_ip  INET,
    CONSTRAINT uq_user_tokens_jti UNIQUE (jti),
    CONSTRAINT ck_user_tokens_type CHECK (token_type IN ('refresh', 'access'))
);
COMMENT ON TABLE  user_tokens IS 'JWT 令牌（refresh token 哈希落库，支持撤销与设备管理）';
COMMENT ON COLUMN user_tokens.token_hash IS '令牌哈希（SHA-256），不存明文';
COMMENT ON COLUMN user_tokens.revoked_at IS '撤销时间；NULL 表示有效';

CREATE INDEX IF NOT EXISTS idx_user_tokens_user ON user_tokens (user_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_tokens_cleanup ON user_tokens (expires_at) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------- 会话
CREATE TABLE IF NOT EXISTS sessions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    title                TEXT        NOT NULL DEFAULT '新会话',
    status               TEXT        NOT NULL DEFAULT 'active',
    model_role           TEXT,
    checkpoint_thread_id TEXT        NOT NULL,
    message_count        INTEGER     NOT NULL DEFAULT 0,
    last_message_at      TIMESTAMPTZ,
    last_preview         TEXT        NOT NULL DEFAULT '',
    meta                 JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at           TIMESTAMPTZ,
    CONSTRAINT ck_sessions_status CHECK (status IN ('active', 'archived')),
    CONSTRAINT ck_sessions_message_count CHECK (message_count >= 0)
);
COMMENT ON TABLE  sessions IS '会话表（1 会话 = 1 LangGraph thread）';
COMMENT ON COLUMN sessions.checkpoint_thread_id IS '绑定的 thread_id（默认 = id::text），清理时据此删 checkpoint';
COMMENT ON COLUMN sessions.last_preview IS '最后一条消息摘要（列表展示，避免回表 messages）';
COMMENT ON COLUMN sessions.deleted_at IS '逻辑删除；数据保留，后台任务据此清理 checkpoint';

CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_thread_active ON sessions (checkpoint_thread_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_user_recent ON sessions (user_id, last_message_at DESC NULLS LAST) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_user_created ON sessions (user_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_cleanup ON sessions (last_message_at) WHERE deleted_at IS NULL;

DROP TRIGGER IF EXISTS trg_sessions_updated_at ON sessions;
CREATE TRIGGER trg_sessions_updated_at BEFORE UPDATE ON sessions FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- 消息（按月 RANGE 分区，长期保留）
CREATE TABLE IF NOT EXISTS messages (
    id            BIGSERIAL,
    session_id    UUID        NOT NULL REFERENCES sessions (id) ON DELETE RESTRICT,
    user_id       UUID        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    seq           INTEGER     NOT NULL,
    role          TEXT        NOT NULL,
    kind          TEXT        NOT NULL DEFAULT 'chat',
    content       TEXT        NOT NULL DEFAULT '',
    content_type  TEXT        NOT NULL DEFAULT 'text/markdown',
    payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    model_role    TEXT,
    token_input   INTEGER,
    token_output  INTEGER,
    latency_ms    INTEGER,
    error_code    TEXT,
    meta          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ,
    CONSTRAINT pk_messages PRIMARY KEY (id, created_at),   -- 分区表主键必须包含分区键
    CONSTRAINT ck_messages_role CHECK (role IN ('user', 'assistant', 'system')),
    CONSTRAINT ck_messages_kind CHECK (kind IN ('chat', 'clarify', 'answer', 'error')),
    CONSTRAINT ck_messages_content_type CHECK (content_type IN ('text/plain', 'text/markdown'))
) PARTITION BY RANGE (created_at);

COMMENT ON TABLE  messages IS '消息表（长期真相；按月分区，checkpoint 压缩/清理不影响）';
COMMENT ON COLUMN messages.seq IS '会话内自增序号（由 append_message 分配，会话行锁保证唯一）';
COMMENT ON COLUMN messages.kind IS 'chat/clarify/answer/error；扩展时改写 CHECK 约束';

-- 唯一性安全网：同会话同序号唯一（含分区键，否则分区表不支持）
CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_session_seq ON messages (session_id, seq, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages (session_id, seq) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_user_time ON messages (user_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_content_fts ON messages USING GIN (to_tsvector('simple', content));

-- 幂等表（分区表无法用 client_msg_id 做全局唯一，故独立小表承载）
CREATE TABLE IF NOT EXISTS message_idempotency (
    session_id         UUID        NOT NULL,
    client_msg_id      TEXT        NOT NULL,
    message_id         BIGINT,
    message_created_at TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, client_msg_id)
);
COMMENT ON TABLE message_idempotency IS '前端消息幂等键 → 已落库消息（重发去重）';

-- 分区维护：确保某时间点所在月分区存在（插入前调用，避免"无分区"报错）
CREATE OR REPLACE FUNCTION ensure_message_partition_for(p_ts TIMESTAMPTZ) RETURNS void AS $$
DECLARE
    v_start DATE := date_trunc('month', p_ts)::date;
    v_end   DATE := (v_start + interval '1 month')::date;
    v_name  TEXT := format('messages_%s', to_char(v_start, 'YYYYMM'));
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = v_name) THEN
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF messages FOR VALUES FROM (%L) TO (%L)', v_name, v_start, v_end);
        EXECUTE format('COMMENT ON TABLE %I IS %L', v_name, 'messages 月分区');
    END IF;
END;
$$ LANGUAGE plpgsql;
COMMENT ON FUNCTION ensure_message_partition_for IS '确保指定时间所在月的 messages 分区存在（幂等）';

-- 分区维护：批量预建 [今天-months_back, 今天+months_ahead] 的月分区（建议每月定时执行）
CREATE OR REPLACE FUNCTION ensure_message_partitions(months_back INTEGER DEFAULT 1, months_ahead INTEGER DEFAULT 2) RETURNS void AS $$
DECLARE
    i INTEGER;
BEGIN
    FOR i IN -months_back..months_ahead LOOP
        PERFORM ensure_message_partition_for(now() + make_interval(months => i));
    END LOOP;
END;
$$ LANGUAGE plpgsql;
COMMENT ON FUNCTION ensure_message_partitions IS '预建 messages 月分区（建议用 pg_cron 每月执行：SELECT ensure_message_partitions(1, 2)）';

-- 初始化分区：上月 ~ 下下月
SELECT ensure_message_partitions(1, 2);

-- 原子追加消息：确保分区 → 幂等校验 → 分配 seq → 刷新会话计数/预览
CREATE OR REPLACE FUNCTION append_message(
    p_session_id    UUID,
    p_role          TEXT,
    p_kind          TEXT,
    p_content       TEXT,
    p_payload       JSONB DEFAULT '{}'::jsonb,
    p_client_msg_id TEXT DEFAULT NULL,
    p_model_role    TEXT DEFAULT NULL,
    p_token_input   INTEGER DEFAULT NULL,
    p_token_output  INTEGER DEFAULT NULL,
    p_latency_ms    INTEGER DEFAULT NULL
) RETURNS messages AS $$
DECLARE
    v_seq  INTEGER;
    v_user UUID;
    v_row  messages;
BEGIN
    PERFORM ensure_message_partition_for(now());

    -- 幂等：同一 client_msg_id 直接返回既有消息
    IF p_client_msg_id IS NOT NULL THEN
        INSERT INTO message_idempotency (session_id, client_msg_id)
        VALUES (p_session_id, p_client_msg_id)
        ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN
            SELECT m.* INTO v_row
              FROM messages m
              JOIN message_idempotency i
                ON i.message_id = m.id AND i.message_created_at = m.created_at
             WHERE i.session_id = p_session_id AND i.client_msg_id = p_client_msg_id;
            RETURN v_row;
        END IF;
    END IF;

    UPDATE sessions
       SET message_count   = message_count + 1,
           last_message_at = now(),
           last_preview    = left(coalesce(p_content, ''), 100)
     WHERE id = p_session_id AND deleted_at IS NULL
    RETURNING message_count, user_id INTO v_seq, v_user;

    IF v_seq IS NULL THEN
        RAISE EXCEPTION 'session not found or deleted: %', p_session_id;
    END IF;

    INSERT INTO messages (session_id, user_id, seq, role, kind, content, payload,
                          model_role, token_input, token_output, latency_ms)
    VALUES (p_session_id, v_user, v_seq, p_role, p_kind, coalesce(p_content, ''),
            coalesce(p_payload, '{}'::jsonb), p_model_role, p_token_input, p_token_output, p_latency_ms)
    RETURNING * INTO v_row;

    IF p_client_msg_id IS NOT NULL THEN
        UPDATE message_idempotency
           SET message_id = v_row.id, message_created_at = v_row.created_at
         WHERE session_id = p_session_id AND client_msg_id = p_client_msg_id;
    END IF;
    RETURN v_row;
EXCEPTION WHEN others THEN
    -- 失败时清理"只写了一半"的幂等占位，避免后续重发被误判为已存在
    DELETE FROM message_idempotency
     WHERE session_id = p_session_id AND client_msg_id = p_client_msg_id AND message_id IS NULL;
    RAISE;
END;
$$ LANGUAGE plpgsql;
COMMENT ON FUNCTION append_message IS '原子追加消息（分区保障 + 幂等 + seq 分配 + 会话计数/预览）';

-- ---------------------------------------------------------------- 会话过程事件（保留 30 天）
CREATE TABLE IF NOT EXISTS session_events (
    id         BIGSERIAL PRIMARY KEY,
    session_id UUID        NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq        INTEGER     NOT NULL,
    type       TEXT        NOT NULL,
    phase      TEXT        NOT NULL DEFAULT '',
    payload    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_session_events_seq UNIQUE (session_id, seq),
    CONSTRAINT ck_session_events_type CHECK (type IN ('thinking', 'tool_call', 'tool_result', 'plan', 'step', 'error'))
);
COMMENT ON TABLE  session_events IS '会话执行过程事件（前端回放）；保留 30 天，由后端定时清理';
COMMENT ON COLUMN session_events.type IS '与 app/agents/events.py 的 EventType 对齐（不含直接展示的 thinkMessage/answer）';

CREATE INDEX IF NOT EXISTS idx_session_events_session_seq ON session_events (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_session_events_created ON session_events (created_at);

-- 过程事件清理（保留 30 天；后端定时任务暂不实现，仅提供函数）
CREATE OR REPLACE FUNCTION cleanup_session_events(p_retention_days INTEGER DEFAULT 30) RETURNS BIGINT AS $$
DECLARE
    v_deleted BIGINT;
BEGIN
    DELETE FROM session_events
     WHERE created_at < now() - make_interval(days => p_retention_days);
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$ LANGUAGE plpgsql;
COMMENT ON FUNCTION cleanup_session_events IS '清理 30 天前的会话过程事件（默认 30 天），返回删除行数';

-- ---------------------------------------------------------------- 监控平台（自 checkpoint 库迁入）
CREATE TABLE IF NOT EXISTS monitor_components (
    id          SERIAL PRIMARY KEY,
    name        TEXT        NOT NULL,
    page        TEXT        NOT NULL,
    metric      TEXT        NOT NULL DEFAULT 'p0',
    model       TEXT,
    stat        TEXT        NOT NULL DEFAULT 'sum',
    granularity TEXT        NOT NULL DEFAULT 'minute',
    range_type  TEXT        NOT NULL DEFAULT '30',
    start_time  TIMESTAMPTZ,
    end_time    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_monitor_components_stat CHECK (stat IN ('sum', 'avg')),
    CONSTRAINT ck_monitor_components_granularity CHECK (granularity IN ('minute', 'hour')),
    CONSTRAINT ck_monitor_components_range CHECK (range_type IN ('30', '7', 'yesterday', 'custom'))
);
COMMENT ON TABLE monitor_components IS '监控组件配置（自 checkpoint 库迁入；打点数据仍来自 logs/tracking.data）';

CREATE TABLE IF NOT EXISTS monitor_field_meanings (
    page        TEXT NOT NULL,
    slot        TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (page, slot)
);
COMMENT ON TABLE monitor_field_meanings IS '各业务名称下 Ext 槽位含义（用户自定义）';

CREATE TABLE IF NOT EXISTS user_token_usage (
    user_id       TEXT   NOT NULL,
    model         TEXT   NOT NULL,
    input_tokens  BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens  BIGINT NOT NULL DEFAULT 0,
    cost          NUMERIC(14, 6) NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, model)
);
COMMENT ON TABLE user_token_usage IS '用户 token 累计（历史兼容表；后续可直接由 messages.token_* 聚合得出，user_id 改用 users.id）';

DROP TRIGGER IF EXISTS trg_monitor_components_updated_at ON monitor_components;
CREATE TRIGGER trg_monitor_components_updated_at BEFORE UPDATE ON monitor_components FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- 迁移记录
INSERT INTO schema_migrations (version, note)
VALUES ('20260912_001_business_schema',
        'users/user_identities/user_tokens/sessions/messages(partitioned)/message_idempotency/session_events/monitor_*')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- ============================================================================
-- 运维 SQL（建库后按需执行）
--
-- 1) 分区维护（建议 pg_cron 每月执行）
--    SELECT cron.schedule('messages-partitions', '0 3 1 * *', $$SELECT ensure_message_partitions(1, 3)$$);
--    -- 或后端定时任务：每月调用 SELECT ensure_message_partitions(1, 3);
--
-- 2) 过程事件清理（保留 30 天；后端定时任务暂不实现）
--    SELECT cleanup_session_events(30);
--
-- 3) 陈旧会话：先逻辑删除，再按 checkpoint_thread_id 删除 checkpoint（应用侧调 adelete_thread）
--    UPDATE sessions SET deleted_at = now()
--     WHERE deleted_at IS NULL AND status = 'active'
--       AND coalesce(last_message_at, created_at) < now() - interval '180 days'
--     RETURNING id, checkpoint_thread_id;
--
-- 4) 过期令牌清理
--    DELETE FROM user_tokens WHERE expires_at < now() - interval '7 days';
--
-- 5) 会话列表（键集分页）
--    SELECT id, title, message_count, last_preview, last_message_at
--      FROM sessions
--     WHERE user_id = $1 AND deleted_at IS NULL AND (last_message_at, id) < ($2, $3)
--     ORDER BY last_message_at DESC NULLS LAST, id DESC
--     LIMIT 20;
--
-- 6) 历史消息（倒序取页、正序返回；分区裁剪命中当月分区）
--    SELECT seq, role, kind, content, payload, created_at
--      FROM messages
--     WHERE session_id = $1 AND deleted_at IS NULL AND seq < $2
--     ORDER BY seq DESC LIMIT 50;
--
-- 7) 自 checkpoint 库迁移 monitor 三表（在业务库执行或先导出再导入）
--    -- 方式一：pg_dump 指定表
--    --   pg_dump -h <old> -d <checkpoint_db> -t monitor_components -t monitor_field_meanings -t user_token_usage --data-only > monitor.sql
--    --   psql -h <new> -d agent_business -f monitor.sql
--    -- 方式二：同实例不同库用 dblink/FDW 直接 INSERT ... SELECT
-- ============================================================================
