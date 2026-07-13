@echo off
chcp 65001 >nul
REM Launch CodexBar (no console window)
set "ROOT=%~dp0"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
where uv >nul 2>nul
if errorlevel 1 (
    echo CodexBar requires uv to prepare its locked project environment.
    pause
    exit /b 1
)
pushd "%ROOT%"
uv sync --quiet --frozen
if errorlevel 1 (
    popd
    echo Failed to prepare the CodexBar project environment.
    pause
    exit /b 1
)
popd
if not exist "%PYW%" (
    echo CodexBar environment was created without pythonw.exe.
    pause
    exit /b 1
)
start "" "%PYW%" "%~dp0codexbar.pyw"
