r"""常驻回归件：静态检查项目代码里 `data.xxx` / `vector_store.xxx` 的引用是否真的存在。

为什么在回归集里：真实缺陷是 06_维护工具/回归测试/layers.py 曾经调 `data.vector_store_status()`
（这个名字**根本不存在**），外层还写了 `hasattr(...)` 兜底 ⇒ 恒 None ⇒ 恒 0 ⇒ 「向量点数漂移」
这条检查长期**静默跳过**，却在覆盖矩阵里显示 ✅ 无缺陷（假通过）。产品代码里同类引用会直接
AttributeError，且只有点到那条路径才暴露，所以做成静态守卫。

只导入模块判属性存在性：**不连 MySQL、不开 Qdrant**，可以在任何时间跑。

两类必须避开的坑（都踩过）：
- 局部变量遮蔽：`data = []` 之后的 `data.append(...)` 是本地列表，不是模块（assistant.py 图表构造处）。
- 带 BOM 的 .py 用 utf-8 读会 `invalid non-printable character U+FEFF` ⇒ 一律 `utf-8-sig`。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]  # 0=常驻件 1=回归测试 2=06_维护工具 3=项目根
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))
sys.path.insert(0, str(ROOT))

import data  # noqa: E402
import vector_store  # noqa: E402

MODULES = {"data": data, "vector_store": vector_store}
SCAN_DIRS = [ROOT / sub for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务", "06_维护工具", "99_启动入口")]


def shadowed_lines(tree: ast.Module) -> set[tuple[int, str]]:
    """函数内被赋过值的名字，在这些行上按局部变量看待，不算模块引用。"""
    blocked: set[tuple[int, str]] = set()
    for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        assigned: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)
            elif isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.target, ast.Name):
                assigned.add(node.target.id)
        for node in ast.walk(func):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in assigned:
                blocked.add((node.lineno, node.value.id))
    return blocked


problems: list[str] = []
checked = 0
for directory in SCAN_DIRS:
    for path in sorted(directory.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError as error:
            problems.append(f"{path.relative_to(ROOT)}: 语法错误 {error}")
            continue
        blocked = shadowed_lines(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            if not isinstance(base, ast.Name) or base.id not in MODULES:
                continue
            if (node.lineno, base.id) in blocked:
                continue
            if node.attr.startswith("__") or node.attr == "config":
                continue
            checked += 1
            if not hasattr(MODULES[base.id], node.attr):
                problems.append(f"{path.relative_to(ROOT)}:{node.lineno}  {base.id}.{node.attr} 不存在")

print(f"引用检查数={checked}")
for line in problems:
    print("  ✗ " + line)
print("PASS 所有 data./vector_store. 引用都存在" if not problems else f"FAIL 有 {len(problems)} 处失效引用")
sys.exit(0 if not problems else 1)
