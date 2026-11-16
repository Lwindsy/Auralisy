ALTER TABLE discovery.execution_slot
    DROP CONSTRAINT execution_slot_slot_no_check;
ALTER TABLE discovery.execution_slot
    ADD CONSTRAINT execution_slot_slot_no_check CHECK (slot_no BETWEEN 1 AND 16);

INSERT INTO discovery.execution_slot(slot_no,state)
SELECT slot_no,'FREE' FROM generate_series(4,16) AS slot_no;
