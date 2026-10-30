r"""导入删除护栏回归：被文档行引用的原文，任何情况下都不许被当成临时副本删掉。

实测事故（本件的由来）：一次 `POST /api/import/start {}` 把
`C:\Users\35999\PycharmProjects\FinSight\\storage\\uploads\\web`
里 **25 份已入库文档的原文删掉了**（`7_document_index` 行还在、切片和向量都还在，
但 `/api/documents/open` 从此打不开、也没法重导）。链路是两条叠加：

1. `start_bulk_import_task(local_dir="")`：`Path("").expanduser()` 落到**服务进程自己的
   当前目录**（= 项目根），`is_dir()` 通过 ⇒ `rglob("*.pdf")` 把托管上传目录整个扫进
   待导入清单。
2. 导入 worker 的 `process_one(path)`：命中重复就 `if is_managed_copy(path) and path.exists():
   path.unlink()`，而 `is_managed_copy` **只看路径不看库里有没有人在引用它** ⇒ 已在库里的
   原文正好都躺在托管目录里 ⇒ 全删。异常分支同样删。

修法：删除前必须查库（`path_referenced_by_document`）；入参必须是绝对路径、且不能圈进
托管上传根。本件断言这两条，并且**只做只读检查**，不真的删任何东西。
"""
import ast
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
for part in ("01_基础配置", "02_向量存储", "03_数据层"):
    sys.path.insert(0, str(PROJECT / part))

import data  # noqa: E402

SOURCE = (PROJECT / "03_数据层" / "data.py").read_text(encoding="utf-8")

failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {label}{('：' + extra) if extra else ''}")


def call(**kwargs):
    """跑 `start_bulk_import_task`，返回 `(是否抛 ValueError, 消息)`。

    只喂**该被拒**的入参：这些分支都在建任务之前 `raise`，所以不会起后台导入、
    不碰盘上文件、不写库。
    """
    try:
        data.start_bulk_import_task(**kwargs)
    except ValueError as exc:
        return True, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return False, "没抛异常（不该放行）"


# ① 空目录必须拒（以前它 = 服务自己的工程目录）
raised, message = call(local_dir="")
check("空 local_dir 被拒（曾把工程目录当导入目录）", raised and "空目录" in message, message)

# ② 相对路径必须拒（相对到服务进程的 cwd，行为不可预期）
raised, message = call(local_dir="storage")
check("相对 local_dir 被拒", raised and "绝对路径" in message, message)

# ③ 圈进托管上传根必须拒（工程目录、storage、上传根本身三种写法都要拦住）
managed_root = (data.BASE_DIR / "storage" / "uploads").resolve()
for bad in (str(data.BASE_DIR), str(data.BASE_DIR / "storage"), str(managed_root)):
    raised, message = call(local_dir=bad)
    check(f"包含托管上传目录被拒：{bad}", raised and "托管上传" in message, message)

# ④ 正斜杠写法也要拦住（HTTP JSON 里传的都是 `C:/a/b` 这种）
raised, message = call(local_dir=str(data.BASE_DIR).replace("\\", "/"))
check("正斜杠写法的工程目录同样被拒", raised and "托管上传" in message, message)

# ⑤ 删除前必须查库：源码级断言（两处 `path.unlink()` 都要带引用校验）
# 注：全文件 `.unlink()` 有 7 处（含文档删除等别的路径），这里**只钉导入 worker 那两处**——
# 出事的就是它们；别处的删文件走 `managed_copy` 列（库里自己声明是托管副本才删），
# 语义上不会误删别人家的原文。
tree = ast.parse(SOURCE)
guarded = SOURCE.count("and not path_referenced_by_document(path)")
bare_in_process_one = 0
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "process_one":
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "unlink"
            ):
                bare_in_process_one += 1
check("导入 worker 里确实有 2 处删文件", bare_in_process_one == 2, f"实测 {bare_in_process_one} 处")
check("两处删文件都带了引用校验", guarded >= bare_in_process_one, f"校验出现 {guarded} 次 / 删除 {bare_in_process_one} 处")

# ⑥ `path_referenced_by_document` 的行为：库里有引用 ⇒ True，随便一个路径 ⇒ False
rows = data.query_all("SELECT file_path AS p FROM 7_document_index LIMIT 1")
if rows:
    real = Path(str(rows[0]["p"]))
    check("库里登记的原文 ⇒ 认定被引用", data.path_referenced_by_document(real) is True, str(real))
else:
    check("库里能取到一条文档行", False, "7_document_index 空？")
check(
    "没人引用的路径 ⇒ 认定可删",
    data.path_referenced_by_document(Path(managed_root) / "0000000000000000000" / "没有这个文件.pdf") is False,
)

# ⑦ 事故现场自检：库里的每一条文档行，原文都必须还在盘上
missing = [
    str(row["p"])
    for row in data.query_all("SELECT file_path AS p FROM 7_document_index")
    if not Path(str(row["p"])).exists()
]
check("全库文档原文都在盘上（丢失即本件要拦的事）", not missing, f"缺失 {len(missing)} 条 " + "; ".join(missing[:3]))

print("\n结论：" + ("全部通过" if not failed else f"{failed} 项失败"))
sys.exit(1 if failed else 0)
