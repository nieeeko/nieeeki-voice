-- nieeeki-voice 0003: Geraeteliste fuer die Geraetebindung (Weg B, plans/online-ohne-tailscale.md, Schritt 6).
-- __SCHEMA__ wird ersetzt durch nieeeki_voice_dev | nieeeki_voice_test | nieeeki_voice_prod.
-- Anwenden: scripts/render-migration.sh <schema> supabase/migrations/0003_geraete.sql \
--             | psql -v ON_ERROR_STOP=1 --single-transaction
-- (genau einmal je Schema, in einer Transaktion; ECC database-migrations; vor PROD Agent database-reviewer)
--
-- Ablauf: Ohne Tailscale reicht sonst ein gestohlenes Supabase-Token fuer eine Sitzung. Darum signiert das Handy
-- jedes Offer mit einem Schluessel, der es nie verlaesst (ECDSA P-256, nicht exportierbar). Hier liegt nur der
-- OEFFENTLICHE Teil. Der Voice-Core (devices.py, service_role) prueft die Signatur gegen die Zeilen mit status 'active'.
--   1. Das Handy traegt seinen oeffentlichen Schluessel als Zeile 'pending' ein (mit seinem Token, nur das ist erlaubt)
--      und zeigt einen Kurzcode aus dem Schluessel. Die Zeile wartet 10 Minuten.
--   2. Nikolaj gibt am Laptop den Code ein (uv run devices.py koppeln); nur der Voice-Core (service_role) setzt
--      status 'active'. Ein Dieb mit Token kann also nur wartende Zeilen anlegen, nie freigeben.
--   3. Entfernen geht nur am Laptop (service_role). Das Handy kann weder freigeben noch loeschen.
-- Das Offer selbst traegt sig/ts im Body (voice_signals.request); 0002 schraenkt dessen Schluessel nicht ein
-- (das macht validate_offer in rendezvous.py und der Proxy), darum aendert 0003 voice_signals nicht.

create table if not exists __SCHEMA__.voice_devices (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  public_key  text not null,               -- roher P-256-Punkt (65 Bytes) als base64url ohne Padding = 87 Zeichen
  label       text,                        -- nur zur Anzeige am Laptop (vom Handy geliefert, nicht vertrauenswuerdig)
  status      text not null default 'pending',
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null default now() + interval '10 minutes',  -- gilt nur fuer 'pending'
  approved_at timestamptz,
  -- kanonische Schreibweise: Der rohe Punkt beginnt immer mit 0x04 (= 'B'), und 65 Bytes lassen im letzten der 87
  -- Zeichen zwei Fuellbits frei (nur A E I M Q U Y c g k o s w 0 4 8). Sonst gaebe es mehrere Schreibweisen desselben
  -- Schluessels, die den Unique-Index umgehen und beim Koppeln denselben Kurzcode haetten.
  constraint voice_devices_key_chk    check (public_key ~ '^B[A-Za-z0-9_-]{85}[AEIMQUYcgkosw048]$'),
  -- keine Steuer- und Formatzeichen: Das Etikett erscheint im Terminal des Laptops. Code-Punkte ausdruecklich, damit es
  -- nicht von der Locale der Datenbank abhaengt ([[:cntrl:]] trifft U+0080 bis U+009F je nach lc_ctype, U+202E nie).
  constraint voice_devices_label_chk  check (label is null or (char_length(label) between 1 and 40
    and label !~ U&'[\0001-\001F\007F-\009F\00AD\061C\200B-\200F\2028-\202E\2060-\206F\FEFF]')),
  constraint voice_devices_status_chk check (status in ('pending', 'active')),
  constraint voice_devices_approved_chk check ((status = 'active') = (approved_at is not null)),
  constraint voice_devices_expiry_chk check (expires_at <= created_at + interval '10 minutes'),
  -- der Index dazu deckt auch den Fremdschluessel user_id ab (fuehrende Spalte)
  constraint voice_devices_user_key_uq unique (user_id, public_key)
);

-- RLS sofort, noch vor allen Rechten (kein Fenster ohne Schutz, falls ein Lauf abbricht)
alter table __SCHEMA__.voice_devices enable row level security;

-- Obergrenzen gegen Missbrauch mit einem gestohlenen Token: hoechstens 5 wartende Zeilen (abgelaufene zaehlen nicht,
-- sie werden hier gleich entfernt: so kann dasselbe Handy es nach 10 Minuten noch einmal versuchen) und 10 Zeilen
-- gesamt je Nutzer. Pro Nutzer serialisiert (Advisory-Lock). security definer, weil `authenticated` nichts loeschen
-- darf; die Funktion beruehrt nur Zeilen von new.user_id, und den kann der Aufrufer nicht setzen (keine Spaltenrechte,
-- unten). PT429 macht PostgREST zu HTTP 429.
create or replace function __SCHEMA__.voice_devices_guard() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  -- Ein angemeldeter Nutzer legt nur Zeilen fuer sich an (service_role hat auth.uid() = null und bleibt unberuehrt)
  if auth.uid() is not null and new.user_id is distinct from auth.uid() then
    raise exception 'fremder Nutzer' using errcode = '42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('voice_devices:' || new.user_id::text, 0));
  delete from __SCHEMA__.voice_devices d
   where d.user_id = new.user_id and d.status = 'pending' and d.expires_at <= now();
  if new.status = 'pending'
     and (select count(*) from __SCHEMA__.voice_devices d where d.user_id = new.user_id and d.status = 'pending') >= 5 then
    raise exception 'zu viele offene Kopplungen' using errcode = 'PT429';
  end if;
  if (select count(*) from __SCHEMA__.voice_devices d where d.user_id = new.user_id) >= 10 then
    raise exception 'zu viele Geraete' using errcode = 'PT429';
  end if;
  return new;
