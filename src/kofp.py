"""kofp.py —— 拳皇2002UM（Steam / Code Mystics 移植）运行时内存补丁工具

为什么必须在运行时改：
    本作 KingOfFighters2002UM.exe 的 .text 在磁盘上是加密的（熵 7.9999，流密码），
    游戏启动后由加载器解密到 0x401000 并置为 RWX。磁盘上直接改字节 = 改密文，
    游戏必然无法启动。所以补丁只能在进程内对已解密的代码下刀。

用法：
    python kofp.py status             # 查看每个补丁位点当前的字节
    python kofp.py apply  halve       # 只应用某一组（见 PATCHES 的 group）
    python kofp.py apply  all         # 应用全部
    python kofp.py restore all        # 还原全部
    python kofp.py test               # 自检：在 .text 尾部填充区写读一次
    python kofp.py read  0x628200 32  # 读内存
    python kofp.py write 0x62824E 90 90 90 90 90

标的说明（均由本仓库的反汇编分析得出，VA 为运行时绝对地址）：
    伤害落地     0x6208E3..0x62092A   HP(word) = [fighter+0x138] -= 伤害
    伤害后处理   0x628200..0x628264   ×0.5 / ×0.75 两档伤害修正
    ×0.5 来源    0x60DA06             [fighter+0x11D] |= 1        （全代码唯一置位点）
    ×0.75 来源   0x60DA1C             [fighter+0x0E6] |= 1
    修正清除     0x63EB00             [+0xE6] &= ~0x40 ; [+0x11D] &= ~1
"""
import ctypes
import ctypes.wintypes as w
import json
import os
import sys

k32 = ctypes.WinDLL('kernel32', use_last_error=True)

PROCESS_ALL = 0x1F0FFF
PIDFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pid.json')

NOP5 = b'\x90' * 5
NOP7 = b'\x90' * 7

# ---------------------------------------------------------------- 补丁表
# (group, 名称, 运行时VA, 期望原字节, 替换字节, 说明)
PATCHES = [
    ('halve', '伤害×0.5 源头禁用', 0x0060DA06,
     bytes.fromhex('80881d010000 01'.replace(' ', '')),
     NOP7,
     '[角色子对象+0x11D] |= 1 —— 全代码唯一置位点，NOP 后伤害不再被打对折'),

    ('halve', '伤害×0.5 分支阻断', 0x0062824E,
     bytes.fromhex('e9edfbffff'),
     NOP5,
     'jmp 0x627E40（$v0 >>= 1）—— 二次保险，直接跳过折半'),

    ('scale75', '伤害×0.75 源头禁用', 0x0060DA1C,
     bytes.fromhex('8088e60000 0001'.replace(' ', '')),
     NOP7,
     '[角色子对象+0x0E6] |= 1 —— 0.75 档修正的置位点'),

    ('scale75', '伤害×0.75 分支阻断', 0x00628264,
     bytes.fromhex('e9f7fbffff'),
     NOP5,
     'jmp 0x627E60（$v0 = $v0/2 + $v0/4）'),
]


# ---------------------------------------------------------------- 基础
try:
    import profile as _cfg
    _DEFAULT_EXE = _cfg.PROCESS_NAME
except Exception:                       # 单独运行 kofp.py 且 config 异常时的兜底
    _DEFAULT_EXE = 'KingOfFighters2002UM.exe'


def find_pid(name=None):
    name = name or _DEFAULT_EXE
    if os.path.exists(PIDFILE):
        try:
            pid = json.load(open(PIDFILE))['pid']
            h = k32.OpenProcess(PROCESS_ALL, False, pid)
            if h:
                k32.CloseHandle(h)
                return pid
        except Exception:
            pass
    import subprocess
    try:
        out = subprocess.check_output(['tasklist', '/FO', 'CSV', '/NH'], text=True,
                                      encoding='gbk', errors='ignore')
    except Exception:
        return None
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0].lower() == name.lower():
            return int(parts[1])
    return None


def find_pids(name=None):
    """返回全部同名进程 PID（多开会同时返回；避免打到残留实例上）"""
    name = name or _DEFAULT_EXE
    import subprocess
    pids = []
    try:
        out = subprocess.check_output(['tasklist', '/FO', 'CSV', '/NH'], text=True,
                                      encoding='gbk', errors='ignore')
    except Exception:
        return pids
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0].lower() == name.lower():
            try:
                pids.append(int(parts[1]))
            except (IndexError, ValueError):
                pass
    return pids


def window_of(pid):
    """返回该进程的可见窗口标题（没有则 None），用来区分哪个实例是真的在玩"""
    u32 = ctypes.WinDLL('user32', use_last_error=True)
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, l):
        p = w.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(h, buf, 256)
            r = w.RECT()
            u32.GetWindowRect(h, ctypes.byref(r))
            if r.right - r.left > 200:
                found.append(buf.value or '(无标题)')
        return True

    u32.EnumWindows(cb, 0)
    return found[0] if found else None


