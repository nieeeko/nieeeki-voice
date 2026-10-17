-- nieeeki-voice: Grundschema. __SCHEMA__ wird ersetzt durch
--   nieeeki_voice_dev | nieeeki_voice_test  (shared-devtest)
--   nieeeki_voice_prod                     (shared-prod)
-- Anwenden: scripts/render-migration.sh <schema> | psql  (genau einmal je Schema; ECC database-migrations)

create extension if not exists vector with schema extensions;

create schema if not exists __SCHEMA__;
-- kein anon: nieeeki ist nicht oeffentlich
grant usage on schema __SCHEMA__ to authenticated, service_role;

-- ---------- Typen (je Typ eigener Block, damit Teil-Laeufe sauber nachziehen) ----------
do $$ begin create type __SCHEMA__.turn_role as enum ('user','assistant','system','tool');
  exception when duplicate_object then null; end $$;
do $$ begin create type __SCHEMA__.memory_kind as enum ('fact','preference','behavior','lesson','skill','episode');
  exception when duplicate_object then null; end $$;
do $$ begin create type __SCHEMA__.feedback_kind as enum ('correction','praise','thumbs_down','thumbs_up','missed_intent');
  exception when duplicate_object then null; end $$;
do $$ begin create type __SCHEMA__.risk_class as enum ('read','change','critical','blocked');
  exception when duplicate_object then null; end $$;
do $$ begin create type __SCHEMA__.action_status as enum ('pending','approved','rejected','running','done','failed','blocked');
  exception when duplicate_object then null; end $$;
do $$ begin create type __SCHEMA__.canvas_type as enum ('table','document','bpmn','mermaid','flow','pipeline','whiteboard','code','chart');
  exception when duplicate_object then null; end $$;

-- ---------- Hilfsfunktion ----------
create or replace function __SCHEMA__.touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin new.updated_at = now(); return new; end $$;

-- ---------- Profil & Geraete ----------
create table if not exists __SCHEMA__.profiles (
  user_id      uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  voice_id     text,                       -- Soniox-Stimme oder UUID der geklonten Stimme
  wake_word    text not null default 'nieeeki',
  settings     jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create table if not exists __SCHEMA__.devices (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references auth.users(id) on delete cascade,
  name           text not null,
  hostname       text,
  tailscale_name text,
  kind           text not null default 'windows-pc',  -- windows-pc | desktop-app | browser
  last_seen_at   timestamptz,
  created_at     timestamptz not null default now()
);

-- ---------- Gespraeche ----------
create table if not exists __SCHEMA__.sessions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  device_id  uuid references __SCHEMA__.devices(id) on delete set null,
  channel    text not null default 'web',       -- web | desktop | phone
  summary    text,
  started_at timestamptz not null default now(),
  ended_at   timestamptz,
  constraint sessions_time_chk check (ended_at is null or ended_at >= started_at)
);

create table if not exists __SCHEMA__.speakers (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,  -- Besitzer des Profils
  name        text not null,
  is_owner    boolean not null default false,   -- true = Nikolaj selbst
  voiceprint  extensions.vector(192),           -- Sprecher-Embedding (z. B. ECAPA-TDNN)
  enrolled_at timestamptz not null default now()
);

create table if not exists __SCHEMA__.turns (
  id            uuid primary key default gen_random_uuid(),
  session_id    uuid not null references __SCHEMA__.sessions(id) on delete cascade,
  user_id       uuid not null references auth.users(id) on delete cascade,
  role          __SCHEMA__.turn_role not null,
  content       text not null,                 -- bei assistant: tatsaechlich Gesprochenes
  interrupted   boolean not null default false,
  speaker_label text,                          -- Soniox-Diarisierung (1, 2, ...)
  speaker_id    uuid references __SCHEMA__.speakers(id) on delete set null,
  latency_ms    integer check (latency_ms >= 0),
  meta          jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);

