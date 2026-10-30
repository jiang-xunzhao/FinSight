"""项目内回归集的**唯一运行入口**：串行跑 `常驻件/` 全部回归件 + 可选四层深测。

用法（venv python）：
    python run_all.py                # 只跑常驻件（27 项）
    python run_all.py --layers       # 常驻件跑完再跑四层深测（会重写报告，带时间戳）
    python run_all.py --quick        # 跳过两个最慢的件（test_ingest_gate 建一次性库、test_end_to_end 走模型）
    python run_all.py --only alias gate   # 只跑名字含这些关键字的件

约定：
- **串行**跑。建/DROP 一次性库的件并发会串台产生假 FAIL（本项目踩过）。
- 需要 Web 服务的件（HTTP/端到端/前端体检）在**服务没起时记为失败并写明原因**，不自作主张跳过。
- 冒烟导入进行中**不要**跑读本库的件（audit_live_state / test_identity_hygiene / live_import_duplicate_smoke
  / test_end_to_end / test_english_labels）：表在半路必然是残缺的，会假 FAIL。
退出码：0 = 全过；1 = 有失败。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITE_DIR = HERE / "常驻件"
PY = sys.executable
SERVER_URL = "http://127.0.0.1:8501/api/summary"

# (文件名, 需要服务, 属于"重"件可被 --quick 跳过)
SUITES: list[tuple[str, bool, bool]] = [
    ("test_report_period_label.py", False, False),
    ("test_company_name_cleaning.py", False, False),
    ("test_company_aliases.py", False, False),
    ("test_percent_and_scope_guards.py", False, False),
    ("test_running_scope.py", False, False),
    ("test_module_refs.py", False, False),
    ("test_balance_unit_recovery.py", False, False),
    ("test_english_labels.py", False, False),
    ("test_f1_stock_code.py", False, False),
    ("test_data_period_and_cost.py", False, False),
    ("test_table_display.py", False, False),
    ("test_validation_provenance.py", False, False),
    ("test_comparison_mixed_period.py", False, False),
    ("test_analysis_mode_alias.py", False, False),
    ("test_offline_sql_repair_guard.py", False, False),
    ("test_comparison_fixture.py", False, False),
    ("test_display_identity_and_eps.py", False, False),
    ("test_company_short_name.py", False, False),
    ("test_sql_alias_display.py", False, False),
    ("test_report_period_and_merge.py", False, False),
    ("test_period_caliber.py", False, False),
    ("test_except_logging.py", False, False),
    ("test_delete_rebuild.py", False, False),
    ("test_alias_cache_race.py", False, False),
    ("test_import_delete_safety.py", False, False),
    ("test_import_jobs_pruning.py", False, False),
    ("test_richer_row_wins.py", False, False),
    ("test_nonperiodic_no_overwrite.py", False, False),
    ("test_equity_statement_scope.py", False, False),
    ("test_cash_forex_and_truncated_label.py", False, False),
    ("test_validation_rows_lifecycle.py", False, False),
    ("test_sql_and_keys.py", False, False),
    ("test_session_task_race.py", False, False),
    ("audit_live_state.py", False, False),
    ("test_identity_hygiene.py", False, False),
    ("test_vector_status.py", True, False),
    ("test_ingest_gate.py", False, True),
    ("test_import_control.py", False, False),
    ("check_web_frontend.py", True, False),
    ("test_http_guards.py", True, False),
    ("test_login_guard.py", True, False),
    ("live_import_duplicate_smoke.py", True, False),
    ("test_history_followup.py", True, False),
    ("test_chart_contract.py", False, False),
    ("test_coverage_and_exports.py", False, False),
    ("test_end_to_end.py", True, True),
]


def server_up() -> bool:
    try:
        # 登录闸门开启时，本机回环带此标记放行（详见 web.py auth_ok 注释）
        request = urllib.request.Request(SERVER_URL, headers={"X-FinSight-Test": "1"})
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layers", action="store_true", help="常驻件之后再跑四层深测")
    parser.add_argument("--quick", action="store_true", help="跳过标记为重的件")
    parser.add_argument("--only", nargs="*", default=None, help="只跑名字含这些关键字的件")
    args = parser.parse_args()

    alive = server_up()
    print(f"服务状态：{'UP ' + SERVER_URL if alive else 'DOWN（需要服务的件将记为失败）'}")

    # 登记表必须跟目录一致：漏登记 = 这个件永远不被跑，而矩阵还显示「全绿」（实测漏掉
    # test_cash_forex_and_truncated_label.py / test_equity_statement_scope.py 两件）。
    registered = {name for name, _, _ in SUITES}
    orphans = sorted(
        path.name for path in SUITE_DIR.glob("*.py")
        if path.name not in registered and not path.name.startswith("_")
    )
    if orphans:
        print(f"❌ 常驻件目录里有 {len(orphans)} 个件没登记进 SUITES（永远不会被跑）：{', '.join(orphans)}")
        return 1

    picked = [
        item for item in SUITES
        if (not args.quick or not item[2])
        and (not args.only or any(word in item[0] for word in args.only))
    ]
    failures: list[str] = []
    started = time.time()
    for name, needs_server, _heavy in picked:
        path = SUITE_DIR / name
        if not path.exists():
            failures.append(f"{name}（文件不存在）")
            print(f"FAIL {name} -> 文件不存在")
            continue
        if needs_server and not alive:
            failures.append(f"{name}（服务未启动）")
            print(f"FAIL {name} -> 服务未启动，无法跑")
            continue
        # 必须给子进程 -X utf8：Windows 上 Python 的 stdio 编码跟随系统代码页（本机 GBK），
        # 而回归件普遍打印 ✅/↔️/⛔ 这类字符 —— 不带这个参数，件会在 print 处抛
        # UnicodeEncodeError 崩掉，被这里记成 exit=1 的「假 FAIL」（实测一次性假红 5 项：
        # test_richer_row_wins / test_nonperiodic_no_overwrite / audit_live_state /
        # test_ingest_gate / 以及 --layers 分支的 run_layers）。
        result = subprocess.run(
            [PY, "-X", "utf8", str(path)], cwd=str(SUITE_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            print(f"PASS {name}")
        else:
            failures.append(name)
            print(f"FAIL {name} -> exit={result.returncode}")
            tail = [line for line in (result.stdout or "").splitlines() if "FAIL" in line][:6]
            for line in tail:
                print(f"      {line}")
            err = (result.stderr or "").strip().splitlines()
            if err:
                print(f"      stderr: {err[-1][:200]}")

    if args.layers:
        result = subprocess.run([PY, "-X", "utf8", str(HERE / "run_layers.py")], cwd=str(HERE), encoding="utf-8", errors="replace")
        if result.returncode != 0:
            failures.append("run_layers.py（有 P0/P1 缺陷）")

    print(f"\n===== {len(picked)} 项，失败 {len(failures)} 项，耗时 {(time.time() - started) / 60:.1f} 分钟 =====")
    for name in failures:
        print(f"  ✗ {name}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
