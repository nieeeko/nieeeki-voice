@echo off
rem nieeeki als eigenes App-Fenster in einer separaten Chrome-Instanz starten.
rem Die Drossel-Schalter wirken nur in einer eigenen Instanz, daher das eigene Profil
rem (Mikrofon einmal erlauben, danach merkt sich Chrome das).
rem Ohne diese Schalter drosselt Chrome/Windows das Fenster, sobald ein anderes Fenster
rem vorne ist: Mikrofon-Audio kommt dann lueckenhaft an, nieeeki versteht nichts mehr.

set "URL=%~1"
if "%URL%"=="" set "URL=http://localhost:3000"

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Chrome nicht gefunden.
  exit /b 1
)

start "" "%CHROME%" ^
  --user-data-dir="%LOCALAPPDATA%\nieeeki-chrome" ^
  --app=%URL% ^
  --window-size=1400,900 ^
  --disable-background-timer-throttling ^
  --disable-renderer-backgrounding ^
  --disable-backgrounding-occluded-windows ^
  --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling ^
  --no-first-run --no-default-browser-check
