# Entfernt den nieeeki-Admin-Helfer wieder (Aufgabe und C:\ProgramData\nieeeki). Fragt per UAC nach.
$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    exit
}
$task = Get-ScheduledTask -TaskName "nieeeki-admin" -ErrorAction SilentlyContinue
if ($task) {
    $task | Stop-ScheduledTask -ErrorAction SilentlyContinue
    $task | Unregister-ScheduledTask -Confirm:$false
}
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*nieeeki\admin\admin_helper.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$root = Join-Path $env:ProgramData "nieeeki"
if (Test-Path $root) { Remove-Item $root -Recurse -Force }
Write-Host "Admin-Helfer entfernt (Aufgabe, Ordner, Erlaubnisliste, Protokoll)." -ForegroundColor Green
Read-Host "Enter zum Schliessen"
