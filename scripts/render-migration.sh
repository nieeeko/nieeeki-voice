#!/usr/bin/env bash
# Rendert eine Migration fuer ein Ziel-Schema.
# Nutzung: scripts/render-migration.sh nieeeki_voice_dev [migration.sql] > out.sql
set -euo pipefail
schema="${1:?Schema angeben: nieeeki_voice_dev | nieeeki_voice_test | nieeeki_voice_prod}"
file="${2:-supabase/migrations/0001_nieeeki_voice_init.sql}"
case "$schema" in
  nieeeki_voice_dev|nieeeki_voice_test|nieeeki_voice_prod) ;;
  *) echo "Unbekanntes Schema: $schema" >&2; exit 1 ;;
esac
sed "s/__SCHEMA__/${schema}/g" "$file"
