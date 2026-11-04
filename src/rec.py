"""rec.py —— 等游戏启动 → 自动装伤害钩子 → 自动记录到 hookdmg_log.txt

用法：
    rec.cmd            等 15 分钟内的游戏启动，一直录到游戏退出
    rec.cmd 300        最多录 300 秒

顺序无所谓：先跑本脚本再开游戏，或先开游戏再跑都行（脚本会等也会认领）。
"""
import os
import sys
import time

import kofp
import hookdmg as H

WAIT_LIMIT = 900        # 等游戏启动的最长时间（秒）


def wait_game(limit=WAIT_LIMIT):
    t0 = time.time()
    shown = False
    while time.time() - t0 < limit:
        pids = kofp.find_pids()
        if pids:
            return pids[0]
        if not shown:
            print('等待 KingOfFighters2002UM.exe 启动……（最多等 %d 秒）' % limit)
            shown = True
        time.sleep(0.5)
    return None


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 3600.0
    pid = wait_game()
    if pid is None:
        raise SystemExit('没等到游戏启动，退出。')

    print('找到游戏 PID %d，等它解密完并进入可写状态……' % pid)
    h = kofp.open_proc(pid)

    # 等 .text 解密完成（0x620902 变成原始指令）
    ok = False
    for _ in range(240):
        try:
            if H.rd(h, H.SITE, H.SITE_LEN) == H.ORIG:
                ok = True
                break
        except SystemExit:
            pass
        time.sleep(0.5)
    if not ok:
        print('[!] 等不到解密完成，仍尝试安装。')

    st = H.install(h)
    print('')
    print('=' * 60)
    print('  钩子就绪，可以开始打了。')
    print('  P1 与 P2 选同一个角色（镜像），互相各打几下最直观。')
    print('  打的过程中不要关游戏；打完回来说一声即可。')
    print('=' * 60)
    print('')

    log = H.LOG
    t0 = time.time()
    seen = H.counter(h, st)
    recs = []
    dead = 0
    with open(log, 'a', encoding='utf-8') as fp:
        fp.write('\n=== %s  rec start (ctr=%d) ===\n' % (time.strftime('%H:%M:%S'), seen))
        while time.time() - t0 < secs:
            if not kofp.find_pids():
                print('游戏已退出，收工。')
                break
            try:
                ctr = H.counter(h, st)
            except SystemExit:
                dead += 1
                if dead > 20:
                    print('读不到计数器，可能钩子被覆盖，重装一次。')
                    try:
                        st = H.install(h)
                        seen = H.counter(h, st)
                    except SystemExit:
                        pass
                    dead = 0
                time.sleep(0.5)
                continue
            if ctr != seen:
                for k in range(seen, ctr):
                    try:
                        ea, d, slot = H.entry(h, st, k)
                    except SystemExit:
                        break
                    recs.append((ea, d, slot))
                    line = H.fmt(k, d, ea, slot, time.time() - t0)
                    print(line)
                    fp.write(line + '\n')
                fp.flush()
                seen = ctr
            time.sleep(0.05)
        for line in H.summarize(recs):
            print(line)
            fp.write(line + '\n')
    print('结束，共记录 %d 次命中，日志：%s' % (len(recs), log))


if __name__ == '__main__':
    main()
