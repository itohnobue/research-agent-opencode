@echo off
REM Wrapper script for web_research.py using uv (Windows)

setlocal

REM Get the directory where this script is located
set "SCRIPT_DIR=%~dp0"
REM Remove trailing backslash
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM Repo root (script lives in .opencode/tools, so two levels up)
for %%I in ("%SCRIPT_DIR%\..\..") do set "REPO_ROOT=%%~fI"

REM Local uv install (tool-use policy R3): repo-local, never system-wide
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

REM Clear PYTHONPATH to avoid conflicts with system Python
set "PYTHONPATH="

REM Set UTF-8 encoding for proper Unicode handling
set PYTHONIOENCODING=utf-8

REM Run with inline dependencies (PEP 723 metadata in web_research.py)
REM Use forward slashes for Python script path
set "SCRIPT_PATH=%SCRIPT_DIR%\web_research.py"
set "SCRIPT_PATH=%SCRIPT_PATH:\=/%"

"%UV_DIR%\uv.exe" run --no-project "%SCRIPT_PATH%" %*
