@echo off
REM ============================================================
REM  Project pip entry point
REM  Why: D:\python.exe is a portable layout whose sys.path does
REM  NOT include D:\Lib\site-packages (where even pip itself
REM  lives). Running `D:\python.exe -m pip` bare fails with
REM  "No module named pip". Setting PYTHONPATH fixes it.
REM
REM  Usage:
REM    pip.bat install redis
REM    pip.bat list
REM    pip.bat freeze > requirements.lock.txt
REM ============================================================
set PYTHONPATH=D:/Lib/site-packages
D:\python.exe -m pip %*
