@echo off
REM Wrapper for browser_fetch.py - the real-browser fetch tier (--url-chrome). Windows.
REM
REM One browser only: real Google Chrome, provisioned repo-local into
REM tmp\browser\chrome on first use, exactly like uv and the macOS/Linux wrappers.
REM A system browser is NEVER used, so every host behaves identically; Chromium and
REM Chrome-for-Testing are measurably detected, so they are not used at all.
REM
REM Graceful degradation: if Chrome is unavailable, the request is served by the
REM ORDINARY static --url path instead of failing.
REM
REM Modes:  <urls...>  fetch them with Chrome
REM         --ensure   provision the tier only (no fetch); exit 0 if ready, 3 if not
REM Env:    BROWSER_FETCH_NO_FALLBACK=1  never run the static fallback (exit 3 instead)

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

for %%I in ("%SCRIPT_DIR%\..\..") do set "REPO_ROOT=%%~fI"

REM ------------------------------------------------------------- uv (repo-local)
set "UV_DIR=%REPO_ROOT%\tmp\uv"
set "UV_OK=0"
if exist "%UV_DIR%\uv.exe" ("%UV_DIR%\uv.exe" --version >nul 2>nul && set "UV_OK=1")
if "%UV_OK%"=="0" (
    echo Installing uv to "%UV_DIR%" ...
    if not exist "%UV_DIR%" mkdir "%UV_DIR%"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%UV_DIR%'; $env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex"
    "%UV_DIR%\uv.exe" --version >nul 2>nul || (
        echo uv verification failed; retrying once ...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_INSTALL_DIR='%UV_DIR%'; $env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex"
    )
)

REM --------------------------------------------------------- Chrome (repo-local)
REM Same principle as the macOS/Linux wrappers: the payload lives in tmp\browser\chrome
REM and is installed on first use. Google ships no Chrome zip for Windows, so the
REM official offline installer is unpacked with 7-Zip's standalone console (7zr.exe):
REM   ChromeStandaloneSetup64.exe -> updater.7z -> <ver>_chrome_installer.exe
REM   -> chrome.7z -> Chrome-bin\chrome.exe
set "BROWSER_DIR=%REPO_ROOT%\tmp\browser"
set "CHROME_DIR=%BROWSER_DIR%\chrome"
set "SEVENZ=%BROWSER_DIR%\7zr.exe"
set "CHROME_SETUP=%BROWSER_DIR%\chrome_setup.exe"
set "CHROME_STAGE=%BROWSER_DIR%\.stage-%RANDOM%%RANDOM%"
set "CHROME_LOCK=%BROWSER_DIR%\.chrome.lock"

call :detect_chrome
if not "%CHROME_OK%"=="0" goto :chrome_ready

if not exist "%BROWSER_DIR%" mkdir "%BROWSER_DIR%"
mkdir "%CHROME_LOCK%" 2>nul
if not errorlevel 1 goto :do_install

echo another Chrome provisioning run is in progress; waiting for it ...
set /a WAITED=0
:waitforchrome
ping -n 6 127.0.0.1 >nul 2>nul
call :detect_chrome
if not "%CHROME_OK%"=="0" goto :chrome_ready
set /a WAITED+=5
if %WAITED% LSS 600 goto :waitforchrome
REM The lock holder failed or died: reclaim the stale lock and take over once.
rmdir "%CHROME_LOCK%" 2>nul
mkdir "%CHROME_LOCK%" 2>nul
if errorlevel 1 goto :chrome_ready
goto :do_install

:do_install
call :install_chrome
rmdir "%CHROME_LOCK%" 2>nul
call :detect_chrome
if not "%CHROME_OK%"=="0" goto :chrome_ready
echo Chrome verification failed; retrying once ...
call :install_chrome
rmdir "%CHROME_LOCK%" 2>nul
call :detect_chrome

:chrome_ready
REM ------------------------------------------------------------- URLs for fallback
set "URLS="
for %%A in (%*) do (
    set "ARG=%%~A"
    if /i "!ARG:~0,7!"=="http://"  set URLS=!URLS! "!ARG!"
    if /i "!ARG:~0,8!"=="https://" set URLS=!URLS! "!ARG!"
)

REM Clear PYTHONPATH to avoid conflicts with system Python; UTF-8 for Unicode handling
set "PYTHONPATH="
set PYTHONIOENCODING=utf-8
set "SCRIPT_PATH=%SCRIPT_DIR%\browser_fetch.py"
set "SCRIPT_PATH=%SCRIPT_PATH:\=/%"

