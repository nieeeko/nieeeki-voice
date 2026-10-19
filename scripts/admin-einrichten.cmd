@echo off
rem Doppelklick: richtet den nieeeki-Admin-Helfer ein (fragt einmal per UAC nach Adminrechten).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0admin-einrichten.ps1"
