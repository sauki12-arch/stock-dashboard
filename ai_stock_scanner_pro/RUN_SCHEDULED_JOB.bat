@echo off
REM Headless scheduled job for Windows Task Scheduler, e.g.:  RUN_SCHEDULED_JOB.bat post_close
REM Jobs: pre_market | post_close | intraday | weekly_review   (times are your choice in Task Scheduler;
REM the app's own timer is only active while the GUI is open).
setlocal
cd /d "%~dp0"
if "%~1"=="" (echo usage: RUN_SCHEDULED_JOB.bat ^<job^> & exit /b 2)
if exist "dist\AIStockScannerPro\AIStockScannerPro-cli.exe" (
  "dist\AIStockScannerPro\AIStockScannerPro-cli.exe" job %1
) else (
  call .venv\Scripts\activate.bat
  python run_app.py job %1
)
endlocal
