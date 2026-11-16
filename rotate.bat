@echo off
REM Account rotation. Uses .venv\ next to this file if it exists (see README),
REM else python on PATH. Override with AIC_PY=C:\path\to\python.exe
setlocal

set "HERE=%~dp0"
if "%AIC_PY%"=="" if exist "%HERE%.venv\Scripts\python.exe" set "AIC_PY=%HERE%.venv\Scripts\python.exe"
if "%AIC_PY%"=="" set "AIC_PY=python"

"%AIC_PY%" "%HERE%rotate_cli.py" %*

endlocal
exit /b %ERRORLEVEL%
