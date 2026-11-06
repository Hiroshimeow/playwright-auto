@echo off
rem One on-login launch for the G6 Pi operator. The Python runner holds an
rem exclusive lock so duplicate invocations cannot dispatch twice.
cd /d E:\git-project\cdpa
start "" /min "E:\git-project\cdpa\.venv\Scripts\pythonw.exe" "E:\git-project\cdpa\workflows\self-evolution\run_pi.py"
exit /b 0
