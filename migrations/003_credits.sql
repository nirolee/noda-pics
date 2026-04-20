-- ============================================================================
-- 003_credits.sql
-- Phase 2: 按张计费 Credits 系统
-- ----------------------------------------------------------------------------
-- 运行方式：
--   mysql -u noda_pics -p noda_pics < migrations/003_credits.sql
-- ============================================================================

-- 1. 给 users 表加 credits 余额字段
-- 所有现有用户将获得 10 credits（与新用户一致，视为补偿）
ALTER TABLE users
  ADD COLUMN credits_balance INT NOT NULL DEFAULT 10 AFTER plan_expires_at;

-- 2. Credit 流水表（审计 + 对账）
CREATE TABLE credit_ledger (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id     INT NOT NULL,
  delta       INT NOT NULL COMMENT '+ for purchase/bonus, - for spend',
  balance_after INT NOT NULL COMMENT '本次操作后的余额（便于对账）',
  reason      VARCHAR(64) NOT NULL COMMENT 'signup_bonus / purchase / job_spend / batch_spend / admin_grant / refund',
  ref_id      VARCHAR(128) DEFAULT NULL COMMENT '关联 job_id / batch_id / checkout_id',
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_user (user_id, created_at),
  INDEX idx_ref  (ref_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. 给现有用户补一条 signup_bonus 流水（使流水与余额一致）
INSERT INTO credit_ledger (user_id, delta, balance_after, reason, ref_id, created_at)
  SELECT id, 10, 10, 'signup_bonus', NULL, COALESCE(created_at, NOW()) FROM users;
