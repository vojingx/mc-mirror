@echo off
chcp 65001 >nul
setlocal
echo.
echo   正在安装 download 命令 ...
echo.

set "SRC=%~dp0"
set "DST=%LOCALAPPDATA%\download-mc\bin"

if not exist "%DST%" mkdir "%DST%"
copy /y "%SRC%download.py" "%DST%\download.py" >nul
copy /y "%SRC%download.cmd" "%DST%\download.cmd" >nul
echo   已复制到 %DST%

powershell -NoProfile -ExecutionPolicy Bypass -Command "$d='%DST%'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if ($null -eq $p) { $p='' }; if ($p -notlike ('*' + $d + '*')) { [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';' + $d), 'User') }; Write-Host '  已加入用户 PATH'"

echo.
echo   请关掉这个窗口，重新打开一个命令行，然后输入：
echo.
echo       download Minecraft 26.1
echo.
pause
