"""离线自检（不需要游戏进程，可在 CI 中运行）

覆盖：
  * 配置加载与默认值
  * 钩子机器码的拼装结构（长度、开头、尾部跳回、原指令位置）
  * 所有源码不含硬编码的绝对路径
"""
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import profile as cfg          # noqa: E402
import hookdmg as H            # noqa: E402


class TestConfig(unittest.TestCase):

    def test_defaults(self):
        self.assertEqual(cfg.P['process_name'], 'KingOfFighters2002UM.exe')
        self.assertEqual(cfg.DEFAULT_FACTOR, 2.0)
        self.assertEqual(len(cfg.HOOK_ORIG), 7)

    def test_addresses_are_ints(self):
        for k in ('hook_site', 'hook_back', 'player_obj', 'enemy_obj'):
            self.assertIsInstance(cfg.P[k], int, k)

    def test_log_path_inside_repo(self):
        self.assertTrue(os.path.abspath(cfg.LOG_PATH).startswith(ROOT))


class TestCave(unittest.TestCase):

    def setUp(self):
        self.cave = 0x10000000
        self.code = H.build_cave(self.cave)

    def test_header(self):
        # pushfd ; push ecx ; push edx
        self.assertEqual(self.code[:3], b'\x9c\x51\x52')

    def test_contains_original_instruction(self):
        i = self.code.find(H.ORIG)
        self.assertNotEqual(i, -1, '机器码里应包含被替换的原指令')

    def test_jump_back_target(self):
        tail = self.code[-5:]
        self.assertEqual(tail[0], 0xE9)
        rel = struct.unpack('<i', tail[1:])[0]
        dest = self.cave + len(self.code) - 5 + 5 + rel
        self.assertEqual(dest, H.BACK)

    def test_site_and_orig_consistent(self):
        self.assertEqual(len(H.ORIG), H.SITE_LEN)
        self.assertEqual(H.BACK, H.SITE + H.SITE_LEN)


class TestConsoleEncoding(unittest.TestCase):
    """英文版 Windows（cp1252）下 print 中文不应崩溃。"""

    def test_stdout_forced_to_utf8(self):
        import io
        import importlib

        buf = io.BytesIO()
        fake = io.TextIOWrapper(buf, encoding='cp1252', errors='strict')
        old = sys.stdout
        sys.stdout = fake
        try:
            importlib.reload(cfg)          # 导入时会把 stdout 重配成 UTF-8
            self.assertEqual(fake.encoding.lower().replace('-', ''), 'utf8')
            print('中文输出测试')           # 修复前这里会抛 UnicodeEncodeError
            fake.flush()
        finally:
            sys.stdout = old
            importlib.reload(cfg)

        self.assertIn('中文输出测试'.encode('utf-8'), buf.getvalue())


class TestPowerPatch(unittest.TestCase):
    """气槽免扣补丁的机器码拼装（不接触进程）"""

    def setUp(self):
        import power as P
        self.P = P

    def test_dec_opcode(self):
        self.assertEqual(self.P.DEC.hex(), 'fe88e3010000')
        self.assertEqual(len(self.P.DEC), 6)

    def test_spend_sites(self):
        sites = [s for s, _ in self.P.SPEND_SITES]
        self.assertEqual(sites, [0x430D60, 0x430DA3, 0x60AC51])

    def test_cave_layout(self):
        p1 = 0x167D360
        b = self.P.cave_bytes(p1)
        self.assertEqual(len(b), 14)
        # cmp eax, imm32
        self.assertEqual(b[0], 0x3D)
        self.assertEqual(struct.unpack('<I', b[1:5])[0], p1)
        # je rel8：下一条在偏移 7，目标应为偏移 13 的 ret
        self.assertEqual(b[5], 0x74)
        target = 7 + struct.unpack('b', b[6:7])[0]
        self.assertEqual(target, 13)
        # dec 紧跟在 je 之后
        self.assertEqual(b[7:13], self.P.DEC)
        # 末尾 ret
        self.assertEqual(b[13], 0xC3)

    def test_hook_points_into_cave(self):
        for site, _ in self.P.SPEND_SITES:
            cave = 0x10000000
            b = self.P.hook_bytes(site, cave)
            self.assertEqual(len(b), 6)
            self.assertEqual(b[0], 0xE8)
            self.assertEqual(b[5], 0x90)
            # call 的下一条是 site+5，相对偏移应指向 cave
            self.assertEqual(site + 5 + struct.unpack('<i', b[1:5])[0], cave)

    def test_cave_signature(self):
        p1 = 0x167D360
        self.assertTrue(self.P._looks_like_cave(self.P.cave_bytes(p1)))
        self.assertFalse(self.P._looks_like_cave(self.P.DEC + b'\x00' * 10))


class TestNoHardcodedPaths(unittest.TestCase):

    def test_sources_have_no_abs_paths(self):
        bad = []
        for base, _, files in os.walk(os.path.join(ROOT, 'src')):
            for name in files:
                if not name.endswith('.py'):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding='utf-8') as fp:
                    text = fp.read()
                for token in ('C:\\Users', 'D:\\2002UM'):
                    if token in text:
                        bad.append('%s 含 %s' % (name, token))
        self.assertEqual(bad, [], '\n'.join(bad))


if __name__ == '__main__':
    unittest.main(verbosity=2)
