"""hookdmg.py —— 在"伤害落地"指令上做钩子，直接读出每次命中的真实伤害

为什么需要它：
    屏幕上看数字/截图都不可靠，而伤害落地指令只有一条：
        0x620902  66 29 90 38 01 00 00   sub word ptr [eax+0x138], dx
    其中 dx = 本次伤害，eax = 受击角色对象（HP 在 +0x138）。
    在这里挂钩，就能得到"实际打进去多少伤害"的精确序列，不需要看画面。

    同时记录引擎的"当前对象槽" [0x21E3414]：
        0x628200 的 ×0.5 修正链就是读 [0x21E3414]+0x11D 的位 0 来决定折半的。
        把它和受击方地址一比，就能判定那条修正链针对的是攻击方还是受击方，
        这决定了"玩家选 BOSS 伤害减半"到底该改哪里。

    受击对象地址可反推攻击方：P1 对象固定 0x167D360，P2 对象固定 0x167D580
    （0x608010 的初始化代码里直接写死这两个地址）。

cave 布局（0x1000 字节）：
    +0x000  机器码（0x620902 的 jmp 跳到这里）
    +0x400  ring A：64 个 dword，eax（受击对象地址）
    +0x500  ring D：64 个 dword，edx（伤害）
    +0x600  计数器
    +0x700  ring B：64 个 dword，[0x21E3414]（当前对象槽）
    +0x800  版本标记 'HKD2'

自愈：不依赖任何外部状态文件。若 0x620902 已是 jmp，脚本顺着跳转找 cave，
    校验机器码 + 版本标记后直接"认领"；旧版或失效则先还原再重装。

用法：
    python hookdmg.py status        # 看钩子在不在、已记录多少
    python hookdmg.py install       # 安装（可重复执行）
    python hookdmg.py watch 120     # 边玩边看
    python hookdmg.py dump          # 打印缓冲区里已有的记录
    python hookdmg.py uninstall     # 拆除并逐字节还原
"""
import ctypes
import ctypes.wintypes as w
import os
import struct
import sys
import time

import kofp
import profile as cfg

k32 = ctypes.WinDLL('kernel32', use_last_error=True)

# ---- 版本相关常量：全部来自 config.json / 内置默认值（见 src/profile.py） ----
SITE = cfg.HOOK_SITE                # 伤害落地指令，钩子挂在这里
ORIG = cfg.HOOK_ORIG                # 该处原始指令（校验用）
SITE_LEN = len(ORIG)
BACK = cfg.HOOK_BACK                # 原指令之后的下一条

RING = 64
OFF_A = 0x400       # 受击方对象地址
OFF_D = 0x500       # 伤害值
OFF_CTR = 0x600     # 计数器
OFF_B = 0x700       # [0x21E3414] 当前对象槽
OFF_MAGIC = 0x800
OFF_FLAG = 0x810    # 补偿开关 dword：0=关，非0=开
OFF_FACTOR = 0x814  # 补偿倍率 dword，256 = ×1.0
MAGIC = b'HKD3'
CAVE_SIZE = 0x1000

OBJSLOT = cfg.OBJ_SLOT              # 引擎"当前角色对象"全局
P1 = cfg.PLAYER_OBJ                 # 玩家 1 角色对象
P2 = cfg.ENEMY_OBJ                  # 玩家 2 / CPU 角色对象
HP_OFF = cfg.HP_OFFSET

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = cfg.LOG_PATH


# ---------------------------------------------------------------- 基础
def alloc(h, size=CAVE_SIZE):
    k32.VirtualAllocEx.restype = ctypes.c_void_p
    p = k32.VirtualAllocEx(h, None, size, 0x3000, 0x40)   # COMMIT|RESERVE, RWX
    if not p:
        raise SystemExit('VirtualAllocEx 失败（错误码 %d）' % ctypes.get_last_error())
    return int(p)


def free(h, va):
    k32.VirtualFreeEx(h, ctypes.c_void_p(va), 0, 0x8000)


def rd(h, va, n):
    return kofp.read_mem(h, va, n)


