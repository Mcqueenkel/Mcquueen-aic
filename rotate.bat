@echo off
REM Account rotation. Prefers the old bot virtualenv interpreter.
setlocal

set "HERE=%~dp0"
if "%AIC_DATA_ROOT%"=="" set "AIC_DATA_ROOT=%HERE%..\data-root"
set "VENV_PY=%AIC_DATA_ROOT%\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%HERE%rotate_cli.py" %*
) else (
    echo [!] the old bot venv not found at "%VENV_PY%"; falling back to system python.
    python "%HERE%rotate_cli.py" %*
)

endlocal
exit /b %ERRORLEVEL%
