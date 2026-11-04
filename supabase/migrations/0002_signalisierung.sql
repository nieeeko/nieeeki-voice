-- nieeeki-voice 0002: Signalisierung fuer "online ohne Tailscale" (Weg B, plans/online-ohne-tailscale.md).
-- __SCHEMA__ wird ersetzt durch nieeeki_voice_dev | nieeeki_voice_test | nieeeki_voice_prod.
-- Anwenden: scripts/render-migration.sh <schema> supabase/migrations/0002_signalisierung.sql \
--             | psql -v ON_ERROR_STOP=1 --single-transaction
-- (genau einmal je Schema, in einer Transaktion: ein Teil-Lauf soll keine Tabelle ohne RLS hinterlassen;
--  ECC database-migrations; vor PROD Agent database-reviewer)
--
-- Ablauf: Der Browser schickt sein WebRTC-Offer an /api/rtc (Vercel). Die Route legt hier eine Zeile an
-- (als Nutzer, die Datenbank prueft aal2, Anmeldealter und Profil). Der Voice-Core fragt nur ausgehend nach,
-- uebernimmt die Zeile atomar (claim_voice_signal), uebergibt das Offer seinem WebRTC-Handler und schreibt die
-- Antwort zurueck. Nur Offers werden Zeilen: ICE-Kandidaten stecken im Offer (waitForICEGathering), PATCH-Anfragen
-- des Clients beantwortet die Route ohne Datenbank. Neuverhandlung und ICE-Neustart sind weitere Offers (pc_id,
-- restart_pc) und damit weitere Zeilen.
--
-- Aufbewahrung: Das Offer enthaelt IP-Adressen. Nach dem Verbindungsaufbau ersetzt der Voice-Core request durch
-- einen Platzhalter und setzt response auf null, loescht die Zeile aber NICHT sofort: Das Rate-Limit zaehlt nur
-- vorhandene Zeilen. purge_voice_signals() raeumt Zeilen frueher als 2 Minuten nie weg (Rate-Limit-Fenster) und
-- sonst nach der gewuenschten Frist (Standard 10 Minuten); der Voice-Core ruft sie beim Start und jede Minute auf.
--
-- Abweichungen vom Plan, bewusst: (1) Rate-Limit statt "hoechstens eine offene Anfrage" (Neuverhandlung und
-- ICE-Neustart brauchen mehrere Zeilen in kurzer Folge). (2) Nicht in der Realtime-Publikation: Der Voice-Core
-- fragt zuerst alle 2 s ab (Plan, Schritt 4). (3) voice_ice (ICE-Zugangsdaten) folgt mit Schritt 3.

do $$ begin create type __SCHEMA__.signal_status as enum ('pending','claimed','answered','failed');
  exception when duplicate_object then null; end $$;

create table if not exists __SCHEMA__.voice_signals (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  request    jsonb not null,            -- Body des Offers: sdp, type, pc_id, restart_pc, requestData
  response   jsonb,                     -- Antwort des Voice-Cores: sdp, type, pc_id (oder ein Fehler)
  status     __SCHEMA__.signal_status not null default 'pending',
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default now() + interval '60 seconds',
  -- coalesce: Ein fehlender Schluessel ergibt NULL, und ein CHECK laesst NULL passieren (nur FALSE weist ab)
  constraint voice_signals_request_chk check (
    jsonb_typeof(request) = 'object'
    and coalesce(request->>'type', '') = 'offer'
    and coalesce(jsonb_typeof(request->'sdp'), '') = 'string'
    and octet_length(request::text) <= 32768),
  constraint voice_signals_response_chk check (response is null or octet_length(response::text) <= 32768),
  constraint voice_signals_answer_chk check (status <> 'answered' or response is not null),
  constraint voice_signals_expiry_chk check (expires_at <= created_at + interval '60 seconds')
);

-- RLS sofort, noch vor allen Rechten (kein Fenster ohne Schutz, falls ein Lauf abbricht)
alter table __SCHEMA__.voice_signals enable row level security;

create index if not exists voice_signals_user_idx    on __SCHEMA__.voice_signals (user_id, created_at desc);
create index if not exists voice_signals_pending_idx on __SCHEMA__.voice_signals (created_at) where status = 'pending';
create index if not exists voice_signals_created_idx on __SCHEMA__.voice_signals (created_at);