REM --ensure: provisioning-only mode (web_research.py's --url preflight) - no fetch
set "ENSURE_ONLY=0"
echo %* | findstr /C:"--ensure" >nul 2>nul
if not errorlevel 1 set "ENSURE_ONLY=1"

if "%CHROME_OK%"=="0" (
    if "%ENSURE_ONLY%"=="1" (
        echo note: Google Chrome could not be provisioned into tmp\browser\chrome
        exit /b 3
    )
    goto :notier
)
if "%ENSURE_ONLY%"=="1" exit /b 0

"%UV_DIR%\uv.exe" run --no-project "%SCRIPT_PATH%" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="3" goto :notier
exit /b %RC%

:notier
REM The caller already holds the static result: never re-run the static fetch.
if "%BROWSER_FETCH_NO_FALLBACK%"=="1" exit /b 3
if "!URLS!"=="" (
    echo error: Google Chrome is not available and no URL was given to fall back on
    exit /b 3
)
echo note: Google Chrome is not available - serving this with the ordinary static --url fetch
set "RESEARCH_PATH=%SCRIPT_DIR%\web_research.py"
set "RESEARCH_PATH=%RESEARCH_PATH:\=/%"
REM --no-render: this fallback IS the static path - it must never escalate back into
REM the browser tier (a nested attempt would recurse while Chrome is missing).
REM web_research.py --url takes exactly one URL, so fetch them one at a time.
set "FALLBACK_RC=0"
for %%U in (!URLS!) do (
    "%UV_DIR%\uv.exe" run --no-project "%RESEARCH_PATH%" --url %%U --no-render
    if errorlevel 1 set "FALLBACK_RC=1"
)
exit /b !FALLBACK_RC!

REM ==========================================================================
REM Subroutines
REM ==========================================================================

:detect_chrome
set "CHROME_OK=0"
for /f "delims=" %%F in ('dir /b /s "%CHROME_DIR%\chrome.exe" 2^>nul') do set "CHROME_OK=1"
exit /b 0

:install_chrome
REM Unpack Google's offline installer into a staging dir and swap it into
REM tmp\browser\chrome only on success: an interrupted extraction must never leave
REM a payload that passes the readiness check. Anything already downloaded is reused.
REM Verified chain, 2026-10-01.
if not exist "%SEVENZ%" (
    echo Downloading 7-Zip standalone (7zr.exe) ...
    call :download "https://www.7-zip.org/a/7zr.exe" "%SEVENZ%"
)
if not exist "%SEVENZ%" exit /b 1
if not exist "%CHROME_SETUP%" (
    echo Downloading the Google Chrome offline installer (one-time, ~160MB) ...
    call :download "https://dl.google.com/chrome/install/ChromeStandaloneSetup64.exe" "%CHROME_SETUP%"
)
if not exist "%CHROME_SETUP%" exit /b 1

if exist "%CHROME_STAGE%" rmdir /s /q "%CHROME_STAGE%"
mkdir "%CHROME_STAGE%"

REM 1/3 installer -> updater.7z      2/3 -> <ver>_chrome_installer.exe
REM 3/3 -> chrome.7z -> Chrome-bin\chrome.exe
"%SEVENZ%" x "%CHROME_SETUP%" -o"%CHROME_STAGE%\s1" -y -bso0 -bsp0 || goto :install_failed
set "UPD="
for /r "%CHROME_STAGE%\s1" %%F in (updater.7z) do set "UPD=%%F"
if not defined UPD goto :install_failed

"%SEVENZ%" x "!UPD!" -o"%CHROME_STAGE%\s2" -y -bso0 -bsp0 || goto :install_failed
set "CHROME_INST="
for /r "%CHROME_STAGE%\s2" %%F in (*_chrome_installer.exe) do set "CHROME_INST=%%F"
if not defined CHROME_INST goto :install_failed

"%SEVENZ%" e "!CHROME_INST!" -o"%CHROME_STAGE%\s3" -y -bso0 -bsp0 || goto :install_failed
if not exist "%CHROME_STAGE%\s3\chrome.7z" goto :install_failed
"%SEVENZ%" x "%CHROME_STAGE%\s3\chrome.7z" -o"%CHROME_STAGE%\chrome" -y -bso0 -bsp0 || goto :install_failed

REM Swap the fully-staged payload into place only now.
rmdir /s /q "%CHROME_DIR%" 2>nul
if exist "%CHROME_DIR%" goto :install_failed
move "%CHROME_STAGE%\chrome" "%CHROME_DIR%" >nul || goto :install_failed

rmdir /s /q "%CHROME_STAGE%" 2>nul
del /q "%CHROME_SETUP%" 2>nul
exit /b 0

:install_failed
rmdir /s /q "%CHROME_STAGE%" 2>nul
exit /b 1

:download
REM :download <url> <outfile> — curl.exe when present (fast, mirrors the .sh), else PowerShell.
where curl.exe >nul 2>nul
if errorlevel 1 goto :download_ps
curl.exe -fL --retry 3 -o "%~2" "%~1"
exit /b %ERRORLEVEL%

:download_ps
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -UseBasicParsing -Uri '%~1' -OutFile '%~2'"
exit /b %ERRORLEVEL%
