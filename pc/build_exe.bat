@echo off
rem Build ExpMeter.exe with PyInstaller (+ optional UPX)
rem Output: dist\ExpMeter.exe

setlocal
cd /d %~dp0

set PYTHON=..\.venv\Scripts\python.exe
if not exist %PYTHON% (
    echo [ERROR] .venv not found: %PYTHON%
    exit /b 1
)

set UPX_DIR=tools\upx-5.0.0-win64
set UPX_OPT=
if exist %UPX_DIR%\upx.exe (
    set UPX_OPT=--upx-dir %UPX_DIR%
    echo [INFO] Using UPX: %UPX_DIR%\upx.exe
) else (
    echo [INFO] UPX not found, building without compression
)

echo [INFO] Cleaning build/ dist/
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo [INFO] Running PyInstaller
%PYTHON% -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "ExpMeter" ^
    --paths . ^
    %UPX_OPT% ^
    run_exp_meter.py

if errorlevel 1 (
    echo [ERROR] Build failed
    exit /b 1
)

echo.
echo [OK] Build complete: dist\ExpMeter.exe
for %%F in (dist\ExpMeter.exe) do @echo      Size: %%~zF bytes
endlocal
