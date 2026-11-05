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


class TestLaunch(unittest.TestCase):
    """一键启动：参数解析、Steam 库解析、就绪判定（不启动进程）"""

    def setUp(self):
        import power as P
        import launch as L
        self.P, self.L = P, L

    def test_args_default(self):
        o = self.L.parse_args(['launch.py'])
        self.assertIsNone(o['exe'])
        self.assertFalse(o['no_power'])
        self.assertEqual(o['dmg'], None)
        self.assertEqual(o['timeout'], self.L.LAUNCH_WAIT)

    def test_args_strip_launch_word(self):
        o = self.L.parse_args(['launch.py', 'launch', '--dmg', '2.5'])
        self.assertEqual(o['dmg'], 2.5)

    def test_args_dmg_without_value_uses_default(self):
        o = self.L.parse_args(['launch.py', '--dmg', '--no-keep'])
        self.assertEqual(o['dmg'], cfg.DEFAULT_FACTOR)
        self.assertTrue(o['no_keep'])

    def test_args_exe_and_timeout(self):
        o = self.L.parse_args(['launch.py', '--exe', r'D:\Games\KOF\k.exe',
                               '--timeout', '30'])
        self.assertEqual(o['exe'], r'D:\Games\KOF\k.exe')
        self.assertEqual(o['timeout'], 30.0)

    def test_assess_cold_on_ciphertext(self):
        site = b'\x37\x9a\xff\x01\x02\x03\x04'
        spends = [b'\x11\x22\x33\x44\x55\x66'] * 3
        self.assertEqual(self.L.assess(site, spends), 'cold')

    def test_assess_ready(self):
        site = cfg.HOOK_ORIG
        spends = [self.P.DEC] * 3
        self.assertEqual(self.L.assess(site, spends), 'ready')

    def test_assess_patched(self):
        site = b'\xe9' + b'\x00' * 6
        spends = [b'\xe8' + b'\x00' * 5] * 3
        self.assertEqual(self.L.assess(site, spends), 'patched')

    def test_assess_mixed_is_cold(self):
        # 一半解密完一半没有 → 还不能动
        site = cfg.HOOK_ORIG
        spends = [self.P.DEC, self.P.DEC, b'\x00' * 6]
        self.assertEqual(self.L.assess(site, spends), 'cold')

    def test_parse_vdf_new_format(self):
        text = ('"libraryfolders"\n{\n "0"\n {\n  "path"  "C:\\\\Steam"\n'
                '  "apps"\n  {\n   "2280"  "123"\n  }\n }\n'
                ' "1"\n {\n  "path"  "D:\\\\SteamLibrary"\n }\n}\n')
        got = self.L.parse_library_vdf(text)
        self.assertIn('C:\\Steam', got)
        self.assertIn('D:\\SteamLibrary', got)

    def test_parse_vdf_old_format(self):
        text = '"libraryfolders"\n{\n "TimeNextStatsReport"  "1"\n "1"  "E:\\\\Lib"\n}\n'
        got = self.L.parse_library_vdf(text)
        self.assertIn('E:\\Lib', got)
        self.assertNotIn('1', got)          # 不要把统计字段当成路径

    def test_scan_finds_nested_exe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            deep = os.path.join(tmp, 'common', 'KOF 2002 UM')
            os.makedirs(deep)
            exe = os.path.join(deep, 'KingOfFighters2002UM.exe')
            open(exe, 'wb').close()
            self.assertEqual(self.L.scan_exe(os.path.join(tmp, 'common')),
                             exe)
            self.assertIsNone(self.L.scan_exe(tmp, 'nope.exe'))

    def test_explicit_exe_must_exist(self):
        with self.assertRaises(SystemExit):
            self.L.find_exe(os.path.join(ROOT, 'no-such-game.exe'))


class TestGameLauncher(unittest.TestCase):
    """游戏目录启动文件的生成（不写盘、不启动游戏）"""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import make_launcher as M
        self.M = M

    def test_placeholder_replaced(self):
        text = self.M.render(r'D:\Some Repo')
        self.assertIn('set "KOF_HOME=D:\\Some Repo"', text)
        self.assertNotIn('{{HOME}}', text)

    def test_crlf_only(self):
        text = self.M.render('X')
        self.assertNotIn('\n', text.replace('\r\n', ''))

    def test_gbk_encodable(self):
        self.M.render(r'D:\路径\仓库').encode('gbk')

    def test_proxy_cleared(self):
        text = self.M.render('X')
        for var in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy'):
            self.assertIn('set "%s="' % var, text)
        self.assertIn('set "NO_PROXY=*"', text)

    def test_has_patch_and_fallback(self):
        text = self.M.render('X')
        self.assertIn('src\\launch.py', text)     # 有仓库就打补丁
        self.assertIn(':plain', text)             # 没有就直接开游戏
        self.assertIn('start "" "%EXE%"', text)


class TestShell(unittest.TestCase):
    """窗口内命令解析（src/shell.py）"""

    def setUp(self):
        import shell
        self.sh = shell

    def test_bare_number(self):
        self.assertEqual(self.sh.parse('2.5'), ('on', 2.5))
        self.assertEqual(self.sh.parse('3'), ('on', 3.0))
        self.assertEqual(self.sh.parse('  1.75  '), ('on', 1.75))

    def test_number_variants(self):
        self.assertEqual(self.sh.parse('x3'), ('on', 3.0))
        self.assertEqual(self.sh.parse('2.5x'), ('on', 2.5))
        self.assertEqual(self.sh.parse('dmg 1.5'), ('on', 1.5))
        self.assertEqual(self.sh.parse('10倍'), ('on', 10.0))

    def test_commands(self):
        self.assertEqual(self.sh.parse('on'), ('on', None))
        self.assertEqual(self.sh.parse('ON 4'), ('on', 4.0))
        self.assertEqual(self.sh.parse('off'), ('off', None))
        self.assertEqual(self.sh.parse('power'), ('power', None))
        self.assertEqual(self.sh.parse('status'), ('status', None))
        self.assertEqual(self.sh.parse('quit'), ('quit', None))
        self.assertEqual(self.sh.parse('Q'), ('quit', None))

    def test_unknown_falls_back_to_help(self):
        self.assertEqual(self.sh.parse(''), ('help', None))
        self.assertEqual(self.sh.parse('???'), ('help', None))
        self.assertEqual(self.sh.parse('abc'), ('help', None))

    def test_clamp(self):
        self.assertEqual(self.sh.clamp(0.001), self.sh.MIN_FACTOR)
        self.assertEqual(self.sh.clamp(999), self.sh.MAX_FACTOR)
        self.assertEqual(self.sh.clamp(2.5), 2.5)

    def test_help_is_gbk_safe(self):
        # 启动文件与 kof.cmd 都是 GBK，帮助文本不能有编码外的字符
        for line in self.sh.HELP:
            line.encode('gbk')

    def test_prompt_say_keeps_prompt_last(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            p = self.sh.Prompt()
            p.say('hello')
            p.say('a', 'b')
        text = buf.getvalue()
        self.assertTrue(text.endswith('kof> '))
        self.assertIn('hello', text)
        self.assertIn('a\nb\n', text)

    def test_plain_matches_print(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.sh.Plain().say('x', 'y')
        self.assertEqual(buf.getvalue(), 'x\ny\n')


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
