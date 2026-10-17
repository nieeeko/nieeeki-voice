"""Gedaechtnis von nieeeki: Sessions, Turns und Lernspeicher in Supabase.

Spricht direkt die PostgREST-API an (service_role, nur serverseitig).
Schema je Umgebung: nieeeki_voice_dev | nieeeki_voice_test | nieeeki_voice_prod.
Ohne SUPABASE_URL laeuft alles als No-Op, damit der Voice-Core auch offline startet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from loguru import logger

EMBED_DIM = 1024  # voyage-3.5 / bge-m3


@dataclass
class MemoryStore:
    url: str | None
    key: str | None
    schema: str
    user_id: str | None
    session_id: str | None = None
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, user_id: str | None = None) -> "MemoryStore":
        return cls(
            url=os.getenv("SUPABASE_URL"),
            key=os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
            schema=os.getenv("NIEEEKI_DB_SCHEMA", "nieeeki_voice_dev"),
            user_id=user_id or os.getenv("NIEEEKI_OWNER_ID"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key and self.user_id)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.url}/rest/v1",
                headers={
                    "apikey": self.key or "",
                    "Authorization": f"Bearer {self.key}",
                    "Accept-Profile": self.schema,
                    "Content-Profile": self.schema,
                    "Prefer": "return=representation",
                },
                timeout=5.0,
            )
        return self._client

    async def _insert(self, table: str, row: dict) -> dict | None:
        if not self.enabled:
            return None
        try:
            r = await self._http().post(f"/{table}", json=row)
            r.raise_for_status()
            return r.json()[0]
        except Exception as e:  # Gedaechtnis darf das Gespraech nie blockieren
            logger.warning("memory insert {} fehlgeschlagen: {}", table, e)
            return None

    async def start_session(self, channel: str = "web") -> None:
        row = await self._insert("sessions", {"user_id": self.user_id, "channel": channel})
        self.session_id = row["id"] if row else None

    async def end_session(self) -> None:
        if not (self.enabled and self.session_id):
            return
        try:
            await self._http().patch(
                f"/sessions?id=eq.{self.session_id}", json={"ended_at": "now()"}
            )
        except Exception as e:
            logger.warning("end_session fehlgeschlagen: {}", e)

    async def log_turn(
        self,
        role: str,
        content: str,
        interrupted: bool = False,
        speaker_label: str | None = None,
    ) -> None:
        if not self.session_id or not content:
            return
        await self._insert(
            "turns",
            {
                "session_id": self.session_id,
                "user_id": self.user_id,
                "role": role,
                "content": content,
                "interrupted": interrupted,
                "speaker_label": speaker_label,
            },
        )

    async def recall_profile(self, limit: int = 40) -> list[str]:
        """Feste Fakten, Vorlieben und Lektionen fuer den System-Prompt."""
        if not self.enabled:
            return []
        try:
            r = await self._http().get(
                "/memories",
                params={
                    "select": "content",
                    "user_id": f"eq.{self.user_id}",
                    "is_active": "eq.true",
                    "kind": "in.(fact,preference,behavior,lesson)",
                    "order": "importance.desc,updated_at.desc",
                    "limit": str(limit),
                },
            )
            r.raise_for_status()
            return [m["content"] for m in r.json()]
        except Exception as e:
            logger.warning("recall_profile fehlgeschlagen: {}", e)
            return []

    async def search(self, embedding: list[float], k: int = 8) -> list[dict]:
        """Semantische Suche ueber pgvector (RPC match_memories)."""
        if not self.enabled:
            return []
        r = await self._http().post(
            "/rpc/match_memories",
            json={"query_embedding": embedding, "match_count": k, "p_user_id": self.user_id},
        )
        r.raise_for_status()
        return r.json()

    async def remember(self, kind: str, content: str, embedding: list[float] | None = None,
                       importance: int = 3) -> bool:
        row = await self._insert(
            "memories",
            {
                "user_id": self.user_id,
                "kind": kind,
                "content": content,
                "embedding": embedding,
                "importance": importance,
                "source_session_id": self.session_id,
            },
        )
        return row is not None

    async def log_action(self, tool: str, params: dict, reason: str, risk: str) -> str | None:
        """Audit-Eintrag fuer eine Aktion, die Nikolaj freigeben muss (Tabelle actions)."""
        row = await self._insert(
            "actions",
            {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "tool": tool,
                "params": params,
                "reason": reason,
                "risk": risk,
                "status": "pending",
            },
        )
        return row["id"] if row else None

    async def update_action(self, action_id: str | None, status: str) -> None:
        if not (self.enabled and action_id):
            return
        try:
            r = await self._http().patch(
                f"/actions?id=eq.{action_id}",
                json={"status": status, "decided_at": datetime.now(timezone.utc).isoformat()},
            )
            r.raise_for_status()
        except Exception as e:
            logger.warning("update_action fehlgeschlagen: {}", e)

    async def save_canvas(self, type_: str, title: str, data: dict) -> str | None:
        """Canvas-Inhalt ablegen (canvas_items), damit er spaeter wieder aufrufbar ist."""
        row = await self._insert(
            "canvas_items",
            {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "type": type_,
                "title": title,
                "data": data,
            },
        )
        return row["id"] if row else None
