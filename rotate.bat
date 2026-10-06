@echo off
REM Account rotation. Prefers the Icarus virtualenv interpreter.
setlocal

set "HERE=%~dp0"
if "%AIC_ICARUS_ROOT%"=="" set "AIC_ICARUS_ROOT=%HERE%..\Icarus-Dev-Build-Private-main"
set "VENV_PY=%AIC_ICARUS_ROOT%\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%HERE%rotate_cli.py" %*
) else (
    echo [!] Icarus venv not found at "%VENV_PY%"; falling back to system python.
    python "%HERE%rotate_cli.py" %*
)

endlocal
exit /b %ERRORLEVEL%
