@echo off
REM ===== AI Stock Scanner Pro - reproducible EXE build (Windows 10/11, 64-bit Python 3.12 or 3.13) =====
setlocal
cd /d "%~dp0"
chcp 65001 >nul

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found in PATH. Install 64-bit Python 3.12/3.13 from python.org and tick "Add python.exe to PATH".
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo [1/5] Creating virtual environment...
  python -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat

echo [2/5] Installing pinned dependencies...
python -m pip install --upgrade pip || exit /b 1
python -m pip install -r requirements-dev.txt || exit /b 1

echo [3/5] Running automated tests (build is aborted on failure)...
set QT_QPA_PLATFORM=offscreen
python -m pytest -q -x || (echo [ERROR] Tests failed - EXE not built. & exit /b 1)

echo [4/5] Building EXE with PyInstaller...
python -m PyInstaller packaging\aiscanner.spec --noconfirm --clean || exit /b 1

echo [5/5] Smoke-testing the built EXE (headless self-test on synthetic data)...
set AISP_HOME=%TEMP%\aisp_buildtest
dist\AIStockScannerPro\AIStockScannerPro-cli.exe selftest || (echo [ERROR] EXE self-test failed. & exit /b 1)

echo.
echo ==============================================================
echo  Build OK:  dist\AIStockScannerPro\AIStockScannerPro.exe
echo  Copy the whole dist\AIStockScannerPro folder to the target PC.
echo ==============================================================
endlocal
