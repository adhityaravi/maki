ALTER TABLE error_patterns
    ADD CONSTRAINT error_patterns_component_pattern_key UNIQUE (component, pattern);
