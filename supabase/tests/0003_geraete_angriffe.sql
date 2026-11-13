-- nieeeki-voice: Angriffstests fuer Migration 0003 (voice_devices).
-- Alles laeuft in EINER Transaktion, die am Ende absichtlich mit einer Exception abbricht (der Bericht steht in der
-- Fehlermeldung); es bleibt nichts zurueck.
-- Ausfuehren: __SCHEMA__ durch nieeeki_voice_dev | nieeeki_voice_test ersetzen (scripts/render-migration.sh passt auch hier),
-- im SQL-Editor des richtigen Projekts (shared-devtest) oder ueber den Supabase-Connector. Erwartet: "Abweichungen: 0".
-- Voraussetzung: voice_devices ist leer; es gibt mindestens einen Nutzer in auth.users (fuer ihn wird kurz ein Profil
-- angelegt, falls keines existiert; auch das wird zurueckgerollt).
-- Nicht gegen PROD laufen lassen, ohne vorher zu pruefen, dass dort keine echten Geraete in voice_devices stehen.

do $$
declare
  r text := '';
  v_owner uuid;
  v_stranger uuid := gen_random_uuid();
  c_owner text; c_aal1 text; c_stale text; c_stranger text; c text;
  t record;
  stmt text; n int; ist text; abw int := 0;
  i int;
