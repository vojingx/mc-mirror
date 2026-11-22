@echo off
setlocal
set "SCRIPT=%~dp0download.py"
where python >nul 2>nul
if errorlevel 1 goto trypy
python "%SCRIPT%" %*
goto end
:trypy
where py >nul 2>nul
if errorlevel 1 goto nopython
py "%SCRIPT%" %*
goto end
:nopython
echo [错误] 没有找到 Python，请先安装 Python 3.9 或更高版本。
echo 下载地址：https://www.python.org/downloads/
pause
:end
