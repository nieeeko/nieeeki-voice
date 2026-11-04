-- Rueckweg zu 0002 (ECC database-migrations: jede Migration hat UP und DOWN).
-- Loescht nur die Signalisierung (offene Anfragen gehen verloren), 0001 und alle Gespraechsdaten bleiben.
-- Reihenfolge ist wichtig: claim_voice_signal gibt den Zeilentyp der Tabelle zurueck und haengt daran, die Tabelle
-- laesst sich erst danach loeschen. Der Trigger faellt mit der Tabelle weg, danach seine Funktion, zuletzt der Typ.
-- Anwenden wie das UP: psql -v ON_ERROR_STOP=1 --single-transaction
drop function if exists __SCHEMA__.claim_voice_signal(uuid);
drop function if exists __SCHEMA__.purge_voice_signals(interval);
drop table    if exists __SCHEMA__.voice_signals;
drop function if exists __SCHEMA__.voice_signals_rate_limit();
drop type     if exists __SCHEMA__.signal_status;