-- Obergrenze gegen Zeilenflut: hoechstens 6 Anfragen je Nutzer und Minute und 60 gespeicherte Zeilen
-- (begrenzt auch das Wachstum, falls der Voice-Core ausfaellt). Pro Nutzer serialisiert (Advisory-Lock), damit
-- parallele Einfuegungen nicht alle bei demselben Zaehlerstand durchlaufen. PT429 macht PostgREST zu HTTP 429.
-- Laeuft als Aufrufer, also unter RLS (wo der Insert erlaubt ist, sind die Zeilen auch sichtbar).
create or replace function __SCHEMA__.voice_signals_rate_limit() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  perform pg_advisory_xact_lock(hashtextextended('voice_signals:' || new.user_id::text, 0));
  if (select count(*) from __SCHEMA__.voice_signals s
       where s.user_id = new.user_id and s.created_at > now() - interval '1 minute') >= 6 then
    raise exception 'zu viele Sitzungsanfragen' using errcode = 'PT429';
  end if;
  if (select count(*) from __SCHEMA__.voice_signals s where s.user_id = new.user_id) >= 60 then
    raise exception 'zu viele gespeicherte Sitzungsanfragen' using errcode = 'PT429';
  end if;
  return new;
end $$;

create trigger voice_signals_rate before insert on __SCHEMA__.voice_signals
  for each row execute function __SCHEMA__.voice_signals_rate_limit();

-- Voice-Core: naechste offene Anfrage des Eigentuemers atomar uebernehmen (mehrere Abfragen duerfen sich
-- nicht dieselbe Zeile greifen; die Ablaufzeit gilt nach der Uhr der Datenbank).
create or replace function __SCHEMA__.claim_voice_signal(p_user_id uuid)
returns setof __SCHEMA__.voice_signals
language sql volatile security invoker set search_path = '' as $$
  update __SCHEMA__.voice_signals s
     set status = 'claimed'
   where s.status = 'pending'
     and s.id = (select c.id from __SCHEMA__.voice_signals c
                  where c.status = 'pending' and c.user_id = p_user_id and c.expires_at > now()
                  order by c.created_at
                  limit 1
                  for update skip locked)
  returning s.*;
$$;

-- Voice-Core: alte Zeilen entfernen, nach der Uhr der Datenbank, nie juenger als das Rate-Limit-Fenster.
create or replace function __SCHEMA__.purge_voice_signals(p_older_than interval default interval '10 minutes')
returns integer language sql volatile security invoker set search_path = '' as $$
  with d as (delete from __SCHEMA__.voice_signals
              where created_at < now() - greatest(p_older_than, interval '2 minutes') returning 1)
  select count(*)::integer from d;
$$;

-- ---------- Rechte & Row Level Security ----------
-- Neue Tabellen erben die Rechte aus 0001 nicht: alles ausdruecklich und so schmal wie moeglich.
-- Die UI-Seite darf nur lesen und eine Anfrage einfuegen (nur Spalte request); Status, Antwort, Ablauf
-- und Eigentuemer setzt die Datenbank bzw. der Voice-Core.
revoke all on __SCHEMA__.voice_signals from public, anon, authenticated;
grant select on __SCHEMA__.voice_signals to authenticated;
grant insert (request) on __SCHEMA__.voice_signals to authenticated;
grant select, insert, update, delete on __SCHEMA__.voice_signals to service_role;

create policy owner_select on __SCHEMA__.voice_signals for select to authenticated
  using (user_id = (select auth.uid())
         and exists (select 1 from __SCHEMA__.profiles p where p.user_id = (select auth.uid())));
create policy owner_insert on __SCHEMA__.voice_signals for insert to authenticated
  with check (user_id = (select auth.uid())
         and exists (select 1 from __SCHEMA__.profiles p where p.user_id = (select auth.uid())));

-- Zusaetzlich (RESTRICTIVE, fuer alle Rollen und alle Befehle): zweiter Faktor und Anmeldung hoechstens 1 Tag alt.
-- Dieselbe Regel wie auth_gate.py fuer die Web-UI (Anmeldebeginn = fruehester Zeitstempel in amr, nur echte
-- Zahlen zaehlen wie dort). Fehlt amr oder ist es kaputt, gilt die Anmeldung als zu alt.
create policy second_factor_and_fresh_login on __SCHEMA__.voice_signals as restrictive for all to public
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

-- Funktionen: kein Zugriff fuer PUBLIC/anon; Uebernahme und Aufraeumen nur fuer den Voice-Core
revoke execute on function __SCHEMA__.voice_signals_rate_limit() from public, anon;
revoke execute on function __SCHEMA__.claim_voice_signal(uuid) from public, anon, authenticated;
revoke execute on function __SCHEMA__.purge_voice_signals(interval) from public, anon, authenticated;
grant execute on function __SCHEMA__.claim_voice_signal(uuid) to service_role;
grant execute on function __SCHEMA__.purge_voice_signals(interval) to service_role;
