@echo off
setlocal DisableDelayedExpansion
set "FUSION_NO_PAUSE="
for %%A in (%*) do if /I "%%~A"=="-NoPause" set "FUSION_NO_PAUSE=1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "FUSION_EXIT=%ERRORLEVEL%"
if not "%FUSION_EXIT%"=="0" echo Fusion installation failed. See the diagnostics and installation report above.
if not defined FUSION_NO_PAUSE pause
exit /b %FUSION_EXIT%
