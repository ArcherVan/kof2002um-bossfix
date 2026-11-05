"""shell.py —— 补气窗口里的极简命令交互

目的：双击 启动KOF2002UM.cmd 之后，用户应该能在**这同一个窗口**里改倍率，
不必再开第二个命令行。

实现要点：
  - 后台线程阻塞读 stdin，主线程用 poll() 非阻塞取行，不耽误补气定时。
  - 输入被重定向到文件/管道时线程读到 EOF 自然退出，不会刷屏。
  - 提示符 "kof> " 打印后一直挂在行首；任何输出前先擦掉它，输出完再重画，
    避免出现 "kof> [ok] ..." 这种粘在一起的日志。
"""
import queue
import re
import sys
import threading

MIN_FACTOR = 0.1
MAX_FACTOR = 16.0

HELP = [
    '  2.5            设为 2.5 倍并开启（也可写 dmg 2.5 / x2.5）',
    '  on [数字]      开启伤害补偿；不带数字沿用当前倍率',
    '  off            关闭伤害补偿（钩子保留，随时可再开）',
    '  power          立即把 1P 气槽补满',
    '  status         查看补丁、倍率、气槽',
    '  help           显示这份说明',
    '  quit           停止补气并退出（补丁仍然有效，直接关窗口也一样）',
]

# 允许 2.5 / x2.5 / 2.5x / 2.5倍 这几种写法
_NUM = re.compile(r'^[x×*]?\s*(\d+(?:\.\d+)?)\s*[倍]?[x×*]?$')


def clamp(f):
    """倍率限幅：太小没意义，太大（>16）一击必杀且血量计算会溢出观感"""
    return max(MIN_FACTOR, min(MAX_FACTOR, float(f)))


def _num(tok):
    m = _NUM.match((tok or '').strip())
    return float(m.group(1)) if m else None


def parse(line):
    """把一行输入解析成 (动作, 参数)

    动作：help / status / power / on / off / quit
    参数：仅 on 用，倍率或 None（None = 沿用当前值）
    """
    s = (line or '').strip().lower()
    if not s:
        return 'help', None
    parts = s.split()
    head, arg = parts[0], (parts[1] if len(parts) > 1 else '')

    if head in ('quit', 'q', 'exit', '/q', '/quit'):
        return 'quit', None
    if head in ('help', 'h', '?', '/?', '/help'):
        return 'help', None
    if head in ('status', 'st', 's', 'info'):
        return 'status', None
    if head in ('power', 'refill', 'p', 'gas', '气'):
        return 'power', None
    if head in ('off', '/off'):
        return 'off', None
    if head in ('on', 'dmg', 'set', 'mul', '倍率', 'x', '*'):
        return 'on', (_num(arg) if arg else None)
    v = _num(s)
    if v is not None:
        return 'on', v
    return 'help', None


class Reader(threading.Thread):
    """后台读 stdin，主线程 poll() 非阻塞取整行"""

    def __init__(self):
        super().__init__(daemon=True)
        self.lines = queue.Queue()
        self.closed = False

    def run(self):
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                self.closed = True
                return
            if not line:                # EOF：输入被重定向或已关闭
                self.closed = True
                return
            self.lines.put(line)

    def poll(self):
        try:
            return self.lines.get_nowait()
        except queue.Empty:
            return None


class Plain:
    """非交互环境的占位实现，行为和普通 print 一致"""

    def show(self):
        pass

    def clear(self):
        pass

    def say(self, *lines):
        for t in lines:
            print(t)


class Prompt(Plain):
    """带 "kof> " 提示符的输出器"""

    TEXT = 'kof> '
    _BLANK = '\r' + ' ' * 24 + '\r'

    def show(self):
        sys.stdout.write(self.TEXT)
        sys.stdout.flush()

    def clear(self):
        sys.stdout.write(self._BLANK)
        sys.stdout.flush()

    def say(self, *lines):
        self.clear()
        for t in lines:
            sys.stdout.write(str(t) + '\n')
        sys.stdout.flush()
        self.show()
