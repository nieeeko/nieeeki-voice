"""Direkte Verbindungen zu GitHub, Vercel und Supabase: ohne Claude Code und ohne Klicken im Browser.

21.10.: Ein Repository per Klick auf github.com anzulegen dauerte 20 Minuten, und ueber Claude Code
ging nicht alles. Hier spricht nieeeki die Dienste direkt an: GitHub ueber die angemeldete
gh-Kommandozeile (Schluesselbund), Vercel ueber VERCEL_API_TOKEN, Supabase ueber die Management-API
(SUPABASE_ACCESS_TOKEN). Zugangsdaten stehen nie in Ergebnissen oder im Audit.

Drei Werkzeuge mit festen Aktionen. Die Risikoklasse steht je Aktion HIER, nicht im Prompt:
  read      sofort (Listen, Status, Fehlerlogs, SQL nur lesend, von Supabase erzwungen)
  change    sofort, mit Audit-Eintrag (Issue, Branch, Pull Request anlegen, Preview neu bauen)
  critical  nur eine Freigabe-Karte in der Oberflaeche; ausgefuehrt erst nach Nikolajs Klick
            (Pull Request mergen, Sichtbarkeit, Produktion, schreibendes SQL)
  blocked   nie (Loeschen, Umgebungsvariablen und Secrets, Schreiben auf PROD-Datenbanken)
Freigaben kann das Modell nicht selbst erteilen: decide() ruft nur bot.py nach einem UI-Klick auf.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

AUDIT_LOG = Path(__file__).resolve().parent / ".jobs" / "cloud_audit.jsonl"
MAX_RESULT_CHARS = 4000
APPROVAL_TTL_SECS = 30 * 60
APPROVAL_ID = re.compile(r"^[0-9a-f]{8}$")
OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
BRANCH = re.compile(r"^(?![-/.])(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])$")
VERCEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
VERCEL_DEPLOYMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,150}$")
SQL_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MAX_SQL_CHARS = 4000
PROD_HINTS = ("prod", "live")
READ_SQL_START = ("select", "with", "explain", "show", "table", "values")
WRITE_SQL = re.compile(
    r"\b(insert|update|delete|merge|upsert|create|alter|drop|truncate|grant|revoke|comment|copy|call|do|"
    r"vacuum|reindex|cluster|refresh|set|reset|lock|listen|notify|security|begin|commit|rollback|into)\b",
    re.IGNORECASE,
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")

SERVICES = {"github": "GitHub", "vercel": "Vercel", "supabase": "Supabase"}
RISK: dict[str, dict[str, str]] = {
    "github": {"repos": "read", "status": "read", "prs": "read", "pr": "read", "issues": "read",
               "issue_anlegen": "change", "branch_anlegen": "change", "pr_anlegen": "change",
               "pr_mergen": "critical", "sichtbarkeit": "critical"},
    "vercel": {"projekte": "read", "deployments": "read", "deployment": "read",
               "neu_bauen": "change", "produktion": "critical"},
    # sql: lesend sofort, schreibend Freigabe, auf PROD gesperrt (entscheidet _supabase_sql)
    "supabase": {"projekte": "read", "tabellen": "read", "sql": "read"},
}


class CloudError(Exception):
    """Fehler mit einem Satz, den nieeeki Nikolaj so sagen kann."""


@dataclass
class Proposal:
    """Kritische Aktion: wird erst nach Nikolajs Klick ausgefuehrt."""

    grund: str
    detail: str
    run: Callable[[], Awaitable[dict]]


# ---------- Ausfuehrung (im Test ersetzt) ----------
async def run_gh(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    exe = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    if not Path(exe).is_file():
        raise CloudError("Die GitHub-Kommandozeile (gh) ist auf dem Laptop nicht installiert.")

    # Ohne GITHUB_TOKEN/GH_TOKEN aus der .env: gh nimmt sonst diese statt der Anmeldung im
    # Schluesselbund (21.10.: der Token in der .env war ungueltig, jede Abfrage scheiterte mit 401).
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}

    def go() -> tuple[int, str, str]:
        # argv-Liste, keine Shell: Titel, Texte und Namen werden nie als Befehl gelesen
        p = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr

    try:
        return await asyncio.to_thread(go)
    except subprocess.TimeoutExpired as e:
        raise CloudError("GitHub antwortet nicht (Zeitueberschreitung).") from e


async def http_json(method: str, url: str, headers: dict, body: object = None, timeout: int = 20) -> tuple[int, object]:
    def go() -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            **headers, "Content-Type": "application/json", "User-Agent": "nieeeki"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, json.loads(raw or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"null")
            except ValueError:
                return e.code, {"message": raw[:200].decode(errors="replace")}

    try:
        return await asyncio.to_thread(go)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise CloudError("Der Dienst ist gerade nicht erreichbar.") from e


def _date(value: object) -> str:
    """ISO-Zeit oder Millisekunden -> '21.10. 18:51' (Ortszeit)."""
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d.%m. %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _short(text: object, limit: int = 120) -> str:
    s = ANSI.sub("", str(text or "")).replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def is_read_sql(sql: str) -> bool:
    """Grob vorsortiert: sieht die Abfrage rein lesend aus? (Supabase erzwingt read_only zusaetzlich.)"""
    s = sql.strip().rstrip(";").strip()
    if not s or ";" in s:
        return False  # mehrere Anweisungen: nie als "lesend" durchwinken
    first = s.split(None, 1)[0].lower()
    return first in READ_SQL_START and not WRITE_SQL.search(s)


def is_prod(project: dict) -> bool:
    name = str(project.get("name", "")).lower()
    return any(hint in name for hint in PROD_HINTS)


class CloudTools:
    """Die Werkzeuge github, vercel und supabase; Freigaben ueber decide()."""

    def __init__(self, gh=None, http=None, env: dict | None = None):
        self._gh = gh or run_gh
        self._http = http or http_json
        self._env = env if env is not None else os.environ
        self._pending: dict[str, dict] = {}
        self._login: str | None = None

    # ---------- Werkzeuge fuer Claude ----------
    def schemas(self) -> list[FunctionSchema]:
        return [
            FunctionSchema(
                name="github",
                description=(
                    "Direkt auf GitHub (Nikolajs Konto) ohne Browser und ohne Claude Code. Lesen: repos, status "
                    "(Haupt-Branch, offene Pull Requests, letzte Workflow-Laeufe), prs, pr, issues. Sofort: "
                    "issue_anlegen, branch_anlegen, pr_anlegen. Nur nach Freigabe-Klick: pr_mergen, sichtbarkeit. "
                    "Loeschen geht nicht. Neues Repository: github_repo_anlegen."
                ),
                properties={
                    "aktion": {"type": "string", "enum": list(RISK["github"])},
                    "repo": {"type": "string", "description": "Name (z. B. radar) oder besitzer/name"},
                    "nummer": {"type": "integer", "description": "Nummer des Pull Requests (pr, pr_mergen)"},
                    "titel": {"type": "string", "description": "Titel fuer Issue oder Pull Request"},
                    "text": {"type": "string", "description": "Beschreibung fuer Issue oder Pull Request"},
                    "branch": {"type": "string", "description": "Branch fuer branch_anlegen oder pr_anlegen"},
                    "basis": {"type": "string", "description": "Ziel-/Ausgangs-Branch, ohne Angabe der Haupt-Branch"},
                    "wert": {"type": "string", "description": "sichtbarkeit: privat oder oeffentlich"},
                },
                required=["aktion"],
            ),
            FunctionSchema(
                name="vercel",
                description=(
                    "Direkt auf Vercel ohne Browser. Lesen: projekte, deployments (eines Projekts), deployment "
                    "(Zustand und bei Fehlern die Fehlerzeilen aus dem Build-Log). Sofort: neu_bauen (Preview). "
                    "Nur nach Freigabe-Klick: produktion (ein Deployment live schalten) und neu_bauen von "
                    "Produktion. Umgebungsvariablen und Loeschen gehen nicht."
                ),
                properties={
                    "aktion": {"type": "string", "enum": list(RISK["vercel"])},
                    "projekt": {"type": "string", "description": "Projektname, z. B. nieeeki-voice"},
                    "deployment": {"type": "string", "description": "Deployment-ID (dpl_...) oder Adresse"},
                },
                required=["aktion"],
            ),
            FunctionSchema(
                name="supabase",
                description=(
                    "Direkt auf Supabase ohne Browser. projekte, tabellen (eines Schemas), sql: lesende Abfragen "
                    "laufen sofort und nur lesend; schreibendes SQL braucht Nikolajs Freigabe-Klick, auf "
                    "PROD-Projekten ist Schreiben gesperrt."
                ),
                properties={
                    "aktion": {"type": "string", "enum": list(RISK["supabase"])},
                    "projekt": {"type": "string", "description": "Projektname oder Ref"},
                    "sql": {"type": "string", "description": "Eine SQL-Anweisung"},
                    "schema": {"type": "string", "description": "Schema fuer tabellen, Standard public"},
                },
                required=["aktion"],
            ),
        ]

    def register(self, llm) -> None:
        for service in SERVICES:
            async def handler(params: FunctionCallParams, service: str = service):
                await params.result_callback(await self.call(service, params.arguments))

            llm.register_function(service, handler)

    # ---------- Ablauf ----------
    async def call(self, service: str, args: object) -> dict:
        args = args if isinstance(args, dict) else {}
        aktion = str(args.get("aktion", "")).strip().lower()
        actions = RISK.get(service, {})
        if aktion not in actions:
            self._audit(service, aktion, args, "rejected", "unbekannte Aktion")
            return {"status": "rejected", "summary": f"Moeglich: {', '.join(actions)}. Loeschen und Secrets gehen nicht."}
        try:
            result = await getattr(self, f"_{service}_{aktion}")(args)
        except CloudError as e:
            self._audit(service, aktion, args, "error", str(e))
            return {"status": "error", "summary": str(e)}
        if isinstance(result, Proposal):
            approval_id = uuid.uuid4().hex[:8]
            self._pending[approval_id] = {"service": service, "aktion": aktion, "proposal": result,
                                          "at": time.monotonic()}
            self._audit(service, aktion, args, "needs_approval", result.detail, approval_id=approval_id)
            return {"status": "needs_approval", "approval_id": approval_id, "job": "cloud",
                    "dienst": SERVICES[service], "grund": result.grund, "detail": result.detail,
                    "summary": "Liegt als Freigabe-Karte in der Oberflaeche. Erst nach Nikolajs Klick passiert es."}
        self._audit(service, aktion, args, str(result.get("status")), result.get("summary", ""))
        return self._limit(result)

    async def decide(self, approval_id: object, ja: bool) -> dict:
        """Nikolajs Klick auf eine Freigabe-Karte (nur aus bot.py, nie vom Modell)."""
        aid = approval_id if isinstance(approval_id, str) and APPROVAL_ID.fullmatch(approval_id) else ""
        entry = self._pending.pop(aid, None)
        if not entry or time.monotonic() - entry["at"] > APPROVAL_TTL_SECS:
            return {"status": "error", "summary": "Unbekannte oder abgelaufene Freigabe."}
        proposal: Proposal = entry["proposal"]
        if not ja:
            self._audit(entry["service"], entry["aktion"], {}, "rejected", proposal.detail, approval_id=aid)
            return {"status": "rejected", "summary": "Abgelehnt, es wurde nichts ausgefuehrt."}
        try:
            result = await proposal.run()
        except CloudError as e:
            result = {"status": "error", "summary": str(e)}
        self._audit(entry["service"], entry["aktion"], {}, str(result.get("status")), proposal.detail, approval_id=aid)
        return self._limit(result)

    def _limit(self, result: dict) -> dict:
        """Ergebnisse klein halten (Gespraechskontext) und nie Zugangsdaten durchreichen."""
        text = json.dumps(result, ensure_ascii=False)
        for key in ("VERCEL_API_TOKEN", "SUPABASE_ACCESS_TOKEN", "GITHUB_TOKEN"):
            secret = self._env.get(key)
            if secret and len(secret) >= 8 and secret in text:
                text = text.replace(secret, "***")
                result = json.loads(text)
        if len(text) <= MAX_RESULT_CHARS:
            return result
        head = {k: v for k, v in result.items() if not isinstance(v, (list, dict))}
        return {**head, "gekuerzt": True, "auszug": text[:MAX_RESULT_CHARS]}

    def _audit(self, service: str, aktion: str, args: dict, status: str, note: object, **extra) -> None:
        event = {"ts": datetime.now(timezone.utc).isoformat(), "dienst": service, "aktion": aktion,
                 "risk": RISK.get(service, {}).get(aktion, "?"), "status": status, "notiz": _short(note, 300),
                 "args": {k: _short(v, 200) for k, v in args.items() if k != "aktion"}, **extra}
        try:
            AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("Cloud-Audit nicht geschrieben: {}", e)

    # ---------- GitHub ----------
    async def _gh_json(self, args: list[str]) -> object:
        code, out, err = await self._gh(args)
        if code != 0:
            raise CloudError(self._gh_error(err))
        return json.loads(out) if out.strip() else None

    @staticmethod
    def _gh_error(err: str) -> str:
        low = err.lower()
        if "could not resolve to a repository" in low or "not found" in low:
            return "Das gibt es auf GitHub nicht, oder es fehlt der Zugriff."
        if "auth login" in low or "authentication" in low:
            return "GitHub-Anmeldung fehlt: am Laptop einmal 'gh auth login'."
        first = next((line for line in err.splitlines() if line.strip()), "unbekannter Fehler")
        return f"GitHub meldet: {_short(first, 200)}"

    async def _owner(self) -> str:
        if not self._login:
            code, out, _ = await self._gh(["api", "user", "--jq", ".login"])
            if code != 0 or not out.strip():
                raise CloudError("GitHub-Anmeldung fehlt: am Laptop einmal 'gh auth login'.")
            self._login = out.strip()
        return self._login

    async def _repo(self, spec: object) -> str:
        s = str(spec or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if not s:
            raise CloudError("Welches Repository? Der Name fehlt.")
        owner, _, name = s.partition("/") if "/" in s else (await self._owner(), "", s)
        if not OWNER.fullmatch(owner) or not REPO.fullmatch(name) or name.lower().endswith(".git"):
            raise CloudError("Das ist kein gueltiger Repository-Name.")
        return f"{owner}/{name}"

    async def _default_branch(self, repo: str) -> str:
        info = await self._gh_json(["repo", "view", repo, "--json", "defaultBranchRef"])
        name = ((info or {}).get("defaultBranchRef") or {}).get("name")
        if not name:
            raise CloudError("Das Repository ist noch leer, es hat keinen Haupt-Branch.")
        return name

    @staticmethod
    def _text(value: object, limit: int, what: str) -> str:
        s = str(value or "").strip()
        if not s:
            raise CloudError(f"{what} fehlt.")
        if len(s) > limit:
            raise CloudError(f"{what} ist zu lang (hoechstens {limit} Zeichen).")
        return s

    async def _own_repo(self, spec: object) -> str:
        """Schreiben nur in Nikolajs eigenen Repositorys: Text aus fremden Issues oder Webseiten soll
        nieeeki nicht dazu bringen, anderswo Issues oder Pull Requests anzulegen."""
        repo = await self._repo(spec)
        if repo.split("/", 1)[0].lower() != (await self._owner()).lower():
            raise CloudError("Aendern geht nur in deinen eigenen Repositorys; lesen geht ueberall.")
        return repo

    @staticmethod
    def _checks(rollup: object) -> list[dict]:
        out = []
        for c in rollup if isinstance(rollup, list) else []:
            out.append({"name": _short(c.get("name") or c.get("context"), 60),
                        "ergebnis": str(c.get("conclusion") or c.get("state") or c.get("status") or "").lower()})
        return out[:10]

    async def _github_repos(self, a: dict) -> dict:
        data = await self._gh_json(["repo", "list", "--limit", "30", "--json", "name,visibility,updatedAt,description"])
        return {"status": "success", "repos": [
            {"name": r["name"], "sichtbarkeit": str(r.get("visibility", "")).lower(),
             "aktualisiert": _date(r.get("updatedAt")), "beschreibung": _short(r.get("description"), 80)}
            for r in data or []]}

    async def _github_status(self, a: dict) -> dict:
        repo = await self._repo(a.get("repo"))
        info = await self._gh_json(["repo", "view", repo, "--json", "visibility,defaultBranchRef,url,pushedAt"])
        prs = await self._gh_json(["pr", "list", "-R", repo, "--state", "open", "--limit", "10",
                                   "--json", "number,title,headRefName"])
        runs = await self._gh_json(["run", "list", "-R", repo, "--limit", "3",
                                    "--json", "workflowName,status,conclusion,headBranch,createdAt"])
        return {"status": "success", "repo": repo, "url": info.get("url"),
                "sichtbarkeit": str(info.get("visibility", "")).lower(),
                "haupt_branch": (info.get("defaultBranchRef") or {}).get("name") or "(leer)",
                "letzter_push": _date(info.get("pushedAt")),
                "offene_prs": [{"nummer": p["number"], "titel": _short(p["title"], 80), "branch": p["headRefName"]}
                               for p in prs or []],
                "workflows": [{"name": _short(r.get("workflowName"), 40), "branch": r.get("headBranch"),
                               "ergebnis": str(r.get("conclusion") or r.get("status") or "").lower(),
                               "zeit": _date(r.get("createdAt"))} for r in runs or []]}

    async def _github_prs(self, a: dict) -> dict:
        repo = await self._repo(a.get("repo"))
        prs = await self._gh_json(["pr", "list", "-R", repo, "--state", "open", "--limit", "15",
                                   "--json", "number,title,headRefName,isDraft"])
        return {"status": "success", "repo": repo, "prs": [
            {"nummer": p["number"], "titel": _short(p["title"], 80), "branch": p["headRefName"],
             "entwurf": bool(p.get("isDraft"))} for p in prs or []]}

    async def _pr_info(self, repo: str, nummer: object) -> dict:
        try:
            n = int(nummer)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            n = 0
        if n < 1:
            raise CloudError("Welcher Pull Request? Die Nummer fehlt.")
        fields = "number,title,state,mergeable,mergeStateStatus,headRefName,baseRefName,url,statusCheckRollup"
        return await self._gh_json(["pr", "view", str(n), "-R", repo, "--json", fields])

    async def _github_pr(self, a: dict) -> dict:
        repo = await self._repo(a.get("repo"))
        p = await self._pr_info(repo, a.get("nummer"))
        return {"status": "success", "repo": repo, "nummer": p["number"], "titel": _short(p["title"], 120),
                "zustand": str(p.get("state", "")).lower(), "mergebar": str(p.get("mergeable", "")).lower(),
                "von": p.get("headRefName"), "nach": p.get("baseRefName"), "url": p.get("url"),
                "checks": self._checks(p.get("statusCheckRollup"))}

    async def _github_issues(self, a: dict) -> dict:
        repo = await self._repo(a.get("repo"))
        issues = await self._gh_json(["issue", "list", "-R", repo, "--state", "open", "--limit", "15",
                                      "--json", "number,title"])
        return {"status": "success", "repo": repo,
                "issues": [{"nummer": i["number"], "titel": _short(i["title"], 80)} for i in issues or []]}

    async def _github_issue_anlegen(self, a: dict) -> dict:
        repo = await self._own_repo(a.get("repo"))
        titel = self._text(a.get("titel"), 200, "Der Titel")
        body = str(a.get("text") or "")[:4000]
        code, out, err = await self._gh(["issue", "create", "-R", repo, "--title", titel, "--body", body])
        if code != 0:
            raise CloudError(self._gh_error(err))
        return {"status": "success", "summary": f"Issue in {repo} angelegt", "url": out.strip()}

    async def _github_branch_anlegen(self, a: dict) -> dict:
        repo = await self._own_repo(a.get("repo"))
        branch = str(a.get("branch") or "").strip()
        if not BRANCH.fullmatch(branch):
            raise CloudError("Der Branch-Name fehlt oder ist ungueltig.")
        basis = str(a.get("basis") or "").strip() or await self._default_branch(repo)
        if not BRANCH.fullmatch(basis):
            raise CloudError("Der Ausgangs-Branch ist ungueltig.")
        code, sha, err = await self._gh(["api", f"repos/{repo}/git/ref/heads/{basis}", "--jq", ".object.sha"])
        if code != 0 or not sha.strip():
            raise CloudError(f"Den Branch {basis} gibt es nicht (oder das Repository ist leer).")
        code, _, err = await self._gh(["api", "-X", "POST", f"repos/{repo}/git/refs",
                                       "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha.strip()}"])
        if code != 0:
            raise CloudError("Den Branch gibt es schon." if "already exists" in err.lower() else self._gh_error(err))
        return {"status": "success", "summary": f"Branch {branch} in {repo} angelegt (von {basis})"}

    async def _github_pr_anlegen(self, a: dict) -> dict:
        repo = await self._own_repo(a.get("repo"))
        head = str(a.get("branch") or "").strip()
        if not BRANCH.fullmatch(head):
            raise CloudError("Von welchem Branch? Der Name fehlt oder ist ungueltig.")
        basis = str(a.get("basis") or "").strip() or await self._default_branch(repo)
        if not BRANCH.fullmatch(basis):
            raise CloudError("Der Ziel-Branch ist ungueltig.")
        titel = self._text(a.get("titel"), 200, "Der Titel")
        body = str(a.get("text") or "")[:4000]
        code, out, err = await self._gh(["pr", "create", "-R", repo, "--head", head, "--base", basis,
                                         "--title", titel, "--body", body])
        if code != 0:
            raise CloudError(self._gh_error(err))
        return {"status": "success", "summary": f"Pull Request {head} nach {basis} angelegt", "url": out.strip()}

    async def _github_pr_mergen(self, a: dict) -> Proposal:
        repo = await self._own_repo(a.get("repo"))
        p = await self._pr_info(repo, a.get("nummer"))
        if str(p.get("state", "")).upper() != "OPEN":
            raise CloudError(f"Pull Request {p['number']} ist nicht mehr offen.")
        n = str(p["number"])

        async def run() -> dict:
            code, _, err = await self._gh(["pr", "merge", n, "-R", repo, "--merge"])
            if code != 0:
                raise CloudError(self._gh_error(err))
            return {"status": "success", "summary": f"Pull Request {n} in {repo} ist gemergt"}

        return Proposal(grund=f"Pull Request {n} mergen", detail=f"{repo} #{n}: {_short(p['title'], 100)} "
                        f"({p.get('headRefName')} -> {p.get('baseRefName')})", run=run)

    async def _github_sichtbarkeit(self, a: dict) -> Proposal:
        repo = await self._own_repo(a.get("repo"))
        wert = str(a.get("wert") or "").strip().lower().replace("ö", "oe")
        visibility = {"privat": "private", "private": "private", "oeffentlich": "public", "public": "public"}.get(wert)
        if not visibility:
            raise CloudError("Sichtbarkeit: privat oder oeffentlich?")

        async def run() -> dict:
            code, _, err = await self._gh(["repo", "edit", repo, "--visibility", visibility,
                                           "--accept-visibility-change-consequences"])
            if code != 0:
                raise CloudError(self._gh_error(err))
            return {"status": "success", "summary": f"{repo} ist jetzt {wert}"}

        return Proposal(grund=f"{repo} {wert} stellen", detail=f"Sichtbarkeit von {repo} auf {visibility}", run=run)

    # ---------- Vercel ----------
    async def _vercel(self, method: str, path: str, body: object = None) -> object:
        token = self._env.get("VERCEL_API_TOKEN")
        if not token:
            raise CloudError("Der Vercel-Zugang fehlt (VERCEL_API_TOKEN in der .env).")
        if team := self._env.get("VERCEL_TEAM_ID"):
            path += ("&" if "?" in path else "?") + f"teamId={team}"
        status, data = await self._http(method, "https://api.vercel.com" + path, {"Authorization": f"Bearer {token}"}, body)
        if status == 404:
            raise CloudError("Das gibt es auf Vercel nicht.")
        if status >= 400:
            msg = (data or {}).get("error", {}).get("message") if isinstance(data, dict) else None
            raise CloudError(f"Vercel meldet: {_short(msg or status, 160)}")
        return data

    @staticmethod
    def _name(value: object, pattern: re.Pattern, what: str) -> str:
        s = str(value or "").strip().removeprefix("https://").strip("/")
        if not pattern.fullmatch(s):
            raise CloudError(f"{what} fehlt oder ist ungueltig.")
        return s

    async def _vercel_projekte(self, a: dict) -> dict:
        data = await self._vercel("GET", "/v9/projects?limit=20")
        out = []
        for p in (data or {}).get("projects", []):
            prod = (p.get("targets") or {}).get("production") or {}
            out.append({"name": p.get("name"), "framework": p.get("framework"),
                        "produktion": (prod.get("alias") or [prod.get("url")])[0],
                        "aktualisiert": _date(p.get("updatedAt"))})
        return {"status": "success", "projekte": out}

    async def _vercel_deployments(self, a: dict) -> dict:
        name = self._name(a.get("projekt"), VERCEL_NAME, "Der Projektname")
        project = await self._vercel("GET", f"/v9/projects/{name}")
        data = await self._vercel("GET", f"/v6/deployments?projectId={project['id']}&limit=6")
        return {"status": "success", "projekt": name, "deployments": [
            {"id": d.get("uid"), "url": d.get("url"), "zustand": str(d.get("state") or d.get("readyState") or "").lower(),
             "ziel": d.get("target") or "preview", "branch": (d.get("meta") or {}).get("githubCommitRef"),
             "commit": _short((d.get("meta") or {}).get("githubCommitMessage"), 60), "zeit": _date(d.get("created"))}
            for d in (data or {}).get("deployments", [])]}

    async def _deployment(self, value: object) -> dict:
        ident = self._name(value, VERCEL_DEPLOYMENT, "Das Deployment")
        return await self._vercel("GET", f"/v13/deployments/{ident}")

    async def _vercel_deployment(self, a: dict) -> dict:
        d = await self._deployment(a.get("deployment"))
        state = str(d.get("readyState") or d.get("status") or "").lower()
        result = {"status": "success", "id": d.get("id"), "projekt": d.get("name"), "url": d.get("url"),
                  "zustand": state, "ziel": d.get("target") or "preview",
                  "branch": (d.get("meta") or {}).get("githubCommitRef"), "fehler": _short(d.get("errorMessage"), 200)}
        if state == "error":
            events = await self._vercel("GET", f"/v3/deployments/{d.get('id')}/events?limit=200")
            lines = []
            for e in events if isinstance(events, list) else []:
                text = _short((e.get("payload") or {}).get("text") or e.get("text"), 200)
                if text and (e.get("type") == "stderr" or re.search(r"error|failed|fehler", text, re.IGNORECASE)):
                    lines.append(text)
            result["build_fehler"] = lines[-12:]
        return result

    async def _vercel_neu_bauen(self, a: dict) -> dict | Proposal:
        d = await self._deployment(a.get("deployment"))
        body = {"name": d.get("name"), "deploymentId": d.get("id")}

        async def run(target: str | None = None) -> dict:
            new = await self._vercel("POST", "/v13/deployments?forceNew=1", {**body, **({"target": target} if target else {})})
            return {"status": "success", "summary": "Neuer Build gestartet", "url": new.get("url"), "id": new.get("id")}

        if d.get("target") == "production":
            return Proposal(grund=f"Produktion von {d.get('name')} neu bauen",
                            detail=f"{d.get('name')}: Deployment {d.get('url')} neu bauen und live schalten",
                            run=lambda: run("production"))
        return await run()

    async def _vercel_produktion(self, a: dict) -> Proposal:
        d = await self._deployment(a.get("deployment"))
        if str(d.get("readyState", "")).upper() != "READY":
            raise CloudError("Nur ein fertiges Deployment (READY) kann live gehen.")
        project_id, dep_id = d.get("projectId"), d.get("id")

        async def run() -> dict:
            await self._vercel("POST", f"/v10/projects/{project_id}/promote/{dep_id}")
            return {"status": "success", "summary": f"{d.get('url')} ist jetzt die Produktion von {d.get('name')}"}

        branch = (d.get("meta") or {}).get("githubCommitRef") or "?"
        return Proposal(grund=f"{d.get('name')} live schalten", detail=f"Deployment {d.get('url')} (Branch {branch}) "
                        f"wird Produktion von {d.get('name')}", run=run)

    # ---------- Supabase ----------
    async def _supabase(self, method: str, path: str, body: object = None) -> object:
        token = self._env.get("SUPABASE_ACCESS_TOKEN")
        if not token:
            raise CloudError("Der Supabase-Zugang fehlt: Nikolaj muss einmal einen Access Token anlegen (supabase.com, "
                             "Konto, Access Tokens) und als SUPABASE_ACCESS_TOKEN in die .env eintragen.")
        status, data = await self._http(method, "https://api.supabase.com" + path, {"Authorization": f"Bearer {token}"}, body)
        if status >= 400:
            msg = data.get("message") if isinstance(data, dict) else None
            raise CloudError(f"Supabase meldet: {_short(msg or status, 200)}")
        return data

    async def _project(self, spec: object) -> dict:
        projects = await self._supabase("GET", "/v1/projects") or []
        s = str(spec or "").strip().lower()
        if not s and len(projects) == 1:
            return projects[0]
        exact = [p for p in projects if s and s in (str(p.get("name", "")).lower(), str(p.get("id", "")).lower())]
        match = exact or [p for p in projects if s and s in str(p.get("name", "")).lower()]
        if len(match) != 1:
            raise CloudError("Welches Supabase-Projekt? Moeglich: " + ", ".join(str(p.get("name")) for p in projects))
        return match[0]

    async def _query(self, project: dict, sql: str, read_only: bool) -> dict:
        data = await self._supabase("POST", f"/v1/projects/{project['id']}/database/query",
                                    {"query": sql, "read_only": read_only})
        rows = data if isinstance(data, list) else []
        return {"status": "success", "projekt": project.get("name"), "anzahl": len(rows),
                "zeilen": [{k: _short(v, 120) for k, v in r.items()} for r in rows[:30] if isinstance(r, dict)]}

    async def _supabase_projekte(self, a: dict) -> dict:
        projects = await self._supabase("GET", "/v1/projects") or []
        return {"status": "success", "projekte": [
            {"name": p.get("name"), "ref": p.get("id"), "region": p.get("region"), "zustand": p.get("status"),
             "prod": is_prod(p)} for p in projects]}

    async def _supabase_tabellen(self, a: dict) -> dict:
        schema = str(a.get("schema") or "public").strip()
        if not SQL_SCHEMA.fullmatch(schema):
            raise CloudError("Ungueltiger Schema-Name.")
        project = await self._project(a.get("projekt"))
        sql = f"select table_name from information_schema.tables where table_schema = '{schema}' order by 1"
        return await self._query(project, sql, read_only=True)

    async def _supabase_sql(self, a: dict) -> dict | Proposal:
        sql = self._text(a.get("sql"), MAX_SQL_CHARS, "Die SQL-Anweisung")
        project = await self._project(a.get("projekt"))
        if is_read_sql(sql):
            return await self._query(project, sql, read_only=True)  # Supabase erzwingt nur-lesen
        if is_prod(project):
            raise CloudError("Auf PROD-Datenbanken schreibt nieeeki nicht. Aenderungen dort laufen als Migration "
                             "mit Pruefung ueber Claude Code.")
        return Proposal(grund=f"SQL in {project.get('name')} ausfuehren", detail=_short(sql, 300),
                        run=lambda: self._query(project, sql, read_only=False))
