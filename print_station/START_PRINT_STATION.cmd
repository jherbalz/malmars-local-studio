@echo off
setlocal
set "JL_STUDIO_ROOT=%~dp0..\..\.."
for %%I in ("%JL_STUDIO_ROOT%") do set "JL_STUDIO_ROOT=%%~fI"
call "%JL_STUDIO_ROOT%\START_STUDIO.cmd" print
exit /b %ERRORLEVEL%
