"""max2.py —— 解除 MAX2 超必杀的"体力 30% 以下"限制

用法：
    max2.py show       查看当前状态（原版 / 已解除）
    max2.py patch      解除体力限制：气槽够 3 格就能发，不再看血量
    max2.py unpatch    还原游戏原本的判定

原理：
    MAX2（3 格气的超必杀）在原版里有两道门槛：
        1. 气槽 >= 3 格
        2. 体力 <= 36（满血 120 的 30%）

    判定代码在 0x641CD0，末尾是：

        00641CE4  cmp  byte ptr [ecx+0x1E3], 3     ; 气槽 >= 3 ?
        00641CEB  jae  0x641CF2
        00641CF2  movzx eax, word ptr [ecx+0x138]  ; 读 HP
        00641CF9  sub  eax, 0x24                   ; HP - 36
        00641D01  test eax, eax
        00641D03  jle  0x641D0A                    ; <= 0 才放行
        00641D05  jmp  0x63F470                    ; 否则按"招式不可用"处理

    把 0x641D03 的 `jle`（7E 05）改成 `jmp`（EB 05），第二道门槛就不再起作用，
    气槽够 3 格随时能发。只改 2 个字节，指令长度不变，不申请代码洞。

注意：
    这段判定是 1P/CPU 共用的（取 [0x21E3414] 当前角色对象），所以 cpu 只要
    气槽够也会解除血量限制。实战中 cpu 很少主动选 MAX2，通常察觉不到。
"""
import sys

import kofp
import profile as cfg

SITE = cfg.MAX2_SITE                 # 判定指令地址
ORIG = b'\x7e\x05'                   # jle 0x641D0A
PATCH = b'\xeb\x05'                  # jmp 0x641D0A
HP_LIMIT = 0x24                      # 36（满血 120 的 30%）
POWER_NEED = 3                       # MAX2 需要的气槽格数


def _open():
    pid = kofp.find_pid()
    if not pid:
        raise SystemExit('未找到游戏进程，请先启动游戏。')
    return kofp.open_proc(pid)


def state(h, site=None):
    """返回 'orig' / 'patched' / 原始字节（不认识）"""
    cur = kofp.read_mem(h, site or SITE, len(ORIG))
    if cur == ORIG:
        return 'orig'
    if cur == PATCH:
        return 'patched'
    return cur


def status(h, site=None):
    st = state(h, site)
    addr = site or SITE
    if st == 'orig':
        print('MAX2 体力限制：原版（体力 <= %d / 120，即 30%% 以下才能发）' % HP_LIMIT)
        print('  0x%08X  %s  jle（条件跳转）' % (addr, ORIG.hex(' ')))
    elif st == 'patched':
        print('MAX2 体力限制：已解除（气槽够 %d 格即可发）' % POWER_NEED)
        print('  0x%08X  %s  jmp（无条件放行）' % (addr, PATCH.hex(' ')))
    else:
        print('MAX2 判定点字节不认识：%s' % st.hex(' '))
        print('  期望 %s 或 %s —— 版本可能不同，未做任何改动'
              % (ORIG.hex(' '), PATCH.hex(' ')))


def install(h, site=None):
    addr = site or SITE
    cur = kofp.read_mem(h, addr, len(ORIG))
    if cur == PATCH:
        print('MAX2 补丁已存在，无需重装')
        status(h, addr)
        return False
    if cur != ORIG:
        raise SystemExit(
            '0x%08X 处字节与预期不符：%s\n期望 %s（jle 0x641D0A）\n'
            '游戏版本可能不同，未做任何改动。' % (addr, cur.hex(' '), ORIG.hex(' ')))
    kofp.write_mem(h, addr, PATCH)
    back = kofp.read_mem(h, addr, len(PATCH))
    if back != PATCH:
        raise SystemExit('写入校验失败：%s' % back.hex(' '))
    print('MAX2 体力限制已解除：气槽够 %d 格随时可发（不需要残血）' % POWER_NEED)
    return True


def restore(h, site=None):
    addr = site or SITE
    cur = kofp.read_mem(h, addr, len(ORIG))
    if cur == ORIG:
        print('MAX2 补丁未安装，无需还原')
        return False
    if cur != PATCH:
        raise SystemExit('0x%08X 处字节不认识：%s，未做改动' % (addr, cur.hex(' ')))
    kofp.write_mem(h, addr, ORIG)
    print('MAX2 判定已还原为原版（体力 30% 以下才能发）')
    return True


def main():
    argv = sys.argv
    cmd = argv[1] if len(argv) > 1 else 'show'
    h = _open()
    if cmd == 'show':
        status(h)
    elif cmd in ('patch', 'install', 'on'):
        install(h)
    elif cmd in ('unpatch', 'restore', 'off'):
        restore(h)
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
