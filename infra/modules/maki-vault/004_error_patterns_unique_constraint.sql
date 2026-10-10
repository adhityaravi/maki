-- Add UNIQUE (component, pattern) backing stem's _handle_pattern_write
-- `ON CONFLICT (component, pattern) DO NOTHING` INSERT (issue #665).
--
-- Wrapped in a DO block so re-apply is a no-op if the constraint was
-- already added manually on the live cluster — Postgres has no
-- `ADD CONSTRAINT IF NOT EXISTS` syntax.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'error_patterns_component_pattern_key'
          AND conrelid = 'error_patterns'::regclass
    ) THEN
        ALTER TABLE error_patterns
            ADD CONSTRAINT error_patterns_component_pattern_key
            UNIQUE (component, pattern);
    END IF;
END $$;
