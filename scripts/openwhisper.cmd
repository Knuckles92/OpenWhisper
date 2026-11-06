@echo off
rem Launcher shim for OpenWhisper. Lives in the repo so updates ride along with `git pull`.
rem %~dp0 = directory of this .cmd file (with trailing backslash). %~dp0.. = repo root.

setlocal
set "REPO=%~dp0.."
set "PYTHONW=%REPO%\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" if exist "%REPO%\venv\Scripts\pythonw.exe" set "PYTHONW=%REPO%\venv\Scripts\pythonw.exe"
set "ENTRY=%REPO%\main.py"

if not exist "%PYTHONW%" (
    echo [openwhisper] Could not find "%PYTHONW%".
    echo [openwhisper] From the repo root, run: uv sync --locked
    exit /b 1
)

cd /d "%REPO%"
"%PYTHONW%" "%ENTRY%" %*
endlocal
