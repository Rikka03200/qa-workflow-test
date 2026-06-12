"""安全加载 scripts/ 下的模块供 in-process 调用。

两类：
- 普通模块（下划线/无连字符）：`select_sprint` / `jira_fetch` / `batch_generate` /
  `qa_pipeline` / `cheap_model` / `_load_env` / `run_sprint` —— 可直接 import_module。
- 连字符文件名：`validate-test-design.py` / `validate-questions.py` /
  `normalize-questions.py` —— 不能普通 import，用 importlib 按路径加载。

【关键副作用防御（实测，记忆里漏记的坑）】：
  这些脚本在 import 期（win32 分支）会**重赋值** `sys.stdout/stderr`：
  - validate-test-design / validate-questions / check-ticket-artifacts：
      `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`  ← 新建 wrapper 并替换全局
  - select_sprint / jira_fetch / cheap_model / qa_pipeline / run_sprint：
      `sys.stdout.reconfigure(encoding="utf-8")`              ← 原地改编码（无害）
  若放任不管：①替换式会改掉 uvicorn 的 stdout；②被替换下来的孤立 wrapper 被 GC 时
  其 __del__ 会 close 底层 buffer（与真 stdout 共享）→ 关闭真正的 stdout。
  对策：import 时若 stdout/stderr 被替换成了新对象，先 detach（切断它对共享 buffer 的
  所有权，使 GC 不再 close buffer）再还原成原对象。reconfigure（原地）不触发还原，安全。
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from .. import config

if str(config.SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(config.SCRIPTS_DIR))

_HYPHEN_CACHE: dict[str, ModuleType] = {}


@contextlib.contextmanager
def _preserve_std():
    """在导入可能篡改 sys.stdout/stderr 的脚本时，保护主进程的标准流。"""
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        yield
    finally:
        for saved, name in ((saved_out, "stdout"), (saved_err, "stderr")):
            cur = getattr(sys, name)
            if cur is not saved:
                # 被替换成了新 wrapper：detach 切断其对共享 buffer 的所有权，避免 GC 关闭真流
                with contextlib.suppress(Exception):
                    cur.detach()
                setattr(sys, name, saved)


def load_normal(name: str) -> ModuleType:
    """import 一个普通命名的 scripts 模块（结果由 importlib 缓存于 sys.modules）。"""
    with _preserve_std():
        return importlib.import_module(name)


def load_hyphenated(filename: str) -> ModuleType:
    """按文件路径加载连字符命名的脚本（如 validate-test-design.py）。"""
    if filename in _HYPHEN_CACHE:
        return _HYPHEN_CACHE[filename]
    path = config.SCRIPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"找不到脚本：{path}")
    modname = "qa_scripts_" + filename.replace("-", "_").replace(".py", "")
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 构造导入 spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod  # 让模块内 dataclass / 相对引用解析正确
    with _preserve_std():
        spec.loader.exec_module(mod)
    _HYPHEN_CACHE[filename] = mod
    return mod


# ---- 便捷访问器（按需懒加载） ----

def validate_test_design():
    return load_hyphenated("validate-test-design.py")


def validate_questions():
    return load_hyphenated("validate-questions.py")


def normalize_questions():
    return load_hyphenated("normalize-questions.py")


def select_sprint():
    return load_normal("select_sprint")


def jira_fetch():
    return load_normal("jira_fetch")


def batch_generate():
    return load_normal("batch_generate")


def load_env():
    return load_normal("_load_env")


def run_sprint():
    return load_normal("run_sprint")
