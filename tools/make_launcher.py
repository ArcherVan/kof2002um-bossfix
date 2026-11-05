"""make_launcher.py —— 在游戏目录生成"双击即用"的启动批处理

用法：
    make_launcher.py                       自动找游戏目录，生成 <游戏目录>\启动KOF2002UM.cmd
    make_launcher.py --game-dir "D:\\Game"  指定游戏目录
    make_launcher.py --out "D:\\x\\go.cmd"  指定输出文件

生成的批处理：清空代理环境变量（开着代理也能起）→ 优先用本仓库的
src\\launch.py 启动并打补丁 → 找不到 Python 时直接起游戏。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import launch  # noqa: E402

TEMPLATE = r'''@echo off
rem 拳皇 2002 UM 一键启动（双击即用）—— 由 kof2002um-bossfix 生成
setlocal
title KOF 2002 UM
for %%I in ("%~dp0.") do set "GAME_DIR=%%~fI"
set "EXE=%GAME_DIR%\KingOfFighters2002UM.exe"
set "KOF_HOME={{HOME}}"

rem 代理环境：清空代理变量，避免游戏和 Steam 接口被代理拖住
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "NO_PROXY=*"
set "no_proxy=*"

if not exist "%EXE%" (
    echo [错误] 没找到 "%EXE%"
    echo        请把本文件放在游戏目录下再运行。
    pause
    exit /b 1
)

cd /d "%GAME_DIR%"

rem 找可用的 Python（只用于打补丁；没有也能开游戏）
set "PY="
py -3 -c "import ctypes" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :run
python -c "import ctypes" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :run
python3 -c "import ctypes" >nul 2>&1
if not errorlevel 1 set "PY=python3"

:run
if not defined PY goto :plain
if not exist "%KOF_HOME%\src\launch.py" goto :plain
%PY% "%KOF_HOME%\src\launch.py" --exe "%EXE%" %*
goto :end

:plain
start "" "%EXE%"

:end
if errorlevel 1 pause
exit /b %errorlevel%
'''


def render(home):
    """批处理里有大量 % 号，不能用 % 格式化，只能做字符串替换"""
    text = TEMPLATE.replace('{{HOME}}', home)
    return text.replace('\r\n', '\n').replace('\n', '\r\n')


def write(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fp:
        fp.write(text.encode('gbk'))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('--game-dir')
    ap.add_argument('--out')
    a = ap.parse_args()

    game_dir = a.game_dir
    if not game_dir:
        exe = launch.find_exe()
        if not exe:
            raise SystemExit('没找到游戏，请用 --game-dir 指定游戏目录')
        game_dir = os.path.dirname(exe)
    game_dir = os.path.abspath(game_dir)
    if not os.path.isdir(game_dir):
        raise SystemExit('目录不存在：%s' % game_dir)

    out = a.out or os.path.join(game_dir, '启动KOF2002UM.cmd')
    home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    write(out, render(home))
    print('已生成：%s' % out)
    print('源仓库：%s' % home)


if __name__ == '__main__':
    main()
