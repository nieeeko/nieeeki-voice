# Richtet den nieeeki-Admin-Helfer ein (einmal, mit Administratorrechten; fragt per UAC nach).
#
# Danach darf nieeeki Admin-Aktionen vorschlagen (Werkzeug als_admin). Ausgefuehrt wird nur, was
# Nikolaj in der Oberflaeche freigibt oder was auf der Erlaubnisliste steht.
#
# Legt an (alles nur fuer Administratoren schreibbar, sonst koennte ein normales Programm den
# Helfer austauschen und waere damit selbst Admin):
#   C:\ProgramData\nieeeki\admin\
#     admin_helper.py, auth_gate.py   Kopie aus voice-core\ (nach Code-Aenderungen erneut ausfuehren)
#     python\, .venv\                 eigenes Python fuer den Helfer
#     admin.env                       SUPABASE_URL, NIEEEKI_OWNER_ID, SUPABASE_PUBLISHABLE_KEY (keine Geheimnisse)
#     client.key                      Schluessel fuer den PC-Host (Nikolaj darf ihn lesen)
#     erlaubt.json                    Erlaubnisliste (bleibt bei erneutem Einrichten erhalten)
#     admin-audit.jsonl               Protokoll aller Admin-Anfragen
#   Aufgabe \nieeeki\nieeeki-admin    startet den Helfer bei Nikolajs Anmeldung mit hoechsten Rechten
#
# Entfernen: scripts\admin-entfernen.ps1

param([string]$Benutzer = "$env:USERDOMAIN\$env:USERNAME", [string]$BenutzerSid = "")

