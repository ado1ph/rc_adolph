ALTER TABLE delivery_reconciliations
    DROP CONSTRAINT delivery_reconciliations_outcome_check;

ALTER TABLE delivery_reconciliations
    ADD CONSTRAINT delivery_reconciliations_outcome_check
    CHECK (outcome IN (
        'delivered',
        'not_delivered',
        'not_delivered_new_key_required',
        'not_delivered_snapshot_expired'
    ));

DELETE FROM delivery_schema WHERE version = 2;
INSERT INTO delivery_schema(version) VALUES (3);
