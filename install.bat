@echo off
REM Fusion Windows installer. Use -NoPause for unattended/scripted installation.
call "%~dp0install.cmd" %*
exit /b %ERRORLEVEL%
