-- Trading system tables
-- Signals, outcomes, and per-asset Kelly + weight state
--
-- NOTE: asset_config is seeded at runtime by the trading loop,
-- not here. Watchlist is managed via loop config (private).

CREATE TABLE trade_signals (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset               TEXT NOT NULL,                          -- e.g. BTC/EUR, SAP
    asset_type          TEXT NOT NULL
                            CHECK (asset_type IN ('crypto', 'stock_xetra', 'stock_us')),
    direction           TEXT NOT NULL
                            CHECK (direction IN ('long', 'short')),
    entry_price         NUMERIC(18, 6) NOT NULL,
    target_price        NUMERIC(18, 6) NOT NULL,
    stop_price          NUMERIC(18, 6) NOT NULL,
    position_size_pct   NUMERIC(6, 4) NOT NULL,                -- Kelly fraction, e.g. 0.25
    composite_score     NUMERIC(6, 2) NOT NULL,                -- 0–100
    ev                  NUMERIC(10, 4),                        -- expected value at signal time
    sentiment_score     NUMERIC(6, 4),                         -- -1 to 1
    sentiment_source    TEXT CHECK (sentiment_source IN ('finnhub', 'finbert', 'none')),
    indicator_snapshot  JSONB,                                 -- RSI, BB, OBV values at signal time
    paper               BOOLEAN NOT NULL DEFAULT true,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'accepted', 'skipped')),
    entry_actual        NUMERIC(18, 6),                        -- actual fill price if accepted
    accepted_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE trade_outcomes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id   UUID NOT NULL REFERENCES trade_signals(id) ON DELETE CASCADE,
    outcome     TEXT NOT NULL
                    CHECK (outcome IN ('hit_target', 'hit_stop', 'manual')),
    exit_price  NUMERIC(18, 6) NOT NULL,
    pnl_pct     NUMERIC(10, 6),                                -- realised return %
    pnl_eur     NUMERIC(12, 4),                                -- realised return in EUR
    logged_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE asset_config (
    asset               TEXT PRIMARY KEY,
    asset_type          TEXT NOT NULL
                            CHECK (asset_type IN ('crypto', 'stock_xetra', 'stock_us')),
    active              BOOLEAN NOT NULL DEFAULT true,
    trade_count         INT NOT NULL DEFAULT 0,
    win_count           INT NOT NULL DEFAULT 0,
    total_win_pct       NUMERIC(12, 6) NOT NULL DEFAULT 0,     -- sum of winning returns
    total_loss_pct      NUMERIC(12, 6) NOT NULL DEFAULT 0,     -- sum of losing returns (abs)
    kelly_fraction      NUMERIC(6, 4) NOT NULL DEFAULT 0.25,   -- recalculated after each trade
    indicator_weights   JSONB NOT NULL DEFAULT '{
        "rsi": 0.35,
        "bb": 0.35,
        "obv": 0.20,
        "sentiment": 0.10
    }',
    last_signal_at      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_trade_signals_asset     ON trade_signals(asset);
CREATE INDEX idx_trade_signals_paper     ON trade_signals(paper);
CREATE INDEX idx_trade_signals_status    ON trade_signals(status);
CREATE INDEX idx_trade_signals_created   ON trade_signals(created_at DESC);
CREATE INDEX idx_trade_outcomes_signal   ON trade_outcomes(signal_id);
