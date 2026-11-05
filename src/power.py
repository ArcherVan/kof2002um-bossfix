"""power.py —— 气槽（能量格）控制

用法：
    power.py show              查看双方血量、气槽格数、免耗气标志
    power.py on [5] [p1]       即时补满 + 置免耗气标志（写数据，临时）
    power.py off [p1]          关闭免耗气标志（格数不动）
    power.py set 3 [p1]        只写格数（0~5）
    power.py patch             安装免扣气补丁（改代码，一次有效到关游戏）
    power.py unpatch           还原补丁

两种模式的区别：
    on   —— 只写角色对象，不改代码。每回合游戏重置后需要重跑。
    patch —— 改代码，跳过扣气指令。装一次就够，整局有效，不用再管。
            只对 1P 放行，CPU 仍正常扣气。

原理：
    角色对象 +0x1E3  字节，当前气槽格数（0~5）。
    角色对象 +0x1EE  字节，免耗气标志，为 1 时游戏跳过扣气（BOSS 待遇）。

    扣气共三处，都是 `dec byte [eax+0x1E3]`（6 字节）：
        0x430D60  超必杀结束
        0x430DA3  MAX 曝气
        0x60AC51  通用扣气
    三处都被替换成 `call <共享代码洞>` + 1 个 NOP，洞内比对 eax 是否为
    1P 对象，是则直接 ret（跳过扣减），否则照原样执行 dec 再 ret。
"""
import ctypes
import struct
import sys

import kofp
import profile as cfg

POWER_OFF = cfg.POWER_OFFSET
FLAG_OFF = cfg.POWER_FLAG_OFFSET
HP_OFF = cfg.HP_OFFSET
MAX_LEVEL = 5

# ---------------------------------------------------------------- 补丁定义
DEC = bytes.fromhex('fe88e3010000')          # dec byte [eax+0x1E3]
SPEND_SITES = (
    (0x430D60, '超必杀'),
    (0x430DA3, 'MAX 曝气'),
    (0x60AC51, '通用扣气'),
)
CAVE_SIZE = 0x1000
CAVE_SIZE_LEN = 14                            # cmp(5) + je(2) + dec(6) + ret(1)


def _k32():
    return kofp.k32


def _alloc(h, size=CAVE_SIZE):
    f = _k32().VirtualAllocEx
    f.restype = ctypes.c_void_p
    p = f(h, None, size, 0x3000, 0x40)        # COMMIT|RESERVE, RWX
    if not p:
        raise SystemExit('VirtualAllocEx 失败（错误码 %d）' % ctypes.get_last_error())
    return int(p)


def _free(h, va):
    _k32().VirtualFreeEx(h, ctypes.c_void_p(va), 0, 0x8000)


def _rd(h, va, n):
    return kofp.read_mem(h, va, n)


def _wr(h, va, data):
    kofp.write_mem(h, va, data)


def cave_bytes(p1obj):
    """共享代码洞：eax == 1P 对象时跳过扣气，否则照原样扣。

    布局（14 字节）：
        0  cmp eax, p1obj
        5  je  +6          → 跳到 13
        7  dec byte [eax+0x1E3]
       13  ret
    hook 用 call，返回地址落在 hook 点后的 NOP 上，再滑到原指令之后。
    """
    b = bytearray()
    b += b'\x3d' + struct.pack('<I', p1obj)  # cmp eax, imm32
    b += b'\x74\x06'                          # je +6
    b += DEC
    b += b'\xc3'                              # ret
    return bytes(b)


def hook_bytes(site, cave):
    rel = cave - (site + 5)
    return b'\xe8' + struct.pack('<i', rel) + b'\x90'


def _looks_like_cave(blob):
    return len(blob) >= CAVE_SIZE_LEN and blob[5:14] == b'\x74\x06' + DEC + b'\xc3'


def find_cave(h):
    """顺着第一处 hook 的 call 找洞；返回洞地址，未安装返回 None。"""
    cur = _rd(h, SPEND_SITES[0][0], 6)
    if cur == DEC:
        return None
    if cur[0] != 0xE8:
        raise SystemExit('0x%08X 上既不是原指令也不是补丁：%s\n'
                         '（游戏被更新过？把这一行发我）'
                         % (SPEND_SITES[0][0], cur.hex(' ')))
    target = SPEND_SITES[0][0] + 5 + struct.unpack('<i', cur[1:5])[0]
    try:
        head = _rd(h, target, CAVE_SIZE_LEN)
    except SystemExit:
        return 'dead'
    if not _looks_like_cave(head):
        return 'dead'
    return target


