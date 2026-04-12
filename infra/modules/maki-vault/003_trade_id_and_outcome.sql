-- Add trade_id correlation column, relax NOT NULL on target/stop, expand outcome values
-- trade_id links to the proposal ID used in NATS KV (e.g. "a1b2c3d4e5f6")

ALTER TABLE trade_signals ADD COLUMN IF NOT EXISTS trade_id TEXT;
CREATE INDEX IF NOT EXISTS idx_trade_signals_trade_id ON trade_signals(trade_id);

-- target_price and stop_price are not computed in the current pipeline
ALTER TABLE trade_signals ALTER COLUMN target_price DROP NOT NULL;
ALTER TABLE trade_signals ALTER COLUMN stop_price DROP NOT NULL;

-- Expand outcome check to include win/loss alongside the original values
ALTER TABLE trade_outcomes DROP CONSTRAINT IF EXISTS trade_outcomes_outcome_check;
ALTER TABLE trade_outcomes ADD CONSTRAINT trade_outcomes_outcome_check
    CHECK (outcome IN ('win', 'loss', 'hit_target', 'hit_stop', 'manual'));
