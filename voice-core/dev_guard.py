"""Waechter fuer Entwicklungsauftraege: PreToolUse-Hook von Claude Code.

Claude Code ruft dieses Skript vor JEDEM Werkzeugaufruf auf (JSON auf stdin). Hier, nicht im
Prompt, gilt Nikolajs Freigabe-Tabelle:

  sofort     Code schreiben, Tests, lokale Commits, neue Repos, Preview-Deploys, DEV-Datenbanken
  Freigabe   git push (main), Produktions-Deploy, Schreiben auf Supabase shared-prod, PR mergen
  gesperrt   Repos/Projekte/Datenbanken loeschen, Secrets/Env-Variablen aendern, Force-Push,
             Domains kaufen, Zugriff auf nieeeki-voice selbst und auf Zugangsdaten

Freigabe-Aktionen werden abgelehnt und als Anfrage in NIEEEKI_PENDING (JSONL) notiert. Nikolaj
gibt sie per Klick in der UI frei; dann steht der exakte Fingerabdruck einmalig in
NIEEEKI_APPROVALS und der naechste identische Aufruf darf durch.

Nur Standardbibliothek: laeuft mit dem Python des Voice-Cores als eigener Prozess.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

PROD_SUPABASE_PROJECTS = {
    p.strip() for p in os.getenv("NIEEEKI_PROD_SUPABASE", "ttvinmxmgzcryffkyzjk").split(",") if p.strip()
}
SELF_REPO = "nieeeki-voice"

# ---------- Bash ----------
BASH_BLOCKED = [
    (r"\bgit\s+push\b.*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)", "Force-Push ist gesperrt"),
    (r"\bgit\s+filter-(branch|repo)\b", "Git-Historie umschreiben ist gesperrt"),
    (r"\bgh\s+repo\s+(delete|archive)\b", "Repos loeschen/archivieren ist gesperrt"),
    (r"\bgh\s+(secret|variable|auth|ssh-key|gpg-key)\b", "Secrets und Zugaenge sind gesperrt"),
    (r"\bvercel\s+(rm|remove)\b|\bvercel\s+project\s+(rm|remove)\b", "Vercel-Projekte loeschen ist gesperrt"),
    (r"\bvercel\s+(env|secrets?|domains?\s+buy|certs?)\b", "Vercel-Env/Domains/Zertifikate sind gesperrt"),
    (r"\bsupabase\s+(projects\s+delete|secrets|orgs)\b", "Supabase-Projekte/Secrets sind gesperrt"),
    (r"\b(dropdb|drop\s+database)\b", "Datenbank loeschen ist gesperrt"),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|[A-Za-z]:\\?)(\s|$)", "Loeschen ausserhalb des Projekts ist gesperrt"),
    (r"Remove-Item\b.*\s(C:\\|~|\$env:USERPROFILE)(\s|\\?$)", "Loeschen ausserhalb des Projekts ist gesperrt"),
    (r"\b(curl|wget|iwr|Invoke-WebRequest)\b[^|]*\|\s*(sh|bash|iex|powershell|pwsh)\b", "Skripte aus dem Netz direkt ausfuehren ist gesperrt"),
    (r"(\.ssh|\.aws|\.config[\\/]gh|credential|\.git-credentials|id_rsa|id_ed25519)", "Zugriff auf Zugangsdaten ist gesperrt"),
    (SELF_REPO, "nieeeki-voice selbst ist fuer Entwicklungsauftraege gesperrt"),
]
BASH_APPROVAL = [
    (r"\bgit\s+push\b", "git push"),
    (r"\bgh\s+pr\s+merge\b", "Pull Request mergen"),
    (r"\bvercel\b.*\s--prod\b|\bvercel\s+promote\b|\bvercel\s+rollback\b|\bvercel\s+alias\b", "Vercel-Produktion"),
    (r"\bsupabase\s+(db\s+push|db\s+reset|migration\s+up|link)\b", "Supabase-Datenbank (CLI) aendern"),
]

# ---------- MCP-Werkzeuge ----------
VERCEL_BLOCKED = re.compile(
    r"(env|secret|domain_availability|buy_domain|create_or_transfer_domain|deletion|firewall|"
    r"protection_bypass|revoke|token|kms|pause_project|delete|attack_challenge|bypass_ip|drain)",
    re.IGNORECASE,
)
VERCEL_APPROVAL = re.compile(
    r"(request_promote|request_rollback|assign_alias|add_project_domain|rolling_release|update_project|"
    r"create_record|update_record|replace_domain_dns|verify_project_domain|edge_config)",
    re.IGNORECASE,
)
SUPABASE_BLOCKED = {"delete_branch", "pause_project", "create_edge_function_secret"}
SUPABASE_WRITE = {
    "apply_migration", "execute_sql", "deploy_edge_function", "create_branch", "merge_branch",
    "reset_branch", "rebase_branch", "restore_project",
}

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def fingerprint(tool_name: str, tool_input: dict) -> str:
    raw = tool_name + "\n" + json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def decide(tool_name: str, tool_input: dict, project_dir: Path | None) -> tuple[str, str]:
    """('allow'|'approval'|'block', Begruendung)."""
    tool_input = tool_input or {}

    if tool_name in FILE_TOOLS or tool_name == "Read":
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if re.search(r"(\.ssh|\.aws|\.git-credentials|id_rsa|id_ed25519)", path, re.IGNORECASE):
            return "block", "Zugriff auf Zugangsdaten ist gesperrt"
        if SELF_REPO in path.replace("\\", "/").split("/"):
            return "block", "nieeeki-voice selbst ist fuer Entwicklungsauftraege gesperrt"
        if tool_name in FILE_TOOLS and project_dir is not None:
            try:
                Path(path).resolve().relative_to(project_dir.resolve())
            except ValueError:
                return "block", f"Schreiben nur im Projektordner {project_dir}"
        return "allow", ""

    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        for pattern, why in BASH_BLOCKED:
            if re.search(pattern, cmd, re.IGNORECASE):
                return "block", why
        for pattern, why in BASH_APPROVAL:
            if re.search(pattern, cmd, re.IGNORECASE):
                return "approval", why
        return "allow", ""

    if tool_name.startswith("mcp__") and "vercel" in tool_name.lower():
        name = tool_name.rsplit("__", 1)[-1]
        if VERCEL_BLOCKED.search(name):
            return "block", f"Vercel {name} ist gesperrt"
        if name == "create_deployment" and str(tool_input.get("target", "")).lower() == "production":
            return "approval", "Vercel-Produktions-Deploy"
        if VERCEL_APPROVAL.search(name):
            return "approval", f"Vercel {name}"
        return "allow", ""

    if tool_name.startswith("mcp__") and "supabase" in tool_name.lower():
        name = tool_name.rsplit("__", 1)[-1]
        if name in SUPABASE_BLOCKED:
            return "block", f"Supabase {name} ist gesperrt"
        if name in SUPABASE_WRITE and str(tool_input.get("project_id", "")) in PROD_SUPABASE_PROJECTS:
            return "approval", f"Supabase PROD: {name}"
        return "allow", ""

    if tool_name.startswith("mcp__") and "github" in tool_name.lower():
        name = tool_name.rsplit("__", 1)[-1].lower()
        if re.search(r"(delete|secret|archive)", name):
            return "block", f"GitHub {name} ist gesperrt"
        if re.search(r"(merge|push)", name):
            return "approval", f"GitHub {name}"
        return "allow", ""

    return "allow", ""


def _consume_approval(fp: str, approvals_path: Path | None) -> bool:
    if not approvals_path or not approvals_path.is_file():
        return False
    try:
        approved = json.loads(approvals_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if fp not in approved:
        return False
    approved.remove(fp)  # einmalig
    approvals_path.write_text(json.dumps(approved), encoding="utf-8")
    return True


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        _deny("Waechter: unlesbare Anfrage")
        return 0
    tool_name = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    project = os.getenv("NIEEEKI_PROJECT_DIR")
    decision, why = decide(tool_name, tool_input, Path(project) if project else None)

    if decision == "allow":
        return 0
    if decision == "block":
        _deny(f"GESPERRT: {why}. Nikolaj macht das selbst. Nicht umgehen, nicht anders versuchen.")
        return 0

    fp = fingerprint(tool_name, tool_input)
    approvals = os.getenv("NIEEEKI_APPROVALS")
    if _consume_approval(fp, Path(approvals) if approvals else None):
        return 0
    pending = os.getenv("NIEEEKI_PENDING")
    if pending:
        with open(pending, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": fp, "grund": why, "tool": tool_name, "input": tool_input},
                               ensure_ascii=False) + "\n")
    _deny(
        f"FREIGABE NOETIG ({why}). Nikolaj muss das in der Oberflaeche freigeben. Mach alles andere "
        "fertig, fuehre diesen Schritt NICHT anders aus und nenne ihn am Ende deiner Antwort."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
