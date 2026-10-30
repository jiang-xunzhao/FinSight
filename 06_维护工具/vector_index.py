"""本地向量索引维护工具。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIRS = (
    PROJECT_ROOT / "01_基础配置",
    PROJECT_ROOT / "02_向量存储",
    PROJECT_ROOT / "03_数据层",
    PROJECT_ROOT / "04_智能分析",
    PROJECT_ROOT / "05_Web服务",
)
for candidate in (PROJECT_ROOT, *MODULE_DIRS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import data


def main() -> None:
    """构建或查看本地向量索引。"""
    parser = argparse.ArgumentParser(description="FinSight 本地向量索引")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="强制重建全部向量",
    )
    parser.add_argument(
        "--sync-qdrant",
        action="store_true",
        help="将现有向量同步到 Qdrant",
    )
    parser.add_argument(
        "--reset-qdrant",
        action="store_true",
        help="同步前重建 Qdrant 集合",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="批处理大小，默认 64",
    )
    args = parser.parse_args()
    if args.sync_qdrant or args.reset_qdrant:
        result = data.sync_qdrant_vector_index(
            force_reset=args.reset_qdrant,
        )
    elif args.rebuild:
        result = data.build_rag_vector_index(
            force=True,
            batch_size=args.batch_size,
        )
    else:
        result = data.load_vector_index_status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
