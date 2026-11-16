CREATE TABLE discovery.admission_guard (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton)
);
INSERT INTO discovery.admission_guard(singleton) VALUES (true);
REVOKE ALL ON discovery.admission_guard FROM PUBLIC;