$ErrorActionPreference = "Stop"
if (-not $BenutzerSid) { $BenutzerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    # Einmal per UAC erhoehen; Benutzer und SID des normalen Kontos mitgeben
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
        "-Benutzer", "`"$Benutzer`"", "-BenutzerSid", $BenutzerSid)
    exit
}

$Repo = Split-Path -Parent $PSScriptRoot
$Root = Join-Path $env:ProgramData "nieeeki"
$Admin = Join-Path $Root "admin"
$AdminsSid = "S-1-5-32-544"
$SystemSid = "S-1-5-18"

function Step($text) { Write-Host "- $text" }

function Protect {
    # Nur SYSTEM und Administratoren schreiben, Nikolaj liest; Inhalt erbt genau das
    icacls $Root /setowner "*$AdminsSid" /T /C /Q | Out-Null
    icacls $Root /inheritance:r /grant:r "*${SystemSid}:(OI)(CI)F" "*${AdminsSid}:(OI)(CI)F" "*${BenutzerSid}:(OI)(CI)RX" /C /Q | Out-Null
    if (Get-ChildItem $Root -Force) { icacls "$Root\*" /reset /T /C /Q | Out-Null }
}

# 1. Ordner: existiert er schon und gehoert nicht den Administratoren, war das nicht dieses Skript.
#    Dann alles verwerfen (auch eine dort untergeschobene Erlaubnisliste).
if (Test-Path $Root) {
    $owner = (Get-Acl $Root).GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -ne $AdminsSid) {
        Step "Vorhandener Ordner $Root gehoerte nicht den Administratoren: wird neu angelegt"
        icacls $Root /setowner "*$AdminsSid" /T /C /Q | Out-Null
        icacls $Root /grant "*${AdminsSid}:(OI)(CI)F" /T /C /Q | Out-Null
        Remove-Item $Root -Recurse -Force
    }
}
Get-ScheduledTask -TaskName "nieeeki-admin" -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*nieeeki\admin\admin_helper.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
New-Item -ItemType Directory -Force $Admin | Out-Null
Protect
foreach ($old in @("admin_helper.py", "auth_gate.py", ".venv", "python")) {
    $p = Join-Path $Admin $old
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}
Step "Ordner $Admin (schreiben nur Administratoren, $Benutzer liest)"

# 2. Code und Konfiguration
Copy-Item (Join-Path $Repo "voice-core\admin_helper.py") $Admin -Force
Copy-Item (Join-Path $Repo "voice-core\auth_gate.py") $Admin -Force
$envFile = @((Join-Path $Repo "voice-core\.env"), (Join-Path $Repo ".env")) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $envFile) { throw "Keine .env gefunden (voice-core\.env oder .env im Repo)" }
$cfg = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*(SUPABASE_URL|NIEEEKI_OWNER_ID|SUPABASE_PUBLISHABLE_KEY)\s*=\s*"?([^"#]*?)"?\s*$') { $cfg[$Matches[1]] = $Matches[2] }
}
foreach ($k in "SUPABASE_URL", "NIEEEKI_OWNER_ID", "SUPABASE_PUBLISHABLE_KEY") {
    if (-not $cfg[$k]) { throw "$k fehlt in $envFile" }
}
if ($cfg["SUPABASE_URL"] -notmatch '^https://') { throw "SUPABASE_URL muss mit https:// beginnen" }
@("SUPABASE_URL=$($cfg['SUPABASE_URL'])", "NIEEEKI_OWNER_ID=$($cfg['NIEEEKI_OWNER_ID'])",
  "SUPABASE_PUBLISHABLE_KEY=$($cfg['SUPABASE_PUBLISHABLE_KEY'])") | Set-Content (Join-Path $Admin "admin.env") -Encoding ascii
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
(($bytes | ForEach-Object { $_.ToString("x2") }) -join "") | Set-Content (Join-Path $Admin "client.key") -Encoding ascii -NoNewline
$allow = Join-Path $Admin "erlaubt.json"
if (-not (Test-Path $allow)) { '{"regeln": []}' | Set-Content $allow -Encoding ascii }
Step "Helfer, admin.env, client.key, erlaubt.json"

# 3. Eigenes Python im Admin-Ordner (nicht das aus dem Benutzerprofil: das koennte jedes Programm aendern)
$uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if (-not (Test-Path $uv)) { $uv = (Get-Command uv -ErrorAction Stop).Source }
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Admin "python"
$env:UV_CACHE_DIR = Join-Path $Admin ".uv-cache"
$env:UV_LINK_MODE = "copy"
$env:UV_PYTHON_PREFERENCE = "only-managed"
& $uv venv --python 3.12 (Join-Path $Admin ".venv") | Out-Null
if ($LASTEXITCODE -ne 0) { throw "uv venv fehlgeschlagen" }
& $uv pip install --python (Join-Path $Admin ".venv\Scripts\python.exe") "pyjwt[crypto]>=2.10" "httpx>=0.27" "python-dotenv>=1.0" "loguru>=0.7" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "uv pip install fehlgeschlagen" }
Remove-Item $env:UV_CACHE_DIR -Recurse -Force -ErrorAction SilentlyContinue
Protect
Step "Python fuer den Helfer"

# 4. Aufgabe: bei Nikolajs Anmeldung mit hoechsten Rechten, in seiner Sitzung (fuer Klicks)
$exe = Join-Path $Admin ".venv\Scripts\pythonw.exe"  # ohne Konsolenfenster
if (-not (Test-Path $exe)) { $exe = Join-Path $Admin ".venv\Scripts\python.exe" }
$action = New-ScheduledTaskAction -Execute $exe -Argument "`"$(Join-Path $Admin 'admin_helper.py')`"" -WorkingDirectory $Admin
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Benutzer
$principal = New-ScheduledTaskPrincipal -UserId $Benutzer -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "nieeeki-admin" -TaskPath "\nieeeki\" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "nieeeki-admin" -TaskPath "\nieeeki\"
Step "Aufgabe \nieeeki\nieeeki-admin"

# 5. Laeuft er?
$ok = $false
for ($i = 0; $i -lt 20 -and -not $ok; $i++) {
    Start-Sleep -Milliseconds 500
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", 8766); $c.Close(); $ok = $true } catch { }
}
if ($ok) { Write-Host "`nFertig: der Admin-Helfer laeuft (127.0.0.1:8766)." -ForegroundColor Green }
else { Write-Host "`nDer Helfer antwortet nicht. Protokoll: $Admin\admin-audit.jsonl" -ForegroundColor Yellow }
Read-Host "Enter zum Schliessen"
