"""env_check.py —— 环境自检

在初次使用或工具行为异常时运行：
    kof.cmd doctor

只做读取与校验，不修改游戏进程。
退出码：0 = 关键项全通过；1 = 有关键项不通过。
"""
import ctypes
import os
import platform
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'src'))

import profile as cfg          # noqa: E402
import kofp                    # noqa: E402

OK = '  [OK]   '
BAD = '  [FAIL] '
WARN = '  [WARN] '

fails = []


def line(tag, text):
    print(tag + text)


def check_os():
    print('\n-- 系统 --')
    line(OK, '平台：%s %s' % (platform.system(), platform.release()))
    if platform.system() != 'Windows':
        fails.append('非 Windows 平台')
        line(BAD, '本项目依赖 Windows 进程 API，无法在 %s 上运行'
             % platform.system())
        return
    bits = platform.architecture()[0]
    line(OK if bits == '64bit' else BAD,
         '架构：%s（本作是 64 位程序）' % bits)
    if bits != '64bit':
        fails.append('Python 不是 64 位')


def check_python():
    print('\n-- Python --')
    ver = sys.version_info
    text = '%d.%d.%d  (%s)' % (ver.major, ver.minor, ver.micro, sys.executable)
    if ver >= (3, 8):
        line(OK, '版本：' + text)
    else:
        line(BAD, '版本过低：%s（需 3.8+）' % text)
        fails.append('Python 版本过低')
    try:
        ctypes.WinDLL('kernel32')
        line(OK, 'ctypes 与 kernel32 可用')
    except Exception as e:
        line(BAD, 'ctypes 异常：%r（见 README 排查章节）' % (e,))
        fails.append('ctypes 不可用')


def check_admin():
    print('\n-- 权限 --')
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    if admin:
        line(OK, '当前为管理员权限')
    else:
        line(WARN, '非管理员权限；多数情况够用，若写入失败请用管理员重开命令行')


def check_config():
    print('\n-- 配置 --')
    line(OK, '来源：%s' % cfg.CONFIG_SOURCE)
    for k in ('process_name', 'hook_site', 'hook_orig', 'player_obj',
              'enemy_obj', 'default_factor'):
        v = cfg.P.get(k)
        if isinstance(v, int) and k not in ('default_factor',):
            v = '0x%08X' % v
        line(OK, '%-14s %s' % (k, v))
    if cfg.CONFIG_SOURCE == 'built-in defaults':
        line(WARN, '使用内置默认值（对应 Steam 版）。换版本时请复制 '
                   'config.example.json 为 config.json 再修改')


def check_game():
    print('\n-- 游戏进程 --')
    pids = kofp.find_pids(cfg.PROCESS_NAME)
    if not pids:
        line(WARN, '未检测到 %s（未启动，或进程名不同）' % cfg.PROCESS_NAME)
        line(WARN, '启动游戏后再执行 kof.cmd install；进程名不符请改 config.json')
        return
    line(OK, '检测到 %d 个进程：%s' % (len(pids), ', '.join(map(str, pids))))
    pid = pids[0]
    title = kofp.window_of(pid)
    line(OK, 'PID %d 窗口：%s' % (pid, title if title else '(无可见窗口/已最小化)'))
    try:
        h = kofp.open_proc(pid)
        head = kofp.read_mem(h, cfg.HOOK_SITE, len(cfg.HOOK_ORIG))
        if head == cfg.HOOK_ORIG:
            line(OK, '钩子点字节与预期一致：%s' % head.hex(' '))
        elif head[0] == 0xE9:
            line(WARN, '钩子点已被改写（可能已装过钩子）：%s' % head.hex(' '))
        else:
            line(BAD, '钩子点字节不符：%s（期望 %s）'
                 % (head.hex(' '), cfg.HOOK_ORIG.hex(' ')))
            fails.append('游戏版本与配置不匹配')
    except SystemExit as e:
        line(BAD, '读取游戏内存失败：%s' % e)
        fails.append('无法读取游戏内存')


def main():
    print('=' * 62)
    print('  kof2002um-bossfix · 环境自检')
    print('=' * 62)
    check_os()
    check_python()
    check_admin()
    check_config()
    check_game()
    print('\n' + '=' * 62)
    if fails:
        print('  结论：存在 %d 个关键问题 -> %s' % (len(fails), '；'.join(fails)))
        print('  处理方式见 README「常见问题」或 docs/03-兼容性与环境适配.md')
        print('=' * 62)
        return 1
    print('  结论：环境可用 ✔')
    print('=' * 62)
    return 0


if __name__ == '__main__':
    sys.exit(main())