end $$;

create trigger voice_devices_guard before insert on __SCHEMA__.voice_devices
  for each row execute function __SCHEMA__.voice_devices_guard();

-- Freigabe: Die Frist von 10 Minuten gilt auch dann, wenn die Zeile noch nicht aufgeraeumt wurde (der Kurzcode hat nur
-- 60 Bit, die Frist ist Teil der Sicherheit), und der Freigabezeitpunkt kommt von der Uhr der Datenbank. Eine
-- abgelaufene Zeile wird nicht freigegeben: "return null" ueberspringt sie, PostgREST meldet dann [] (devices.py:
-- "inzwischen abgelaufen").
create or replace function __SCHEMA__.voice_devices_approve() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if old.status = 'pending' and new.status = 'active' then
    if old.expires_at <= now() then
      return null;
    end if;
    new.approved_at := now();
  end if;
  return new;
end $$;

create trigger voice_devices_approve before update on __SCHEMA__.voice_devices
  for each row execute function __SCHEMA__.voice_devices_approve();

-- Voice-Core / Laptop-Werkzeug: abgelaufene wartende Zeilen entfernen (Uhr der Datenbank)
create or replace function __SCHEMA__.purge_voice_devices()
returns integer language sql volatile security invoker set search_path = '' as $$
  with d as (delete from __SCHEMA__.voice_devices where status = 'pending' and expires_at <= now() returning 1)
  select count(*)::integer from d;
$$;

-- ---------- Rechte & Row Level Security ----------
-- Neue Tabellen erben die Rechte aus 0001 nicht: alles ausdruecklich und so schmal wie moeglich.
-- Das Handy darf die eigenen Zeilen lesen (Stand der Kopplung) und eine wartende Zeile anlegen (nur public_key und
-- label); Status, Ablauf, Eigentuemer und Freigabe setzt die Datenbank bzw. der Voice-Core.
revoke all on __SCHEMA__.voice_devices from public, anon, authenticated;
grant select (id, user_id, public_key, label, status, created_at, expires_at, approved_at) on __SCHEMA__.voice_devices to authenticated;
grant insert (public_key, label) on __SCHEMA__.voice_devices to authenticated;
grant select, insert, update, delete on __SCHEMA__.voice_devices to service_role;

create policy owner_select on __SCHEMA__.voice_devices for select to authenticated
  using (user_id = (select auth.uid())
         and exists (select 1 from __SCHEMA__.profiles p where p.user_id = (select auth.uid())));
create policy owner_insert on __SCHEMA__.voice_devices for insert to authenticated
  with check (user_id = (select auth.uid()) and status = 'pending'
         and exists (select 1 from __SCHEMA__.profiles p where p.user_id = (select auth.uid())));

-- Zusaetzlich (RESTRICTIVE, fuer alle Rollen und alle Befehle): zweiter Faktor und Anmeldung hoechstens 1 Tag alt,
-- dieselbe Regel wie bei voice_signals (0002) und auth_gate.py.
create policy second_factor_and_fresh_login on __SCHEMA__.voice_devices as restrictive for all to public
  using (
    (select auth.jwt()->>'aal') = 'aal2'
    and coalesce((select min((e->>'timestamp')::numeric)
                    from jsonb_array_elements(case when jsonb_typeof((select auth.jwt()->'amr')) = 'array'
                                                   then (select auth.jwt()->'amr') else '[]'::jsonb end) e
                   where jsonb_typeof(e->'timestamp') = 'number'), 0)
        >= extract(epoch from now()) - 86400)
  with check (
    (select auth.jwt()->>'aal') = 'aal2'
    and coalesce((select min((e->>'timestamp')::numeric)
                    from jsonb_array_elements(case when jsonb_typeof((select auth.jwt()->'amr')) = 'array'
                                                   then (select auth.jwt()->'amr') else '[]'::jsonb end) e
                   where jsonb_typeof(e->'timestamp') = 'number'), 0)
        >= extract(epoch from now()) - 86400);

-- Funktionen: kein Zugriff fuer PUBLIC/anon/authenticated; Aufraeumen nur fuer den Voice-Core
revoke execute on function __SCHEMA__.voice_devices_guard() from public, anon, authenticated;
revoke execute on function __SCHEMA__.voice_devices_approve() from public, anon, authenticated;
revoke execute on function __SCHEMA__.purge_voice_devices() from public, anon, authenticated;
grant execute on function __SCHEMA__.purge_voice_devices() to service_role;
