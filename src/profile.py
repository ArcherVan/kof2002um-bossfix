"""profile.py —— 运行配置加载器

把"和具体游戏版本强绑定"的常量集中到一处，便于适配不同发行版/不同补丁版本。

优先级（从高到低）：
    1. 环境变量 KOF_CONFIG 指定的 JSON 文件
    2. 仓库根目录的 config.json
    3. 内置默认值（对应 Steam 版 4,748,304 字节 / MD5 e7a78eb4...）

也就是说：不开 config.json 也能直接跑；换版本时只改 config.json，不动代码。
"""
import json
import os
import sys


def _force_utf8_console():
    """把标准输出/错误切成 UTF-8。

    英文版 Windows 的默认代码页是 cp1252，直接 print 中文会抛
    UnicodeEncodeError 并中断程序；CI 里输出被重定向到管道时同理。
    控制台输出走 WriteConsoleW，不受代码页影响；管道/重定向则按 UTF-8 写出。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


_force_utf8_console()

# 内置默认值（与 Steam 版 KingOfFighters2002UM.exe 对应）
DEFAULTS = {
    # 进程名：大小写不敏感，按 tasklist 输出匹配
    'process_name': 'KingOfFighters2002UM.exe',
    # 伤害落地指令所在的运行时地址（钩子挂在这里）
    'hook_site': 0x620902,
    # 该处的原始指令字节（校验用；不一致会拒绝改动）
    'hook_orig': '66 29 90 38 01 00 00',
    # 原指令结束后的下一条地址（钩子执行完跳回这里）
    'hook_back': 0x620909,
    # 引擎"当前角色对象"全局槽（仅用于记录，可留默认）
    'obj_slot': 0x21E3414,
    # 1P 角色对象固定地址
    'player_obj': 0x167D360,
    # 2P 角色对象固定地址（补偿只在"受击方=此地址"时生效）
    'enemy_obj': 0x167D580,
    # 角色对象内 HP 字段偏移
    'hp_offset': 0x138,
    # 首次安装后的默认倍率（1.0 = 不放大）
    'default_factor': 2.0,
    # 运行日志文件名（相对仓库根目录）
    'log_file': 'hookdmg_log.txt',
}

CONFIG_NAME = 'config.json'
ENV_KEY = 'KOF_CONFIG'


def _repo_root():
    """src/ 的上一级即仓库根目录"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _coerce(key, value):
    """把 JSON 里的字符串/数字统一成内部需要的类型"""
    if key in ('hook_site', 'hook_back', 'obj_slot', 'player_obj',
               'enemy_obj', 'hp_offset'):
        if isinstance(value, str):
            return int(value, 0)
        return int(value)
    if key == 'default_factor':
        return float(value)
    if key == 'hook_orig' and isinstance(value, str):
        return ' '.join(value.replace(',', ' ').split())
    return value


def _load_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as fp:
            raw = json.load(fp)
    except (OSError, ValueError) as e:
        print('[warn] 读取配置失败（%s）：%r，改用默认值' % (path, e),
              file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        print('[warn] 配置格式应为 JSON 对象：%s' % path, file=sys.stderr)
        return {}
    return raw


def load():
    """返回合并后的配置字典，并记录配置来源"""
    cfg = dict(DEFAULTS)
    cfg['_source'] = 'built-in defaults'

    path = os.environ.get(ENV_KEY)
    if path and os.path.isfile(path):
        cfg.update({k: _coerce(k, v) for k, v in _load_file(path).items()})
        cfg['_source'] = path
        return cfg

    default_path = os.path.join(_repo_root(), CONFIG_NAME)
    if os.path.isfile(default_path):
        cfg.update({k: _coerce(k, v)
                    for k, v in _load_file(default_path).items()})
        cfg['_source'] = default_path
    return cfg


P = load()

# ---- 便于直接 import 的短名字 ----
PROCESS_NAME = P['process_name']
HOOK_SITE = P['hook_site']
HOOK_ORIG = bytes.fromhex(P['hook_orig'].replace(' ', ''))
HOOK_BACK = P['hook_back']
OBJ_SLOT = P['obj_slot']
PLAYER_OBJ = P['player_obj']
ENEMY_OBJ = P['enemy_obj']
HP_OFFSET = P['hp_offset']
DEFAULT_FACTOR = P['default_factor']
LOG_PATH = os.path.join(_repo_root(), P['log_file'])
CONFIG_SOURCE = P['_source']

if __name__ == '__main__':
    print('配置来源：%s' % CONFIG_SOURCE)
    for k, v in DEFAULTS.items():
        print('  %-14s %s' % (k, P.get(k)))
