@echo off
setlocal
set "ROOT=%~dp0"
pushd "%ROOT%"
"%ROOT%.venv\Scripts\python.exe" -m playwright_auto.cdpa_cli %* --config "%ROOT%cdpa.yaml"
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
