@echo off
REM ===== AI Stock Scanner Pro launcher =====
REM Uses the built EXE if present, otherwise runs from source in the local virtual environment.
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if exist "dist\AIStockScannerPro\AIStockScannerPro.exe" (
  start "" "dist\AIStockScannerPro\AIStockScannerPro.exe" %*
  goto :eof
)
if exist "AIStockScannerPro.exe" (
  start "" "AIStockScannerPro.exe" %*
  goto :eof
)

if not exist .venv\Scripts\pythonw.exe (
  echo First run: creating environment and installing dependencies - this takes a few minutes...
  where python >nul 2>nul || (echo [ERROR] Python 3.12/3.13 64-bit is required: https://www.python.org/downloads/ & pause & exit /b 1)
  python -m venv .venv || (pause & exit /b 1)
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt || (echo [ERROR] pip install failed & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
start "" .venv\Scripts\pythonw.exe run_app.py %*
endlocal
