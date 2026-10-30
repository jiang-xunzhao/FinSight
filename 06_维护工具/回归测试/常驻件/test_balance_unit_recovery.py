import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("01_基础配置", "02_向量存储", "03_数据层", "04_智能分析", "05_Web服务"):
    sys.path.insert(0, str(ROOT / sub))
import data


def value(code: str, name: str, amount: Decimal) -> data.StatementValue:
    return data.StatementValue(metric_code=code, metric_name=name, statement_type="balance",
                               current=amount, previous=None, current_raw="", previous_raw=None,
                               page_no=1, row_index=1, confidence=Decimal("0.9"))


# 万元表要**自洽**：资产 = (负债+权益) × 10000，否则倍率本身就对不上任何单位
wan_liab, wan_equity = Decimal("4869520524.97"), Decimal("11610674719.67")
cases = (
    ("TCL千元表(实测)", Decimal("365144968892"), Decimal("237525394"), Decimal("127619575"), 1000),
    ("老板电器元表(实测)", Decimal("16551968719.72"), Decimal("4869520524.97"), Decimal("11610674719.67"), 0),
    ("五粮液元表(实测)", Decimal("184999362605.88"), Decimal("64054972918.06"), Decimal("120944389687.82"), 0),
    ("万元表(自洽构造)", (wan_liab + wan_equity) * 10000, wan_liab, wan_equity, 10000),
    ("不平的表(不该动)", Decimal("100000000"), Decimal("90000000"), Decimal("5000000"), 0),
    ("资产太小(不该动)", Decimal("5000"), Decimal("3"), Decimal("2"), 0),
)
ok = True
for label, assets, liab, equity, expected in cases:
    values = {
        "total_assets": value("total_assets", "资产总计", assets),
        "total_liabilities": value("total_liabilities", "负债合计", liab),
        "total_equity": value("total_equity", "股东权益合计", equity),
    }
    scale = data.recover_balance_sheet_scale(values)
    check = values["total_liabilities"].current + values["total_equity"].current
    # 断言只校两件事：判出的倍数 == 期望；倍数 != 0 时恒等式必须被补平（==0 时必须原样不动）
    good = scale == expected and values["total_liabilities"].current == liab * (scale or 1) and (abs(check - assets) / assets < Decimal("0.01") if scale else check == liab + equity)
    ok = ok and bool(good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}: scale={scale}(期望{expected}) 勾稽={check} vs 资产={assets}")
print("ALL_OK" if ok else "SOME_FAILED")
sys.exit(0 if ok else 1)

