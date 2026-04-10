CREATE TABLE error_patterns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    component TEXT NOT NULL,
    pattern TEXT NOT NULL,
    classification TEXT NOT NULL
        CHECK (classification IN ('no_op', 'escalate')),
    confidence FLOAT NOT NULL DEFAULT 0.5,
    occurrence_count INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes TEXT
);

CREATE INDEX idx_error_patterns_component ON error_patterns(component);
CREATE INDEX idx_error_patterns_classification ON error_patterns(classification);
