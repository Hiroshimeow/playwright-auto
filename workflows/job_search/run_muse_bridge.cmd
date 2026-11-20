@echo off
setlocal
set "ROOT=E:\git-project\cdpa"
if not exist "%ROOT%\.runtime\job-search" mkdir "%ROOT%\.runtime\job-search"
cd /d "%ROOT%"
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\workflows\job_search\muse_cdpa_followup.py" --apply >> "%ROOT%\.runtime\job-search\muse-bridge-g6.log" 2>&1
if errorlevel 1 exit /b %ERRORLEVEL%
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\workflows\job_search\muse_cdpa_bridge.py" --apply >> "%ROOT%\.runtime\job-search\muse-bridge-g6.log" 2>&1
exit /b %ERRORLEVEL%
