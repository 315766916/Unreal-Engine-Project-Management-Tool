@echo off
rem KL Unreal Box environment installer (ASCII-only on purpose)
cd /d "%~dp0"
if exist "Engine\Scripts\pythonw.exe" (
    echo [KL Box] engine already ready. Nothing to do.
    pause
    exit /b 0
)
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -m venv "Engine" || (echo [KL Box] failed to create venv & pause & exit /b 1)
"Engine\Scripts\python.exe" -m pip install PyQt6 || (echo [KL Box] failed to install PyQt6 & pause & exit /b 1)
echo [KL Box] Done! Double-click run.bat to start.
pause
