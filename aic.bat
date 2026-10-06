@echo off
REM Launcher: prefers the old bot virtualenv interpreter, which already has
REM curl_cffi / msgpack / pycryptodome installed.
setlocal

set "HERE=%~dp0"
if "%AIC_DATA_ROOT%"=="" set "AIC_DATA_ROOT=%HERE%..\data-root"
set "VENV_PY=%AIC_DATA_ROOT%\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%HERE%aic_cli.py" %*
) else (
    echo [!] the old bot venv not found at "%VENV_PY%"; falling back to system python.
    python "%HERE%aic_cli.py" %*
)

endlocal
exit /b %ERRORLEVEL%
