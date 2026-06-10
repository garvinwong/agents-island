@echo off
rem Agents Island 启动器：先拉 WSL 桥，再启 Windows 岛壳
rem WSL 发行版可在本目录 distro.txt 指定（缺省=默认发行版）
setlocal
set "LAUNCH=%~dp0"
set "DISTRO_ARG="
if exist "%LAUNCH%distro.txt" (
    set /p DISTRO=<"%LAUNCH%distro.txt"
    if defined DISTRO set "DISTRO_ARG=-d %DISTRO%"
)
wsl.exe %DISTRO_ARG% -- bash -c "bash \"$(wslpath '%LAUNCH%start_bridge.sh')\""
start "" pythonw.exe "%LAUNCH%..\win\island.py"
endlocal
