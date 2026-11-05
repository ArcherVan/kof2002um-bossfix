"""launch.py —— 一条命令：启动游戏 + 自动打好补丁

用法：
    launch.py                    启动游戏，装免扣气补丁，并持续把 1P 气槽顶满
    launch.py --no-power         只启动游戏，不打补丁
    launch.py --no-keep          打补丁，但不持续补气（回合开始仍需自己攒一格）
    launch.py --dmg 2.0          同时装伤害钩子并打开 2.0 倍补偿
    launch.py --exe <路径>       手动指定游戏 exe
    launch.py --steam            用 steam:// 协议启动（走 Steam 客户端，最稳）
    launch.py --timeout 120      等待引擎解密的最长秒数（默认 90）
    launch.py --new              已有游戏在跑时，仍然再启动一个
    launch.py --stdin            强制开启窗口命令（输入被重定向时也能用）

启动后留在窗口里可以直接敲命令（不必另开命令行）：
    2.5          设为 2.5 倍伤害并开启       off      关闭伤害补偿
    on [数字]    开启补偿 / 改倍率           power    立刻补满 1P 气槽
    status       查看补丁、倍率、气槽         quit     停止补气并退出

为什么不能"启动完立刻改"：
    本作 .text 在磁盘上是加密的（熵 7.9999），游戏进程起来后由加载器解密到
    0x401000 并置 RWX。解密没完成时写进去的字节可能被随后的解密覆盖，补丁看起来
    "打上了"其实白打。所以这里用"补丁位点的原始字节是否已就位"做就绪判断
    （0x620902 的伤害指令 + 三处扣气指令），而不是死等固定秒数；写完再等 1 秒复查
    一次，被覆盖就重装。

    判据的偶然命中概率可以忽略：需要 4 个位点同时等于原始字节，密文全中约 2^-200。
    实测（Win11 + NVMe）进程第一次可见时约 0.5s，此时已经解密完毕，所以通常
    这几行等待是瞬时的；慢盘/杀软扫描下才会真的停在这里。
"""
import ctypes
import ctypes.wintypes as w
import io
import os
import re
import subprocess
import sys
import time
from contextlib import redirect_stdout

import kofp
import power
import profile as cfg
import shell

EXE_NAME = cfg.PROCESS_NAME
POLL = 0.25                 # 就绪轮询间隔（秒）
LAUNCH_WAIT = 90.0          # 等待进程出现（Steam 冷启动可能很慢）
DIRECT_WAIT = 12.0          # 直接起 exe 的容忍时间，超时改用 Steam 协议重试

_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
_k32.WaitForSingleObject.restype = w.DWORD
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, w.DWORD]
_k32.CloseHandle.restype = w.BOOL
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

SYNCHRONIZE = 0x00100000
QUERY_LIMITED = 0x1000
WAIT_TIMEOUT = 0x102


# ---------------------------------------------------------------- 进程探活
def is_alive(pid):
    """进程还活着返回 True。

    不用 os.kill(pid, 0) —— 在 Windows 上它会真的发终止信号。
    """
    h = _k32.OpenProcess(SYNCHRONIZE | QUERY_LIMITED, False, pid)
    if not h:
        return False
    try:
        return _k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        _k32.CloseHandle(h)


# ---------------------------------------------------------------- 找游戏
def parse_library_vdf(text):
    """从 libraryfolders.vdf 里抽出所有库目录（新旧两种格式都认）"""
    paths = []
    for k, v in re.findall(r'"([^"]*)"\s+"([^"]*)"', text):
        v = v.replace('\\\\', '\\')
        if k.lower() == 'path':
            paths.append(v)
        elif k.isdigit() and re.match(r'^[A-Za-z]:[\\/]', v):
            paths.append(v)
    return paths


