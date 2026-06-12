"""算术沙箱：用 Python ast 做【数值白名单求值】，替代给强模型 agent 裸 Bash。

在多人 + 客户数据的服务器上给 agent 裸 Bash = RCE + 数据外泄面。recompute() 只允许
数字字面量 + 四则/取整/幂/取模 + round/min/max/abs/sum/ceil/floor，禁止名称引用、
属性访问、任意函数调用、推导式、导入——覆盖绝大多数「个数/数量/金额/取整/分配/累计/
阈值」复算，且零代码执行风险。强模型抽检的 arithmetic 维度据此独立复算。
"""

from __future__ import annotations

import ast
import math
import re
from typing import Any

_ALLOWED_FUNCS = {
    "round": round, "min": min, "max": max, "abs": abs, "sum": sum,
    "ceil": math.ceil, "floor": math.floor, "int": int, "float": float,
}
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARY = (ast.UAdd, ast.USub)


class RecomputeError(ValueError):
    pass


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise RecomputeError(f"不允许的字面量：{node.value!r}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        return _apply_bin(node.op, _eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARY):
        v = _eval(node.operand)
        return +v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval(e) for e in node.elts]
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise RecomputeError("只允许 round/min/max/abs/sum/ceil/floor/int/float")
        if node.keywords:
            raise RecomputeError("不允许关键字参数")
        return _ALLOWED_FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise RecomputeError(f"不允许的表达式节点：{type(node).__name__}")


def _apply_bin(op: ast.AST, a: Any, b: Any) -> Any:
    if isinstance(op, ast.Add):
        return a + b
    if isinstance(op, ast.Sub):
        return a - b
    if isinstance(op, ast.Mult):
        return a * b
    if isinstance(op, ast.Div):
        return a / b
    if isinstance(op, ast.FloorDiv):
        return a // b
    if isinstance(op, ast.Mod):
        return a % b
    if isinstance(op, ast.Pow):
        if abs(b) > 64:  # 防 10**100000 之类的资源炸弹
            raise RecomputeError("幂指数过大")
        return a ** b
    raise RecomputeError("不允许的运算符")


def recompute(expression: str) -> Any:
    """安全求值一个纯数值算术表达式。非法/越权一律抛 RecomputeError。"""
    expr = (expression or "").strip()
    if not expr or len(expr) > 500:
        raise RecomputeError("表达式为空或过长")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise RecomputeError(f"语法错误：{e}")
    return _eval(tree)


_EXPR_RE = re.compile(r"([0-9][0-9\.\s\+\-\*/%\(\)]{2,80}?)\s*[=＝]\s*([0-9]+(?:\.[0-9]+)?)")


def verify_claims(text: str) -> list[dict]:
    """从证据文本里抽取「算式 = 值」断言，用 recompute 独立复核（计入业务取整规则）。

    返回 [{expr, claimed, computed, ok, exact}]。ok=True 表示与裸算或 floor/ceil/round
    任一一致（避免把"17/5=3 向下取整""2/5=1 少于1按1"等正确业务规则误判为错）；
    只有连取整都对不上才 ok=False。供 arithmetic 维度做确定性二次核验（advisory，非权威判定）。
    """
    out: list[dict] = []
    for m in _EXPR_RE.finditer(text or ""):
        expr, claimed = m.group(1).strip(), m.group(2).strip()
        if not re.search(r"[\+\-\*/%]", expr):  # 必须真含运算符，跳过 "1 = 1" 之类
            continue
        try:
            computed = recompute(expr)
            c, cl = float(computed), float(claimed)
            exact = abs(c - cl) < 1e-6
            nd = len(claimed.split(".")[1]) if "." in claimed else 0
            # 计入取整不变量：整数 floor/ceil/round，或按 claimed 小数位四舍五入，任一一致即视为相符
            consistent = (exact
                          or any(abs(fn(c) - cl) < 1e-6 for fn in (math.floor, math.ceil, round))
                          or abs(round(c, nd) - cl) < 0.5 * 10 ** (-nd))
            out.append({"expr": expr, "claimed": claimed, "computed": computed,
                        "ok": consistent, "exact": exact})
        except (RecomputeError, ValueError):
            continue
    return out