begin
  select id into v_owner from auth.users order by created_at limit 1;
  if v_owner is null then raise exception 'kein Nutzer in auth.users'; end if;
  insert into __SCHEMA__.profiles (user_id) values (v_owner) on conflict do nothing;
  if exists (select 1 from __SCHEMA__.voice_devices) then raise exception 'voice_devices nicht leer'; end if;

  c_owner    := json_build_object('sub', v_owner, 'role', 'authenticated', 'aal', 'aal2', 'amr', json_build_array(json_build_object('method','totp','timestamp', extract(epoch from now())::bigint)))::text;
  c_aal1     := json_build_object('sub', v_owner, 'role', 'authenticated', 'aal', 'aal1', 'amr', json_build_array(json_build_object('method','password','timestamp', extract(epoch from now())::bigint)))::text;
  c_stale    := json_build_object('sub', v_owner, 'role', 'authenticated', 'aal', 'aal2', 'amr', json_build_array(json_build_object('method','totp','timestamp', extract(epoch from now())::bigint - 172800)))::text;
  c_stranger := json_build_object('sub', v_stranger, 'role', 'authenticated', 'aal', 'aal2', 'amr', json_build_array(json_build_object('method','totp','timestamp', extract(epoch from now())::bigint)))::text;

  -- (Reihenfolge, Name, Rolle, Claims, erwartet, Statement); @K1@..@K12@ = gueltige, verschiedene Schluessel
  for t in select * from (values
    -- Phase A: Spaltenrechte, RLS, Pruefregeln (Tabelle leer)
    (1,  'A01 Eintragen (key,label)',            'authenticated', 'owner',    'OK:1',    $q$insert into __SCHEMA__.voice_devices (public_key,label) values (@K1@, 'Pixel ü')$q$),
    (2,  'A02 Eintragen ohne Label',             'authenticated', 'owner',    'OK:1',    $q$insert into __SCHEMA__.voice_devices (public_key) values (@K2@)$q$),
    (3,  'A03 status=active setzen',             'authenticated', 'owner',    'E:42501', $q$insert into __SCHEMA__.voice_devices (public_key,label,status) values (@K3@,'x','active')$q$),
    (4,  'A04 fremde user_id setzen',            'authenticated', 'owner',    'E:42501', $q$insert into __SCHEMA__.voice_devices (public_key,label,user_id) values (@K3@,'x','@STRANGER@')$q$),
    (5,  'A05 approved_at setzen',               'authenticated', 'owner',    'E:42501', $q$insert into __SCHEMA__.voice_devices (public_key,approved_at) values (@K3@, now())$q$),
    (6,  'A06 expires_at setzen',                'authenticated', 'owner',    'E:42501', $q$insert into __SCHEMA__.voice_devices (public_key,expires_at) values (@K3@, now() + interval '5 minutes')$q$),
    (7,  'A07 UPDATE label',                     'authenticated', 'owner',    'E:42501', $q$update __SCHEMA__.voice_devices set label = 'neu'$q$),
    (8,  'A08 UPDATE status',                    'authenticated', 'owner',    'E:42501', $q$update __SCHEMA__.voice_devices set status = 'active'$q$),
    (9,  'A09 DELETE',                           'authenticated', 'owner',    'E:42501', $q$delete from __SCHEMA__.voice_devices$q$),
    (10, 'A10 SELECT als Besitzer',              'authenticated', 'owner',    'OK:2',    $q$select id from __SCHEMA__.voice_devices$q$),
    (11, 'A11 SELECT als Fremder',               'authenticated', 'stranger', 'OK:0',    $q$select id from __SCHEMA__.voice_devices$q$),
    (12, 'A12 SELECT mit aal1',                  'authenticated', 'aal1',     'OK:0',    $q$select id from __SCHEMA__.voice_devices$q$),
    (13, 'A13 SELECT mit altem Login',           'authenticated', 'stale',    'OK:0',    $q$select id from __SCHEMA__.voice_devices$q$),
    (14, 'A14 anon SELECT',                      'anon',          'anon',     'E:42501', $q$select id from __SCHEMA__.voice_devices$q$),
    (15, 'A15 anon INSERT',                      'anon',          'anon',     'E:42501', $q$insert into __SCHEMA__.voice_devices (public_key) values (@K3@)$q$),
    (16, 'A16 INSERT mit aal1',                  'authenticated', 'aal1',     'E:42501', $q$insert into __SCHEMA__.voice_devices (public_key) values (@K3@)$q$),
    (17, 'A17 INSERT mit altem Login',           'authenticated', 'stale',    'E:42501', $q$insert into __SCHEMA__.voice_devices (public_key) values (@K3@)$q$),
    (18, 'A18 INSERT als Fremder (ohne Profil)', 'authenticated', 'stranger', 'E:42501', $q$insert into __SCHEMA__.voice_devices (public_key) values (@K3@)$q$),
    (19, 'A19 doppelter Schluessel',             'authenticated', 'owner',    'E:23505', $q$insert into __SCHEMA__.voice_devices (public_key) values (@K1@)$q$),
    (20, 'A20 Schluessel endet auf B',           'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key) values ('B'||repeat('A',85)||'B')$q$),
    (21, 'A21 Schluessel 86 Zeichen',            'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key) values ('B'||repeat('A',84)||'A')$q$),
    (22, 'A22 Schluessel beginnt mit C',         'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key) values ('C'||repeat('A',85)||'A')$q$),
    (23, 'A23 Schluessel mit +',                 'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key) values ('B'||repeat('A',84)||'+'||'A')$q$),
    (24, 'A24 Label leer',                       'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key,label) values (@K3@, '')$q$),
    (25, 'A25 Label 41 Zeichen',                 'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key,label) values (@K3@, repeat('x',41))$q$),
    (26, 'A26 Label mit BEL',                    'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key,label) values (@K3@, 'a'||chr(7))$q$),
    (27, 'A27 Label mit U+0085',                 'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key,label) values (@K3@, 'a'||chr(133))$q$),
    (28, 'A28 Label mit U+202E (RLO)',           'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key,label) values (@K3@, 'a'||chr(8238))$q$),
    (29, 'A29 Label mit U+200B',                 'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key,label) values (@K3@, 'a'||chr(8203))$q$),
    (30, 'A30 Label mit U+FEFF',                 'authenticated', 'owner',    'E:23514', $q$insert into __SCHEMA__.voice_devices (public_key,label) values (@K3@, 'a'||chr(65279))$q$),
    (31, 'A31 purge als authenticated',          'authenticated', 'owner',    'E:42501', $q$select __SCHEMA__.purge_voice_devices()$q$),
    (32, 'A32 purge als anon',                   'anon',          'anon',     'E:42501', $q$select __SCHEMA__.purge_voice_devices()$q$),
    (33, 'A33 Guard-Funktion direkt aufrufen',   'authenticated', 'owner',    'E:42501', $q$select __SCHEMA__.voice_devices_guard()$q$),
    -- Phase B: Obergrenzen (erst aufraeumen)
    (40, 'B00 aufraeumen',                       'postgres',      'anon',     'OK:*',    $q$delete from __SCHEMA__.voice_devices$q$),
    (41, 'B01 sechs wartende in EINEM Statement','authenticated', 'owner',    'E:PT429', $q$insert into __SCHEMA__.voice_devices (public_key,label) select 'B'||repeat('A',84)||chr(64+g)||'A', 'g'||g from generate_series(1,6) g$q$),
    (42, 'B02 danach keine Zeile uebrig',        'service_role',  'service',  'OK:0',    $q$select id from __SCHEMA__.voice_devices$q$),
    (43, 'B03 wartende 1',                       'authenticated', 'owner',    'OK:1',    $q$insert into __SCHEMA__.voice_devices (public_key) values (@K1@)$q$),
    (44, 'B04 wartende 2',                       'authenticated', 'owner',    'OK:1',    $q$insert into __SCHEMA__.voice_devices (public_key) values (@K2@)$q$),
    (45, 'B05 wartende 3',                       'authenticated', 'owner',    'OK:1',    $q$insert into __SCHEMA__.voice_devices (public_key) values (@K3@)$q$),
    (46, 'B06 wartende 4',                       'authenticated', 'owner',    'OK:1',    $q$insert into __SCHEMA__.voice_devices (public_key) values (@K4@)$q$),
    (47, 'B07 wartende 5',                       'authenticated', 'owner',    'OK:1',    $q$insert into __SCHEMA__.voice_devices (public_key) values (@K5@)$q$),
    (48, 'B08 wartende 6 (Grenze 5)',            'authenticated', 'owner',    'E:PT429', $q$insert into __SCHEMA__.voice_devices (public_key) values (@K6@)$q$),
    -- Phase C: Gesamtgrenze 10
    (50, 'C00 aufraeumen',                       'postgres',      'anon',     'OK:*',    $q$delete from __SCHEMA__.voice_devices$q$),
    (51, 'C01 zehn aktive anlegen',              'postgres',      'anon',     'OK:10',   $q$insert into __SCHEMA__.voice_devices (user_id,public_key,status,approved_at) select '@OWNER@', 'B'||repeat('A',84)||chr(64+g)||'A', 'active', now() from generate_series(1,10) g$q$),
    (52, 'C02 elftes Geraet (Gesamtgrenze)',     'authenticated', 'owner',    'E:PT429', $q$insert into __SCHEMA__.voice_devices (public_key) values (@K11@)$q$),
    -- Phase D: Freigabe, Ablauf, Aufraeumen (service_role)
    (60, 'D00 aufraeumen',                       'postgres',      'anon',     'OK:*',    $q$delete from __SCHEMA__.voice_devices$q$),
    (61, 'D01 service_role legt wartende an',    'service_role',  'service',  'OK:1',    $q$insert into __SCHEMA__.voice_devices (user_id,public_key) values ('@OWNER@', @K1@)$q$),
    (62, 'D02 freigeben (falsches approved_at)', 'service_role',  'service',  'OK:1',    $q$update __SCHEMA__.voice_devices set status='active', approved_at='2000-01-01' where public_key=@K1@$q$),
    (63, 'D03 approved_at kommt von der DB-Uhr', 'service_role',  'service',  'OK:1',    $q$select id from __SCHEMA__.voice_devices where public_key=@K1@ and status='active' and approved_at > now() - interval '1 minute'$q$),
    (64, 'D04 active ohne approved_at',          'service_role',  'service',  'E:23514', $q$insert into __SCHEMA__.voice_devices (user_id,public_key,status) values ('@OWNER@', @K2@, 'active')$q$),
    (65, 'D05 pending mit approved_at',          'service_role',  'service',  'E:23514', $q$insert into __SCHEMA__.voice_devices (user_id,public_key,approved_at) values ('@OWNER@', @K2@, now())$q$),
    (66, 'D06 unbekannter Status',               'service_role',  'service',  'E:23514', $q$insert into __SCHEMA__.voice_devices (user_id,public_key,status) values ('@OWNER@', @K2@, 'foo')$q$),
    (67, 'D07 Ablauf spaeter als 10 Minuten',    'service_role',  'service',  'E:23514', $q$insert into __SCHEMA__.voice_devices (user_id,public_key,expires_at) values ('@OWNER@', @K2@, now() + interval '1 day')$q$),
    (68, 'D08 abgelaufene wartende anlegen',     'service_role',  'service',  'OK:1',    $q$insert into __SCHEMA__.voice_devices (user_id,public_key,created_at,expires_at) values ('@OWNER@', @K4@, now() - interval '20 minutes', now() - interval '10 minutes')$q$),
    (69, 'D09 abgelaufene NICHT freigeben',      'service_role',  'service',  'OK:0',    $q$update __SCHEMA__.voice_devices set status='active' where public_key=@K4@$q$),
    (70, 'D10 sie bleibt wartend',               'service_role',  'service',  'OK:1',    $q$select id from __SCHEMA__.voice_devices where public_key=@K4@ and status='pending'$q$),
    (71, 'D11 purge raeumt genau 1 auf',         'service_role',  'service',  'OK:1',    $q$select 1 from (select __SCHEMA__.purge_voice_devices() p) q where p = 1$q$),
    (72, 'D12 weitere abgelaufene anlegen',      'service_role',  'service',  'OK:1',    $q$insert into __SCHEMA__.voice_devices (user_id,public_key,created_at,expires_at) values ('@OWNER@', @K5@, now() - interval '20 minutes', now() - interval '10 minutes')$q$),
    (73, 'D13 neuer Eintrag raeumt sie auf',     'authenticated', 'owner',    'OK:1',    $q$insert into __SCHEMA__.voice_devices (public_key) values (@K6@)$q$),
    (74, 'D14 abgelaufene ist weg',              'service_role',  'service',  'OK:0',    $q$select id from __SCHEMA__.voice_devices where public_key=@K5@$q$)
  ) as v(ord, name, rolle, claims, erwartet, stmt) order by ord loop
    begin
      if t.rolle = 'postgres' then
        execute 'reset role';
      else
        execute 'set local role ' || t.rolle;
      end if;
      c := case t.claims when 'owner' then c_owner when 'aal1' then c_aal1 when 'stale' then c_stale
                         when 'stranger' then c_stranger when 'service' then '{"role":"service_role"}' else '{"role":"anon"}' end;
      perform set_config('request.jwt.claims', c, true);
      perform set_config('request.jwt.claim.sub', coalesce(c::json->>'sub', ''), true);
      stmt := t.stmt;
      stmt := replace(stmt, '@OWNER@', v_owner::text);
      stmt := replace(stmt, '@STRANGER@', v_stranger::text);
      for i in 1..12 loop
        stmt := replace(stmt, '@K' || i || '@', quote_literal('B' || repeat('A', 84) || chr(64 + i) || 'A'));
      end loop;
      execute stmt;
      get diagnostics n = row_count;
      ist := 'OK:' || n;
    exception when others then
      ist := 'E:' || sqlstate;
    end;
    execute 'reset role';
    if ist = t.erwartet or (t.erwartet = 'OK:*' and ist like 'OK:%') then
      r := r || chr(10) || 'ok         ' || t.name || ' -> ' || ist;
    else
      abw := abw + 1;
      r := r || chr(10) || 'ABWEICHUNG ' || t.name || ' -> ' || ist || ' (erwartet ' || t.erwartet || ')';
    end if;
  end loop;

  raise exception E'ERGEBNIS (Abweichungen: %)%', abw, r;
end $$;
