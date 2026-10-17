-- Rueckweg zu 0001 (ECC database-migrations: jede Migration hat UP und DOWN).
-- ACHTUNG: loescht alle nieeeki-voice-Daten im Schema. In PROD nur nach Backup.
drop schema if exists __SCHEMA__ cascade;
