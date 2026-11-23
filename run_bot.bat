@echo off
rem Runs the bot forever: restarts it 10 seconds after it stops or crashes.
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
:loop
"%PY%" -m bot.main
echo [%date% %time%] bot exited with code %errorlevel%, restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
