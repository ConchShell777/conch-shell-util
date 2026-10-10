@echo off
setlocal
rem Choose a Python 3.10+ installation that includes Tkinter.
py -3 -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 (
    set "COMFY_BATCH_PY=py -3"
    goto run
)
python -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>&1
if not errorlevel 1 (
    set "COMFY_BATCH_PY=python"
    goto run
)
echo Python 3.10+ with Tkinter was not found.
echo Install Python from https://www.python.org/downloads/
echo Enable the Python launcher or Add Python to PATH during setup.
pause
exit /b 1

:run
if /i "%~1"=="--check" exit /b 0
%COMFY_BATCH_PY% "%~dp0comfyui_batch.py"
set "COMFY_BATCH_EXIT=%errorlevel%"
if not "%COMFY_BATCH_EXIT%"=="0" (
    echo.
    echo The application exited with an error. See the message above.
    pause
)
exit /b %COMFY_BATCH_EXIT%