def build_cave(cave):
    ring_a = cave + OFF_A
    ring_d = cave + OFF_D
    ring_b = cave + OFF_B
    ctr = cave + OFF_CTR
    flag = cave + OFF_FLAG
    fac = cave + OFF_FACTOR

    code = b''
    code += b'\x9c'                                       # pushfd
    code += b'\x51\x52'                                   # push ecx ; push edx
    code += b'\x8b\x0d' + struct.pack('<I', ctr)          # mov ecx, [ctr]
    code += b'\x83\xe1' + bytes([RING - 1])               # and ecx, 63
    code += b'\x89\x04\x8d' + struct.pack('<I', ring_a)   # mov [ecx*4+ringA], eax
    code += b'\x89\x14\x8d' + struct.pack('<I', ring_d)   # mov [ecx*4+ringD], edx
    code += b'\x8b\x15' + struct.pack('<I', OBJSLOT)      # mov edx, [0x21E3414]
    code += b'\x89\x14\x8d' + struct.pack('<I', ring_b)   # mov [ecx*4+ringB], edx
    code += b'\xff\x05' + struct.pack('<I', ctr)          # inc dword [ctr]

    # ---- 伤害补偿块：仅当 开关非0 且 受击方==P2（即玩家在攻击）时放大 dx ----
    comp = bytearray()
    comp += b'\x83\x3d' + struct.pack('<I', flag) + b'\x00'   # cmp dword [flag], 0
    comp += b'\x74\x00'
    je_at = len(comp) - 1
    comp += b'\x3d' + struct.pack('<I', P2)                   # cmp eax, P2
    comp += b'\x75\x00'
    jne_at = len(comp) - 1
    comp += b'\x0f\xb7\x0c\x24'                               # movzx ecx, word [esp]
    comp += b'\x0f\xaf\x0d' + struct.pack('<I', fac)          # imul ecx, [factor]
    comp += b'\xc1\xe9\x08'                                   # shr ecx, 8
    comp += b'\x89\x0c\x24'                                   # mov [esp], ecx
    done = len(comp)
    comp[je_at] = done - (je_at + 1)
    comp[jne_at] = done - (jne_at + 1)
    code += bytes(comp)

    code += b'\x5a\x59\x9d'                               # pop edx ; pop ecx ; popfd
    code += ORIG                                          # 原指令
    rel = BACK - (cave + len(code) + 5)
    code += b'\xe9' + struct.pack('<i', rel)              # jmp back
    return code


def looks_like_ours(b):
    if len(b) < 20 or b[:3] != b'\x9c\x51\x52':
        return False
    i = b.find(ORIG)
    while i != -1:
        if i + 7 < len(b) and b[i + 7] == 0xE9:
            return True
        i = b.find(ORIG, i + 1)
    return False


def find_cave(h):
    """None=未装；'dead'=跳向已释放内存；'old'=旧版；dict=当前版本"""
    cur = rd(h, SITE, SITE_LEN)
    if cur == ORIG:
        return None
    if cur[0] != 0xE9:
        raise SystemExit('0x%08X 上的字节既不是原指令也不是钩子：%s\n'
                         '（若游戏刚被 Steam 更新过，请把这一行发我）'
                         % (SITE, cur.hex(' ')))
    target = SITE + 5 + struct.unpack('<i', cur[1:5])[0]
    try:
        head = rd(h, target, 160)
        magic = rd(h, target + OFF_MAGIC, 4)
    except SystemExit:
        return 'dead'
    if not looks_like_ours(head) or magic != MAGIC:
        return 'old'
    return {'cave': target, 'ctr': target + OFF_CTR,
            'ringA': target + OFF_A, 'ringD': target + OFF_D,
            'ringB': target + OFF_B}


def revive(h):
    kofp.write_mem(h, SITE, ORIG)
    back = rd(h, SITE, SITE_LEN)
    print('落地地点已还原：%s %s' % (back.hex(' '), '✔' if back == ORIG else '✘'))


def install(h):
    st = find_cave(h)
    if st in ('dead', 'old'):
        why = '指向已释放内存的失效跳转' if st == 'dead' else '旧版本钩子'
        print('[!] 0x%08X 上是%s，先还原再重装' % (SITE, why))
        revive(h)
        st = None
    if st:
        print('钩子已存在，直接认领：cave=0x%08X' % st['cave'])
        print('落地点字节：%s' % rd(h, SITE, SITE_LEN).hex(' '))
        return st
    cave = alloc(h)
    code = build_cave(cave)
    kofp.write_mem(h, cave, code)
    kofp.write_mem(h, cave + OFF_A, b'\x00' * (OFF_MAGIC - OFF_A))
    kofp.write_mem(h, cave + OFF_MAGIC, MAGIC)
    kofp.write_mem(h, cave + OFF_FLAG, struct.pack('<II', 0, 256))   # 默认关闭，倍率 1.0
    rel = cave - (SITE + 5)
    kofp.write_mem(h, SITE, b'\xe9' + struct.pack('<i', rel) + b'\x90\x90')
    back = rd(h, SITE, SITE_LEN)
    if back[0] != 0xE9:
        raise SystemExit('写入后校验失败：%s' % back.hex(' '))
    print('钩子已安装：cave=0x%08X  钩子点=0x%08X' % (cave, SITE))
    return {'cave': cave, 'ctr': cave + OFF_CTR,
            'ringA': cave + OFF_A, 'ringD': cave + OFF_D,
            'ringB': cave + OFF_B}


