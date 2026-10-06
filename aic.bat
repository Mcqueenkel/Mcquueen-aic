@echo off
REM Launcher: prefers the Icarus virtualenv interpreter, which already has
REM curl_cffi / msgpack / pycryptodome installed.
setlocal

set "HERE=%~dp0"
if "%AIC_ICARUS_ROOT%"=="" set "AIC_ICARUS_ROOT=%HERE%..\Icarus-Dev-Build-Private-main"
set "VENV_PY=%AIC_ICARUS_ROOT%\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%HERE%aic_cli.py" %*
) else (
    echo [!] Icarus venv not found at "%VENV_PY%"; falling back to system python.
    python "%HERE%aic_cli.py" %*
)

endlocal
exit /b %ERRORLEVEL%
