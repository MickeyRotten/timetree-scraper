@echo off
python "%~dp0sync.py" %*
if errorlevel 1 pause
