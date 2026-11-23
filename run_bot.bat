@echo off
rem Runs the bot forever: restarts it 10 seconds after it stops or crashes.
cd /d "%~dp0"
if not exist logs mkdir logs
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
:loop
"%PY%" -m bot.main >> logs\stdout.log 2>&1
echo [%date% %time%] bot exited with code %errorlevel%, restarting in 10 seconds... >> logs\stdout.log
rem ping instead of timeout: timeout fails when there is no console (Task Scheduler)
ping -n 11 127.0.0.1 >nul
goto loop