-- ---------- Lernspeicher ----------
create table if not exists __SCHEMA__.memories (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references auth.users(id) on delete cascade,
  kind              __SCHEMA__.memory_kind not null,
  content           text not null,
  embedding         extensions.vector(1024),
  importance        smallint not null default 3 check (importance between 1 and 5),
  confidence        real not null default 0.8 check (confidence between 0 and 1),
  is_active         boolean not null default true,
  superseded_by     uuid references __SCHEMA__.memories(id) on delete set null,
  source_session_id uuid references __SCHEMA__.sessions(id) on delete set null,
  source_turn_id    uuid references __SCHEMA__.turns(id) on delete set null,
  use_count         integer not null default 0 check (use_count >= 0),
  last_used_at      timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create table if not exists __SCHEMA__.feedback (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  turn_id     uuid references __SCHEMA__.turns(id) on delete set null,
  action_id   uuid,
  kind        __SCHEMA__.feedback_kind not null,
  note        text,
  lesson_id   uuid references __SCHEMA__.memories(id) on delete set null,  -- daraus gelernte Lektion
  created_at  timestamptz not null default now()
);

-- ---------- Canvas ----------
create table if not exists __SCHEMA__.canvas_items (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  session_id uuid references __SCHEMA__.sessions(id) on delete set null,
  type       __SCHEMA__.canvas_type not null,
  title      text not null,
  data       jsonb not null,
  version    integer not null default 1 check (version >= 1),
  parent_id  uuid references __SCHEMA__.canvas_items(id) on delete set null,  -- Vorversion
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- ---------- PC-Aktionen (Audit + Freigaben) ----------
create table if not exists __SCHEMA__.actions (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  session_id   uuid references __SCHEMA__.sessions(id) on delete set null,
  turn_id      uuid references __SCHEMA__.turns(id) on delete set null,
  device_id    uuid references __SCHEMA__.devices(id) on delete set null,
  tool         text not null,
  params       jsonb not null default '{}'::jsonb,
  reason       text,
  risk         __SCHEMA__.risk_class not null,
  status       __SCHEMA__.action_status not null default 'pending',
  result       jsonb,
  decided_at   timestamptz,
  created_at   timestamptz not null default now(),
  finished_at  timestamptz
);

alter table __SCHEMA__.feedback add constraint feedback_action_fk
  foreign key (action_id) references __SCHEMA__.actions(id) on delete set null;

-- ---------- Indizes ----------
create index if not exists devices_user_idx      on __SCHEMA__.devices (user_id);
create index if not exists sessions_user_idx     on __SCHEMA__.sessions (user_id, started_at desc);
create index if not exists sessions_device_idx   on __SCHEMA__.sessions (device_id);
create index if not exists speakers_user_idx     on __SCHEMA__.speakers (user_id);
create unique index if not exists speakers_one_owner_idx on __SCHEMA__.speakers (user_id) where is_owner;
create index if not exists turns_session_idx     on __SCHEMA__.turns (session_id, created_at);
create index if not exists turns_user_idx        on __SCHEMA__.turns (user_id, created_at desc);
create index if not exists turns_speaker_idx     on __SCHEMA__.turns (speaker_id) where speaker_id is not null;
create index if not exists memories_user_all_idx on __SCHEMA__.memories (user_id);
create index if not exists memories_user_idx     on __SCHEMA__.memories (user_id, kind) where is_active;
create index if not exists memories_super_idx    on __SCHEMA__.memories (superseded_by);
create index if not exists memories_session_idx  on __SCHEMA__.memories (source_session_id);
create index if not exists memories_turn_idx     on __SCHEMA__.memories (source_turn_id);
create index if not exists memories_embedding_idx on __SCHEMA__.memories
  using hnsw (embedding extensions.vector_cosine_ops);
create index if not exists feedback_user_idx     on __SCHEMA__.feedback (user_id);
create index if not exists feedback_turn_idx     on __SCHEMA__.feedback (turn_id);
create index if not exists feedback_action_idx   on __SCHEMA__.feedback (action_id);
create index if not exists feedback_lesson_idx   on __SCHEMA__.feedback (lesson_id);
create index if not exists canvas_user_idx       on __SCHEMA__.canvas_items (user_id, updated_at desc);
create index if not exists canvas_session_idx    on __SCHEMA__.canvas_items (session_id);
create index if not exists canvas_parent_idx     on __SCHEMA__.canvas_items (parent_id);
create index if not exists actions_user_idx      on __SCHEMA__.actions (user_id, created_at desc);
create index if not exists actions_pending_idx   on __SCHEMA__.actions (user_id) where status = 'pending';
create index if not exists actions_session_idx   on __SCHEMA__.actions (session_id);
create index if not exists actions_turn_idx      on __SCHEMA__.actions (turn_id);
create index if not exists actions_device_idx    on __SCHEMA__.actions (device_id);

-- ---------- Trigger ----------
create trigger profiles_touch before update on __SCHEMA__.profiles
  for each row execute function __SCHEMA__.touch_updated_at();
create trigger memories_touch before update on __SCHEMA__.memories
  for each row execute function __SCHEMA__.touch_updated_at();
create trigger canvas_touch before update on __SCHEMA__.canvas_items
  for each row execute function __SCHEMA__.touch_updated_at();

-- ---------- Semantische Suche ----------
create or replace function __SCHEMA__.match_memories(
  query_embedding extensions.vector(1024),
  match_count int default 8,
  p_user_id uuid default null
) returns table (id uuid, kind __SCHEMA__.memory_kind, content text, similarity real)
language plpgsql stable security invoker set search_path = '' as $$
begin
  -- iterative HNSW-Suche, damit Filter (aktiv, Nutzer) die Trefferzahl nicht schrumpfen lassen
  perform set_config('hnsw.iterative_scan', 'relaxed_order', true);
  perform set_config('hnsw.ef_search', '100', true);
  return query
    select m.id, m.kind, m.content,
           (1 - (m.embedding operator(extensions.<=>) query_embedding))::real
    from __SCHEMA__.memories m
    where m.is_active
      and m.embedding is not null
      and m.user_id = coalesce(p_user_id, (select auth.uid()))
    order by m.embedding operator(extensions.<=>) query_embedding
    limit greatest(1, least(match_count, 50));
end $$;

-- ---------- Rechte & Row Level Security ----------
-- Grundsatz (ECC database-reviewer): Das Projekt ist mit anderen Apps geteilt, auth.users
-- enthaelt also fremde Nutzer. Zugriff hat nur, wer ein nieeeki-Profil hat - und Profile
-- legt ausschliesslich der Backend-Dienst (service_role) an.

grant select, insert, update, delete on all tables in schema __SCHEMA__ to service_role;
grant select, insert, update, delete on all tables in schema __SCHEMA__ to authenticated;
alter default privileges in schema __SCHEMA__
  grant select, insert, update, delete on tables to service_role;

do $$
declare t text;
begin
  foreach t in array array['profiles','devices','sessions','speakers','turns','memories',
                           'feedback','canvas_items','actions'] loop
    execute format('alter table __SCHEMA__.%I enable row level security', t);
  end loop;

  -- Tabellen mit vollem Eigentuemer-Zugriff, aber nur fuer registrierte nieeeki-Nutzer
  foreach t in array array['devices','sessions','speakers','turns','memories',
                           'feedback','canvas_items'] loop
    execute format(
      'create policy owner_all on __SCHEMA__.%I for all to authenticated
         using (user_id = (select auth.uid())
                and exists (select 1 from __SCHEMA__.profiles p where p.user_id = (select auth.uid())))
         with check (user_id = (select auth.uid())
                and exists (select 1 from __SCHEMA__.profiles p where p.user_id = (select auth.uid())))', t);
  end loop;
end $$;

-- Profil: lesen und aendern ja, anlegen/loeschen nur durch das Backend
create policy profile_select on __SCHEMA__.profiles for select to authenticated
  using (user_id = (select auth.uid()));
create policy profile_update on __SCHEMA__.profiles for update to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
revoke insert, delete on __SCHEMA__.profiles from authenticated;

-- PC-Aktionen: die UI darf nur lesen und offene Anfragen freigeben/ablehnen.
-- Anlegen, Ausfuehren und Ergebnis schreiben nur der Backend-Dienst.
create policy actions_select on __SCHEMA__.actions for select to authenticated
  using (user_id = (select auth.uid()));
create policy actions_decide on __SCHEMA__.actions for update to authenticated
  using (user_id = (select auth.uid()) and status = 'pending')
  with check (user_id = (select auth.uid()) and status in ('approved', 'rejected'));
revoke insert, update, delete on __SCHEMA__.actions from authenticated;
grant update (status, decided_at) on __SCHEMA__.actions to authenticated;

-- Stimmabdruecke sind biometrische Daten (DSGVO Art. 9): nicht an die UI
revoke select, insert, update on __SCHEMA__.speakers from authenticated;
grant select (id, user_id, name, is_owner, enrolled_at) on __SCHEMA__.speakers to authenticated;
grant update (name) on __SCHEMA__.speakers to authenticated;

-- Funktionen: kein Zugriff fuer PUBLIC/anon
revoke execute on all functions in schema __SCHEMA__ from public, anon;
grant execute on function __SCHEMA__.match_memories(extensions.vector, int, uuid) to authenticated, service_role;
alter default privileges in schema __SCHEMA__ revoke execute on functions from public;

-- Live-Updates fuer Canvas und Freigaben in der UI
do $$ begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    begin alter publication supabase_realtime add table __SCHEMA__.canvas_items;
      exception when duplicate_object then null; end;
    begin alter publication supabase_realtime add table __SCHEMA__.actions;
      exception when duplicate_object then null; end;
  end if;
end $$;
