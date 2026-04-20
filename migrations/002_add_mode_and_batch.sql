-- ============================================================================
-- 002_add_mode_and_batch.sql
-- Phase 1: 给 jobs 表加 mode + batch_id，支持 character_pack (CCDB) 功能
-- ----------------------------------------------------------------------------
-- 运行方式（在 223 MySQL 上）：
--   mysql -u noda_pics -p noda_pics < migrations/002_add_mode_and_batch.sql
-- 幂等：已加过列会报错，但不会破坏数据。如需先检查：
--   SHOW COLUMNS FROM jobs LIKE 'mode';
--   SHOW COLUMNS FROM jobs LIKE 'batch_id';
-- ============================================================================

ALTER TABLE jobs
  ADD COLUMN mode VARCHAR(32) NOT NULL DEFAULT 'txt2img' AFTER style,
  ADD COLUMN batch_id VARCHAR(64) DEFAULT NULL AFTER id;

-- 索引：按 batch_id 查询全部子任务（/api/batches/:id）
CREATE INDEX idx_jobs_batch_id ON jobs (batch_id);

-- 回填历史数据：有 reference_image_url 的视为 pulid（保持兼容旧行为）
UPDATE jobs
  SET mode = 'pulid'
  WHERE reference_image_url IS NOT NULL
    AND mode = 'txt2img';