def open_proc(pid):
    h = k32.OpenProcess(PROCESS_ALL, False, pid)
    if not h:
        raise SystemExit('OpenProcess 失败，错误码 %d（请用管理员权限运行）' % ctypes.get_last_error())
    return h


def read_mem(h, va, n):
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(va), buf, n, ctypes.byref(got)):
        raise SystemExit('ReadProcessMemory 失败 @0x%X' % va)
    return buf.raw[:got.value]


def write_mem(h, va, data):
    n = ctypes.c_size_t(0)
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    old = w.DWORD(0)
    if not k32.VirtualProtectEx(h, ctypes.c_void_p(va), len(data), 0x40, ctypes.byref(old)):
        raise SystemExit('VirtualProtectEx 失败 @0x%X' % va)
    ok = k32.WriteProcessMemory(h, ctypes.c_void_p(va), buf, len(data), ctypes.byref(n))
    k32.VirtualProtectEx(h, ctypes.c_void_p(va), len(data), old.value, ctypes.byref(old))
    if not ok:
        raise SystemExit('WriteProcessMemory 失败 @0x%X' % va)
    return n.value


def select(group):
    if group in (None, 'all'):
        return PATCHES
    sel = [p for p in PATCHES if p[0] == group]
    if not sel:
        raise SystemExit('没有名为 %r 的补丁组，可选：all / %s'
                         % (group, ' / '.join(sorted({p[0] for p in PATCHES}))))
    return sel


# ---------------------------------------------------------------- 命令
def cmd_status(h):
    print('%-6s %-18s %-10s %-24s %-24s' % ('组', '名称', 'VA', '当前字节', '补丁字节'))
    for g, name, va, old, new, _ in PATCHES:
        cur = read_mem(h, va, len(old))
        tag = '已打' if cur == new else ('原始' if cur == old else '??未知')
        print('%-6s %-18s 0x%08X %-24s %-24s [%s]'
              % (g, name, va, cur.hex(' '), new.hex(' '), tag))


def cmd_apply(h, group):
    for g, name, va, old, new, desc in select(group):
        cur = read_mem(h, va, len(old))
        if cur == new:
            print('跳过（已应用）0x%08X %s' % (va, name))
            continue
        if cur != old:
            print('跳过（原字节不符，可能不是同一版本）0x%08X 实际 %s 期望 %s'
                  % (va, cur.hex(' '), old.hex(' ')))
            continue
        write_mem(h, va, new)
        print('已打补丁 0x%08X  %-18s %s' % (va, name, desc))


def cmd_restore(h, group):
    for g, name, va, old, new, _ in select(group):
        cur = read_mem(h, va, len(old))
        if cur == old:
            print('跳过（本来就是原样）0x%08X %s' % (va, name))
            continue
        if cur != new:
            print('跳过（当前字节既非原样也非补丁）0x%08X %s' % (va, cur.hex(' ')))
            continue
        write_mem(h, va, old)
        print('已还原 0x%08X  %s' % (va, name))


def cmd_test(h):
    va = 0x401000 + 0x2FDF00          # .text 尾部零填充，永不执行
    orig = read_mem(h, va, 4)
    print('原值 @0x%08X : %s' % (va, orig.hex(' ')))
    probe = b'\x90' * 4
    write_mem(h, va, probe)
    back = read_mem(h, va, 4)
    print('写入后      : %s' % back.hex(' '))
    write_mem(h, va, orig)
    restored = read_mem(h, va, 4)
    print('已还原      : %s' % restored.hex(' '))
    print('结论：%s' % ('补丁通道可用 ✔' if back == probe and restored == orig else '补丁通道异常 ✘'))


def run_one(pid, cmd, arg):
    title = window_of(pid)
    print('--- PID %d  窗口：%s ---' % (pid, title if title else '(无/最小化)'))
    h = open_proc(pid)
    if cmd == 'status':
        cmd_status(h)
    elif cmd == 'apply':
        cmd_apply(h, arg)
    elif cmd == 'restore':
        cmd_restore(h, arg)
    elif cmd == 'test':
        cmd_test(h)
    elif cmd == 'read':
        va = int(sys.argv[2], 0)
        n = int(sys.argv[3], 0) if len(sys.argv) > 3 else 16
        b = read_mem(h, va, n)
        for i in range(0, len(b), 16):
            print('%08X  %s' % (va + i, b[i:i + 16].hex(' ')))
    elif cmd == 'write':
        va = int(sys.argv[2], 0)
        data = bytes(int(x, 16) for x in sys.argv[3:])
        write_mem(h, va, data)
        print('已写入 %d 字节 @0x%08X' % (len(data), va))
    else:
        print(__doc__)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    pids = find_pids()
    if not pids:
        raise SystemExit('未找到 KingOfFighters2002UM.exe，请先启动游戏。')
    if len(pids) > 1:
        print('发现 %d 个游戏进程：%s（将逐个处理）' % (len(pids), pids))
    for pid in pids:
        run_one(pid, cmd, arg)


if __name__ == '__main__':
    main()
