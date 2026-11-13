-- Rueckweg zu 0003 (ECC database-migrations: jede Migration hat UP und DOWN).
-- Loescht nur die Geraeteliste (gekoppelte Geraete gehen verloren, danach muss neu gekoppelt werden); 0001, 0002 und
-- alle Gespraechsdaten bleiben. Reihenfolge: erst die Tabelle (der Trigger faellt mit ihr weg), dann seine Funktion.
-- Anwenden wie das UP: psql -v ON_ERROR_STOP=1 --single-transaction
drop function if exists __SCHEMA__.purge_voice_devices();
drop table    if exists __SCHEMA__.voice_devices;
drop function if exists __SCHEMA__.voice_devices_guard();
drop function if exists __SCHEMA__.voice_devices_approve();