def uninstall(h):
    st = find_cave(h)
    if st in ('dead', 'old'):
        revive(h)
        return
    if not st:
        print('没有钩子，无需拆除；落地点字节：%s' % rd(h, SITE, SITE_LEN).hex(' '))
        return
    kofp.write_mem(h, SITE, ORIG)
    back = rd(h, SITE, SITE_LEN)
    print('落地地点已还原：%s %s' % (back.hex(' '), '✔' if back == ORIG else '✘'))
    free(h, st['cave'])
    print('cave 0x%08X 已释放' % st['cave'])


# ---------------------------------------------------------------- 读记录
def counter(h, st):
    return struct.unpack('<I', rd(h, st['ctr'], 4))[0]


def entry(h, st, k):
    i = k & (RING - 1)
    ea = struct.unpack('<I', rd(h, st['ringA'] + i * 4, 4))[0]
    ed = struct.unpack('<I', rd(h, st['ringD'] + i * 4, 4))[0]
    eb = struct.unpack('<I', rd(h, st['ringB'] + i * 4, 4))[0]
    return ea, ed & 0xFFFF, eb


def who(ea):
    if ea == P2:
        return 'P1→P2(玩家打)'
    if ea == P1:
        return 'P2→P1(电脑打)'
    return '?(%08X)' % ea


def slot_role(slot, ea):
    """判定 0x628200 修正链读的对象槽是攻击方还是受击方"""
    if slot == ea:
        return '受击方'
    if ea == P2 and slot == P1 or ea == P1 and slot == P2:
        return '攻击方'
    return '其他(%08X)' % slot


def hp(h):
    a = struct.unpack('<h', rd(h, P1 + HP_OFF, 2))[0]
    b = struct.unpack('<h', rd(h, P2 + HP_OFF, 2))[0]
    return a, b


def fmt(k, d, ea, slot, t=None):
    return '  #%-4d %s伤害=%-4d (0x%03X)  受击=%08X %-14s 对象槽=%s' % (
        k + 1,
        ('t=%5.1fs  ' % t) if t is not None else ' ',
        d, d, ea, who(ea), slot_role(slot, ea))


def dump(h, st, start=0, end=None):
    ctr = counter(h, st)
    if end is None:
        end = ctr
    start = max(start, ctr - RING)
    if start >= end:
        print('缓冲区暂无记录（计数 %d）' % ctr)
        return
    hp1, hp2 = hp(h)
    print('计数 %d，缓冲区最近 %d 条（P1 HP=%d  P2 HP=%d）：'
          % (ctr, end - start, hp1, hp2))
    for k in range(start, end):
        ea, d, slot = entry(h, st, k)
        print(fmt(k, d, ea, slot))


def summarize(recs):
    from collections import defaultdict
    g = defaultdict(list)
    role = defaultdict(int)
    for ea, d, slot in recs:
        g[who(ea)].append(d)
        role[slot_role(slot, ea)] += 1
    lines = ['  ---------- 汇总 ----------']
    for k, v in g.items():
        v = sorted(v)
        lines.append('  %-14s 次数=%d  最小=%d  最大=%d  平均=%.1f'
                     % (k, len(v), v[0], v[-1], sum(v) / len(v)))
        lines.append('      序列=%s' % v)
    lines.append('  0x628200 修正链读的对象槽 = %s'
                 % ' / '.join('%s×%d' % (k, v) for k, v in role.items()))
    lines.append('  --------------------------')
    return lines


def watch(h, secs):
    st = install(h)
    last = counter(h, st)
    print('起始计数 %d，开始监视……（每次命中打印一行，同时写入 hookdmg_log.txt）' % last)
    print('P1 对象=0x%08X  P2 对象=0x%08X' % (P1, P2))
    t0 = time.time()
    seen = last
    recs = []
    with open(LOG, 'a', encoding='utf-8') as fp:
        fp.write('\n=== %s  watch start (ctr=%d) ===\n' % (time.strftime('%H:%M:%S'), last))
        while time.time() - t0 < secs:
            ctr = counter(h, st)
            if ctr != seen:
                for k in range(seen, ctr):
                    ea, d, slot = entry(h, st, k)
                    recs.append((ea, d, slot))
                    line = fmt(k, d, ea, slot, time.time() - t0)
                    print(line)
                    fp.write(line + '\n')
                fp.flush()
                seen = ctr
            time.sleep(0.06)
        for line in summarize(recs):
            print(line)
            fp.write(line + '\n')
    print('结束，本次新增 %d 次命中' % len(recs))