def _reg_steam_paths():
    out = []
    try:
        import winreg
    except ImportError:
        return out
    targets = (
        (winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Valve\Steam'),
    )
    for hive, key in targets:
        try:
            with winreg.OpenKey(hive, key) as k:
                for name in ('SteamPath', 'InstallPath'):
                    try:
                        v = winreg.QueryValueEx(k, name)[0]
                    except OSError:
                        continue
                    if isinstance(v, str) and v.strip():
                        out.append(v)
        except OSError:
            pass
    return out


def steam_roots():
    """Steam 本体目录 + 所有内容库目录（去重、保持顺序）"""
    seen, out = set(), []

    def add(p):
        if not p:
            return
        p = os.path.normpath(p)
        key = p.lower()
        if key not in seen and os.path.isdir(p):
            seen.add(key)
            out.append(p)

    for p in _reg_steam_paths():
        add(p)

    # 兜底：常见安装位置（注册表被清理过的绿色版）
    for p in (r'C:\Program Files (x86)\Steam',
              r'C:\Program Files\Steam',
              r'D:\Steam', r'D:\SteamLibrary',
              r'E:\Steam', r'E:\SteamLibrary'):
        add(p)

    # 从每个库的 libraryfolders.vdf 继续展开
    extra = []
    for root in list(out):
        vdf = os.path.join(root, 'steamapps', 'libraryfolders.vdf')
        try:
            with open(vdf, encoding='utf-8', errors='ignore') as fp:
                extra.extend(parse_library_vdf(fp.read()))
        except OSError:
            pass
    for p in extra:
        add(p)
    return out


def scan_exe(base, name=EXE_NAME, depth=3):
    """在 base 下（含自身）找指定文件名，大小写不敏感；找不到返回 None"""
    target = name.lower()
    level = [base]
    for _ in range(depth + 1):
        nxt = []
        for d in level:
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_file() and e.name.lower() == target:
                        return e.path
                    if e.is_dir():
                        nxt.append(e.path)
                except OSError:
                    continue
        level = nxt
        if not level:
            break
    return None


def find_exe(explicit=None):
    """按 命令行 > config.json > Steam 库 > 工具目录附近 的顺序定位游戏"""
    for cand in (explicit, cfg.P.get('game_exe')):
        if cand:
            cand = os.path.expanduser(os.path.expandvars(cand))
            if os.path.isfile(cand):
                return os.path.abspath(cand)
            if explicit:
                raise SystemExit('--exe 指向的文件不存在：%s' % cand)
            print('[warn] config.json 里的 game_exe 不存在：%s' % cand)
    for root in steam_roots():
        common = os.path.join(root, 'steamapps', 'common')
        hit = scan_exe(common, EXE_NAME, depth=2)
        if hit:
            return os.path.abspath(hit)
    # 兜底：绿色版/复制出来的游戏常和本工具放在一起
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (here, os.path.dirname(here),
                 os.getcwd(), os.path.dirname(os.getcwd())):
        hit = scan_exe(base, EXE_NAME, depth=2)
        if hit:
            return os.path.abspath(hit)
    return None


def appid_from_dir(exe):
    """游戏目录里若有 steam_appid.txt，用它作为启动协议号"""
    path = os.path.join(os.path.dirname(exe), 'steam_appid.txt')
    try:
        with open(path, encoding='ascii', errors='ignore') as fp:
            val = fp.read().strip().split()[0]
        return val if val.isdigit() else None
    except (OSError, IndexError):
        return None


# ---------------------------------------------------------------- 就绪判断
def assess(site, spends):
    """site = 伤害落地处字节；spends = 三处扣气点字节

    返回 'cold'（引擎还没解密完）/ 'ready'（原始状态，可打补丁）/ 'patched'
    """
    if not site:
        return 'cold'
    hook_ok = site == cfg.HOOK_ORIG or site[:1] == b'\xe9'
    spend_ok = bool(spends) and all(
        b == power.DEC or b[:1] == b'\xe8' for b in spends)
    if not (hook_ok and spend_ok):
        return 'cold'
    pristine = site == cfg.HOOK_ORIG and all(b == power.DEC for b in spends)
    return 'ready' if pristine else 'patched'


def probe(h):
    site = kofp.read_mem(h, cfg.HOOK_SITE, len(cfg.HOOK_ORIG))
    spends = [kofp.read_mem(h, s, len(power.DEC))
              for s, _ in power.SPEND_SITES]
    return assess(site, spends)


def _clear_line():
    sys.stdout.write('\r' + ' ' * 48 + '\r')
    sys.stdout.flush()


def wait_ready(h, timeout=LAUNCH_WAIT, quiet=False):
    t0 = time.time()
    tick = -1
    drawn = False
    # 重定向到文件/管道时不要用 \r 进度条，否则日志里会留下一长串空格
    tty = not quiet and sys.stdout.isatty()
    while True:
        try:
            state = probe(h)
        except SystemExit:
            state = 'cold'
        if state != 'cold':
            if drawn:
                _clear_line()
            return state, time.time() - t0
        elapsed = time.time() - t0
        if elapsed >= timeout:
            if drawn:
                _clear_line()
            return 'timeout', elapsed
        if tty:
            cur = int(elapsed * 2)
            if cur != tick:
                tick = cur
                drawn = True
                sys.stdout.write('\r  等待引擎解密 … %4.1fs' % elapsed)
                sys.stdout.flush()
        time.sleep(POLL)


# ---------------------------------------------------------------- 启动
def start_game(exe, appid=None):
    """启动游戏。给了 appid 走 Steam 协议，否则直接起 exe。"""
    if appid:
        os.startfile('steam://rungameid/%s' % appid)
        return
    flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen([exe], cwd=os.path.dirname(exe) or None,
                     close_fds=True, creationflags=flags)


def wait_new_pid(before, timeout=LAUNCH_WAIT):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for pid in kofp.find_pids(EXE_NAME):
            if pid not in before:
                return pid
        time.sleep(0.2)
    return None


def pick_pid(pids):
    for pid in pids:
        if kofp.window_of(pid):
            return pid
    return pids[0]


# ---------------------------------------------------------------- 补气
def refill(h, level=power.MAX_LEVEL):
    """1P 气槽低于 level 就写满；返回 True 表示写了"""
    addr = cfg.PLAYER_OBJ + cfg.POWER_OFFSET
    cur = kofp.read_mem(h, addr, 1)[0]
    if cur >= level:
        return False
    kofp.write_mem(h, addr, bytes([level]))
    return True


def post_check(h, delay=1.0):
    """写完再等一秒复查：防止"刚好撞上解密尾巴"被覆盖"""
    time.sleep(delay)
    try:
        return probe(h) == 'patched'
    except SystemExit:
        return False


def keep_full(pid, h, opt=None):
    """盯到游戏退出为止：气槽一掉就补满；同时接受窗口里敲的命令"""
    opt = opt or {}
    reader = None
    if opt.get('stdin') or sys.stdin.isatty():
        reader = shell.Reader()
        reader.start()
    ui = shell.Prompt() if (reader and sys.stdout.isatty()) else shell.Plain()

    ui.say('开始维持 1P 气槽（%d 格）。' % power.MAX_LEVEL)
    if reader:
        ui.say('本窗口可直接改倍率：输入 2.5 回车 = 2.5 倍；help 看全部命令，quit 退出。')
    else:
        ui.say('关掉这个窗口不影响补丁，只是不再自动补满。')
    topped = False
    try:
        while is_alive(pid):
            try:
                if refill(h) and not topped:
                    topped = True
                    ui.say('  已补满 1P 气槽')
            except SystemExit:
                pass                    # 对局切换瞬间读不到，忽略
            if reader is not None:
                line = reader.poll()
                while line is not None:
                    act, val = shell.parse(line)
                    if not handle(h, act, val, ui):
                        ui.say('已退出（补丁仍然有效，只是不再自动补气）。')
                        return
                    line = reader.poll()
            time.sleep(0.4)
    except KeyboardInterrupt:
        ui.clear()
        print('\n已停止维持（补丁仍然有效）。')
        return
    ui.clear()
    print('游戏进程已退出。')


# ---------------------------------------------------------------- 窗口内命令
def hook_installed(h):
    import hookdmg
    try:
        st = hookdmg.find_cave(h)
    except SystemExit:
        return False
    return bool(st) and not isinstance(st, str)


def capture(fn, *a):
    """把函数里的 print 收成行列表，便于用统一提示符输出"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a)
    return [l for l in buf.getvalue().splitlines() if l.strip()]


def status_lines(h):
    out = []
    st = 'cold'
    try:
        st = probe(h)
    except SystemExit:
        st = 'cold'
    out.append('补丁：%s' % {'patched': '已安装', 'ready': '未安装',
                             'cold': '读不到（对局切换中？）'}.get(st, st))
    try:
        lvl = kofp.read_mem(h, cfg.PLAYER_OBJ + cfg.POWER_OFFSET, 1)[0]
        out.append('1P 气槽：%d / %d' % (lvl, power.MAX_LEVEL))
    except SystemExit:
        out.append('1P 气槽：读不到')
    import hookdmg
    on, fac = hookdmg.comp_state(h)
    if fac is None:
        out.append('伤害倍率：钩子未装 —— 输入 2 即装上并开 2 倍')
    else:
        out.append('伤害倍率：%s   %g 倍' % ('开' if on else '关', fac))
    return out


def handle(h, act, val, ui):
    """执行一条窗口命令；返回 False 表示用户要退出"""
    import hookdmg

    if act == 'quit':
        return False
    if act == 'help':
        ui.say(*shell.HELP)
        return True
    if act == 'status':
        ui.say(*status_lines(h))
        return True
    if act == 'power':
        addr = cfg.PLAYER_OBJ + cfg.POWER_OFFSET
        kofp.write_mem(h, addr, bytes([power.MAX_LEVEL]))
        ui.say('1P 气槽已补满（%d 格）' % power.MAX_LEVEL)
        return True
    if act == 'off':
        if not hook_installed(h):
            ui.say('钩子还没装，没有可关的补偿。输入 2 就会装上并开 2 倍。')
            return True
        ui.say(*capture(hookdmg.set_comp, h, False))
        return True

    # act == 'on'
    try:
        if not hook_installed(h):
            ui.say('伤害钩子还没装，先装上（几十毫秒）…')
            hookdmg.install(h)
        _, cur = hookdmg.comp_state(h)
        f = shell.clamp(val if val is not None
                        else (cur if cur else cfg.DEFAULT_FACTOR))
        ui.say(*capture(hookdmg.set_comp, h, True, f))
        if f >= 4.0:
            ui.say('  提示：%g 倍已属一击必杀级别，嫌太狠就输入 2 调回来。' % f)
    except SystemExit as e:
        ui.say('[!] %s' % e)
    return True


# ---------------------------------------------------------------- 参数
def parse_args(argv):
    opt = {'exe': None, 'no_power': False, 'no_keep': False, 'dmg': None,
           'timeout': LAUNCH_WAIT, 'new': False, 'steam': False,
           'stdin': False, 'help': False}
    args = list(argv[1:])
    if args and args[0].lower() in ('launch', 'run', 'start'):
        args.pop(0)
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('-h', '--help'):
            opt['help'] = True
        elif a == '--exe':
            i += 1
            opt['exe'] = args[i] if i < len(args) else None
        elif a == '--dmg':
            nxt = args[i + 1] if i + 1 < len(args) else ''
            try:
                opt['dmg'] = float(nxt)
                i += 1
            except ValueError:
                opt['dmg'] = cfg.DEFAULT_FACTOR
        elif a == '--timeout':
            i += 1
            try:
                opt['timeout'] = float(args[i])
            except (ValueError, IndexError):
                pass
        elif a == '--no-power':
            opt['no_power'] = True
        elif a == '--no-keep':
            opt['no_keep'] = True
        elif a == '--new':
            opt['new'] = True
        elif a == '--steam':
            opt['steam'] = True
        elif a == '--stdin':
            opt['stdin'] = True
        i += 1
    return opt


# ---------------------------------------------------------------- 主流程
def main():
    opt = parse_args(sys.argv)
    if opt['help']:
        print(__doc__)
        return

    print('=== 拳皇 2002 UM 一键启动 ===')
    running = kofp.find_pids(EXE_NAME)

    if running and not opt['new']:
        pid = pick_pid(running)
        print('游戏已在运行（PID %d），跳过启动，直接打补丁' % pid)
    else:
        exe = find_exe(opt['exe'])
        if not exe:
            raise SystemExit(
                '没找到 %s。\n'
                '  办法一：在 config.json 里写 "game_exe": "D:\\...\\%s"\n'
                '  办法二：kof.cmd launch --exe "<游戏完整路径>"\n'
                '  （Steam 装在非默认位置时也会自动扫，但需要注册表里的 Steam 路径）'
                % (EXE_NAME, EXE_NAME))
        print('游戏路径：%s' % exe)
        appid = cfg.P.get('steam_appid') or appid_from_dir(exe)
        if opt['steam'] and not appid:
            print('[warn] 没拿到 appid，--steam 无效，改为直接启动 exe')
        before = set(kofp.find_pids(EXE_NAME))
        if appid and opt['steam']:
            print('通过 Steam 启动（appid %s），首次可能要等云同步…' % appid)
            start_game(exe, appid)
            pid = wait_new_pid(before, LAUNCH_WAIT)
        else:
            print('正在启动…')
            start_game(exe)
            pid = wait_new_pid(before, DIRECT_WAIT)
            if not pid and appid:
                print('直接启动没起来（多见于 Steam DRM 拦截），改用 Steam 协议重试…')
                start_game(exe, appid)
                pid = wait_new_pid(before, LAUNCH_WAIT)
        if not pid:
            raise SystemExit('等了 %.0f 秒也没看到 %s 进程。\n'
                             '· 若游戏要求先登录 Steam，请先开 Steam 再重试\n'
                             '· 若弹了错误框，用 --steam 或 config.json 里的 steam_appid 启动'
                             % (LAUNCH_WAIT, EXE_NAME))
        print('已启动：PID %d' % pid)

    h = kofp.open_proc(pid)
    state, elapsed = wait_ready(h, opt['timeout'])
    if state == 'timeout':
        raise SystemExit('等了 %.0f 秒引擎仍未解密，放弃（游戏可能还在读盘，'
                         '可以加大 --timeout 后重试）' % elapsed)
    print('引擎已就绪（%.1fs）' % elapsed)

    if opt['no_power']:
        print('按 --no-power 跳过补丁。')
        return

    power.install(h)
    if not post_check(h):
        print('[!] 写入后复查不通过（多半是引擎还在解密），重试一次…')
        power.install(h)
        if not post_check(h):
            raise SystemExit('补丁写入后被覆盖，请把上面的输出发我（可能版本不一致）')
    if opt['dmg'] is not None:
        import hookdmg
        print('')
        hookdmg.install(h)
        hookdmg.set_comp(h, True, opt['dmg'])

    print('')
    if state == 'patched':
        print('补丁原本就在（同一个进程里重复执行过），已确认状态。')
    else:
        print('全部完成，直接进游戏即可。')
    if opt['no_keep']:
        print('（--no-keep：不自动补气，也没有窗口命令；'
              '想边玩边改倍率就去掉这个参数）')
        return
    keep_full(pid, h, opt)


if __name__ == '__main__':
    try:
        main()
    except SystemExit as e:
        if str(e):
            print(str(e), file=sys.stderr)
        sys.exit(1 if str(e) else 0)
