@echo off
rem Launcher shim for OpenWhisper. Lives in the repo so updates ride along with `git pull`.
rem %~dp0 = directory of this .cmd file (with trailing backslash). %~dp0.. = repo root.

setlocal
set "REPO=%~dp0.."
set "PYTHONW=%REPO%\venv\Scripts\pythonw.exe"
set "ENTRY=%REPO%\main.py"

if not exist "%PYTHONW%" (
    echo [openwhisper] Could not find "%PYTHONW%".
    echo [openwhisper] Make sure the venv is created: python -m venv venv ^&^& pip install -r requirements.txt
    exit /b 1
)

cd /d "%REPO%"
"%PYTHONW%" "%ENTRY%" %*
endlocal
