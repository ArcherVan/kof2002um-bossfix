@echo off
rem ============================================================
rem  kof2002um-bossfix —— 统一入口
rem
rem  用法（在仓库根目录执行）：
rem      kof.cmd doctor              环境自检
rem      kof.cmd config              查看当前配置
rem      kof.cmd install             加载钩子（游戏须已启动）
rem      kof.cmd on 2                开启补偿，倍率 2.00
rem      kof.cmd off                 关闭补偿
rem      kof.cmd show                查看开关与倍率
rem      kof.cmd status              查看钩子与血量
rem      kof.cmd watch 120           实时打印伤害 120 秒
rem      kof.cmd dump                打印已记录的伤害
rem      kof.cmd uninstall           卸载并还原
rem      kof.cmd rec 300             录制伤害到日志 300 秒
rem      kof.cmd launch             启动游戏并自动打补丁
rem      kof.cmd help                显示本帮助
rem ============================================================
setlocal EnableDelayedExpansion
for %%I in ("%~dp0.") do set "ROOT=%%~fI"
set "PY="

rem ---- 依次尝试 py 启动器 / python / python3，要求能 import ctypes ----
py -3 -c "import ctypes" >nul 2>&1
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python -c "import ctypes" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    python3 -c "import ctypes" >nul 2>&1
    if not errorlevel 1 set "PY=python3"
)
if not defined PY (
    echo [error] 未找到可用的 Python 3。
    echo         需要 3.8 或更新版本，且 ctypes 可正常导入。
    echo         安装后重开命令行，或把 python.exe 所在目录加入 PATH。
    exit /b 1
)

if "%~1"=="" goto :help
if /i "%~1"=="help" goto :help
if /i "%~1"=="--help" goto :help
if /i "%~1"=="-h" goto :help

if /i "%~1"=="doctor" (
    %PY% "%ROOT%\tools\env_check.py"
    exit /b %errorlevel%
)

if /i "%~1"=="power" (
    rem 气槽：show / set <格数> [p1|p2] / on [格数] [p1|p2] / off
    rem       patch 免扣气补丁（一次有效） / unpatch 还原
    %PY% "%ROOT%\src\power.py" %2 %3 %4
    exit /b %errorlevel%
)

if /i "%~1"=="launch" (
    rem 一条命令：启动游戏 + 装免扣气补丁；加 --dmg 2 同时开伤害补偿
    %PY% "%ROOT%\src\launch.py" %*
    exit /b %errorlevel%
)

if /i "%~1"=="rec" (
    if "%~2"=="" (
        %PY% "%ROOT%\src\rec.py"
    ) else (
        %PY% "%ROOT%\src\rec.py" %2
    )
    exit /b %errorlevel%
)

%PY% "%ROOT%\src\hookdmg.py" %*
exit /b %errorlevel%

:help
echo.
echo   kof2002um-bossfix  统一入口
echo   ------------------------------------------------------------
echo     kof.cmd doctor              环境自检
echo     kof.cmd config              查看当前配置
echo     kof.cmd install             加载钩子（游戏须已启动）
echo     kof.cmd on 2                开启补偿，倍率 2.00
echo     kof.cmd on 1.5              自定义倍率
echo     kof.cmd off                 关闭补偿
echo     kof.cmd show                查看开关与倍率
echo     kof.cmd status              查看钩子与双方血量
echo     kof.cmd watch 120           实时打印伤害，120 秒
echo     kof.cmd dump                打印已记录的伤害
echo     kof.cmd uninstall           卸载并逐字节还原
echo     kof.cmd rec 300             录制伤害到 hookdmg_log.txt
echo     kof.cmd launch             启动游戏并自动打补丁（常用）
echo     kof.cmd launch --dmg 2    启动 + 补丁 + 2 倍伤害补偿
echo     kof.cmd power show          查看双方气槽
echo     kof.cmd power patch         免扣气补丁：装一次整局有效
echo     kof.cmd power unpatch       还原补丁
echo     kof.cmd power on 5          临时补满（每回合需重跑）
echo   ------------------------------------------------------------
echo   首次使用请先运行： kof.cmd doctor
echo.
exit /b 0