def set_comp(h, on, factor=None):
    """开关伤害补偿；factor 为浮点倍率（2.0 = 翻倍）"""
    st = find_cave(h)
    if not st or isinstance(st, str):
        raise SystemExit('钩子未按当前版本安装，先执行 install')
    flag = st['cave'] + OFF_FLAG
    if factor is not None:
        kofp.write_mem(h, flag + 4, struct.pack('<I', int(round(factor * 256))))
    kofp.write_mem(h, flag, struct.pack('<I', 1 if on else 0))
    show_comp(h, st)


def comp_state(h, st=None):
    """返回 (是否开启, 倍率)；钩子未安装时返回 (False, None)"""
    if st is None:
        st = find_cave(h)
    if not st or isinstance(st, str):
        return False, None
    on, fac = struct.unpack('<II', rd(h, st['cave'] + OFF_FLAG, 8))
    return bool(on), fac / 256.0


def show_comp(h, st=None):
    if st is None:
        st = find_cave(h)
        if not st or isinstance(st, str):
            raise SystemExit('钩子未按当前版本安装，先执行 install')
    on, fac = comp_state(h, st)
    print('伤害补偿：%s   倍率 = %.2f （仅对"受击方=P2"即玩家打出的伤害生效）'
          % ('开' if on else '关', fac))


def status(h):
    st = find_cave(h)
    hp1, hp2 = hp(h)
    print('P1 对象 0x%08X  HP=%d' % (P1, hp1))
    print('P2 对象 0x%08X  HP=%d' % (P2, hp2))
    if not st:
        print('钩子：未安装    落地点字节：%s' % rd(h, SITE, SITE_LEN).hex(' '))
        return
    if st == 'dead':
        print('钩子：失效（jmp 指向已释放内存）  执行 install 会自动还原并重装')
        return
    if st == 'old':
        print('钩子：旧版本  执行 install 会自动升级')
        return
    print('钩子：已安装    cave=0x%08X  计数=%d  落地点字节：%s'
          % (st['cave'], counter(h, st), rd(h, SITE, SITE_LEN).hex(' ')))


def pick_pid():
    pids = kofp.find_pids(cfg.PROCESS_NAME)
    if not pids:
        raise SystemExit('未找到 %s，请先启动游戏。\n'
                         '（进程名不符时，改 config.json 的 process_name）'
                         % cfg.PROCESS_NAME)
    if len(pids) == 1:
        return pids[0]
    for pid in pids:
        if kofp.window_of(pid):
            print('检测到 %d 个游戏进程，选用有窗口的 PID %d' % (len(pids), pid))
            return pid
    print('检测到 %d 个游戏进程，均无可见窗口，选用 PID %d' % (len(pids), pids[0]))
    return pids[0]


def print_config():
    print('配置来源：%s' % cfg.CONFIG_SOURCE)
    for k in ('process_name', 'hook_site', 'hook_orig', 'hook_back',
              'player_obj', 'enemy_obj', 'hp_offset',
              'power_offset', 'power_flag_offset',
              'default_factor', 'log_file'):
        v = cfg.P.get(k)
        if isinstance(v, int) and k != 'default_factor':
            v = '0x%08X' % v
        print('  %-18s %s' % (k, v))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'config':                 # 不需要游戏进程
        print_config()
        return
    pid = pick_pid()
    h = kofp.open_proc(pid)
    print('--- PID %d ---' % pid)
    if cmd == 'install':
        install(h)
    elif cmd == 'uninstall':
        uninstall(h)
    elif cmd == 'watch':
        watch(h, float(sys.argv[2]) if len(sys.argv) > 2 else 60)
    elif cmd == 'dump':
        st = find_cave(h)
        if not st or isinstance(st, str):
            raise SystemExit('钩子未按当前版本安装，先执行 install')
        dump(h, st)
    elif cmd in ('status', 'stat'):
        status(h)
    elif cmd == 'on':
        f = float(sys.argv[2]) if len(sys.argv) > 2 else cfg.DEFAULT_FACTOR
        set_comp(h, True, f)
    elif cmd == 'off':
        set_comp(h, False)
    elif cmd == 'show':
        show_comp(h)
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