def status(h):
    cave = find_cave(h)
    if cave == 'dead':
        print('补丁状态：失效（hook 指向的内存已释放）')
        return
    if not cave:
        print('补丁状态：未安装')
        for site, name in SPEND_SITES:
            print('  0x%08X  %-10s %s' % (site, name, _rd(h, site, 6).hex(' ')))
        return
    print('补丁状态：已安装   代码洞 = 0x%08X' % cave)
    print('  洞内机器码  %s' % _rd(h, cave, CAVE_SIZE_LEN).hex(' '))
    for site, name in SPEND_SITES:
        b = _rd(h, site, 6)
        ok = b[0] == 0xE8 and site + 5 + struct.unpack('<i', b[1:5])[0] == cave
        print('  0x%08X  %-10s %s  %s' % (site, name, b.hex(' '), 'OK' if ok else '不一致'))
    print('\n说明：只对 1P 免扣气，CPU 仍正常消耗。')


def install(h):
    st = find_cave(h)
    if st == 'dead':
        print('[!] 检测到失效 hook，先还原再重装')
        restore(h)
        st = None
    if st:
        print('补丁已存在，代码洞 = 0x%08X，无需重装' % st)
        status(h)
        return
    for site, name in SPEND_SITES:
        cur = _rd(h, site, 6)
        if cur != DEC:
            raise SystemExit('0x%08X (%s) 字节不符：%s\n期望 %s'
                             % (site, name, cur.hex(' '), DEC.hex(' ')))
    cave = _alloc(h)
    _wr(h, cave, cave_bytes(cfg.PLAYER_OBJ))
    back = _rd(h, cave, CAVE_SIZE_LEN)
    if back != cave_bytes(cfg.PLAYER_OBJ):
        _free(h, cave)
        raise SystemExit('代码洞写入校验失败：%s' % back.hex(' '))
    for site, name in SPEND_SITES:
        _wr(h, site, hook_bytes(site, cave))
    print('补丁已安装：代码洞 0x%08X，%d 处扣气点已接管' % (cave, len(SPEND_SITES)))
    # 顺带把 1P 气槽补满：补丁只保证“不扣”，开局若是 0 格仍需先攒一格
    try:
        _write(h, 'p1', POWER_OFF, MAX_LEVEL)
        _write(h, 'p1', FLAG_OFF, 1)
        print('1P 气槽已补满 %d 格' % MAX_LEVEL)
    except SystemExit:
        print('（对局尚未开始，气槽补满已跳过）')
    status(h)


def restore(h):
    st = find_cave(h)
    if not st:
        print('未安装补丁，无需还原')
        return
    cave = None if st == 'dead' else st
    for site, name in SPEND_SITES:
        _wr(h, site, DEC)
    for site, name in SPEND_SITES:
        got = _rd(h, site, 6)
        print('  0x%08X  还原为 %s  %s' % (site, got.hex(' '), 'OK' if got == DEC else '失败'))
    if cave:
        _free(h, cave)
        print('代码洞 0x%08X 已释放' % cave)
    print('补丁已移除，扣气恢复原样。')


# ---------------------------------------------------------------- 数据写入
def _targets():
    return {'p1': cfg.PLAYER_OBJ, 'p2': cfg.ENEMY_OBJ}


def _open():
    pid = kofp.find_pid()
    if not pid:
        raise SystemExit('未找到游戏进程，请先启动游戏。')
    return kofp.open_proc(pid)


def _state(h, base):
    hp = int.from_bytes(_rd(h, base + HP_OFF, 2), 'little', signed=True)
    pw = _rd(h, base + POWER_OFF, 1)[0]
    fl = _rd(h, base + FLAG_OFF, 1)[0]
    return hp, pw, fl


def show(h):
    print('         HP     气槽   免耗气')
    for name in ('p1', 'p2'):
        hp, pw, fl = _state(h, _targets()[name])
        print('  %-4s  %-6d %d 格   %s'
              % (name.upper(), hp, pw, '是' if fl else '否'))


def _write(h, side, off, value):
    _wr(h, _targets()[side] + off, bytes([value]))


def _side(argv, idx):
    if len(argv) > idx and argv[idx] in ('p1', 'p2'):
        return argv[idx]
    return 'p1'


def main():
    argv = sys.argv
    cmd = argv[1] if len(argv) > 1 else 'show'
    h = _open()

    if cmd == 'show':
        show(h)

    elif cmd == 'on':
        side = _side(argv, 3)
        level = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else MAX_LEVEL
        level = max(0, min(MAX_LEVEL, level))
        _write(h, side, POWER_OFF, level)
        _write(h, side, FLAG_OFF, 1)
        print('%s 已开启无限气：%d 格 + 免耗气标志' % (side.upper(), level))

    elif cmd == 'off':
        side = _side(argv, 2)
        _write(h, side, FLAG_OFF, 0)
        print('%s 已关闭免耗气（格数保持不变）' % side.upper())

    elif cmd == 'set':
        side = _side(argv, 3)
        level = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else MAX_LEVEL
        level = max(0, min(MAX_LEVEL, level))
        _write(h, side, POWER_OFF, level)
        print('%s 气槽已写入 %d 格' % (side.upper(), level))

    elif cmd in ('patch', 'install'):
        install(h)

    elif cmd in ('unpatch', 'restore'):
        restore(h)

    elif cmd in ('status', 'patch-status'):
        status(h)

    else:
        print(__doc__)


if __name__ == '__main__':
    main()
