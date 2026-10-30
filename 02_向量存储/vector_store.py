"""Qdrant 向量库适配层。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
import atexit
from pathlib import Path
from typing import Any, Iterable

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from config import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL,
    QDRANT_COLLECTION,
    QDRANT_PATH,
    QDRANT_URL,
)

_CLIENT: QdrantClient | None = None
_CLIENT_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False

os.environ.setdefault("QDRANT_DISABLE_TELEMETRY", "true")


def vector_backend() -> str:
    """返回当前配置的向量后端。"""
    configured = os.getenv("VECTOR_BACKEND", "qdrant").strip().lower()
    return "memory" if configured == "memory" else "qdrant"


def get_client() -> QdrantClient:
    """创建并复用 Qdrant 本地或服务端客户端。"""
    global _CLIENT, _ATEXIT_REGISTERED
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            if QDRANT_URL:
                _CLIENT = QdrantClient(
                    url=QDRANT_URL,
                    timeout=30,
                    check_compatibility=False,
                )
            else:
                QDRANT_PATH.mkdir(parents=True, exist_ok=True)
                _CLIENT = QdrantClient(
                    path=str(QDRANT_PATH),
                    check_compatibility=False,
                )
            if not _ATEXIT_REGISTERED:
                atexit.register(_CLIENT.close)
                _ATEXIT_REGISTERED = True
    return _CLIENT


def ensure_collection() -> None:
    """确保向量集合存在且维度正确。"""
    client = get_client()
    if not client.collection_exists(QDRANT_COLLECTION):
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(
                size=LOCAL_EMBEDDING_DIM,
                distance=Distance.COSINE,
            ),
        )
        # 载荷索引对本地模式和远端都有效：按公司筛选时没有索引就是全量扫载荷。
        for field_name in (
            "company_ticker",
            "deal_id",
            "file_path",
            "report_period",
        ):
            try:
                client.create_payload_index(
                    collection_name=QDRANT_COLLECTION,
                    field_name=field_name,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception:
                pass
        return
    info = client.get_collection(QDRANT_COLLECTION)
    vectors = info.config.params.vectors
    size = getattr(vectors, "size", None)
    if size is not None and int(size) != LOCAL_EMBEDDING_DIM:
        raise ValueError(
            f"Qdrant 集合维度为 {size}，期望 {LOCAL_EMBEDDING_DIM}"
        )


def build_chunk_key(chunk: dict[str, Any]) -> str:
    """生成跨存储稳定文本块标识。"""
    raw = "|".join(
        (
            str(chunk.get("file_path") or ""),
            str(chunk.get("page_no") or 0),
            str(chunk.get("chunk_index") or 0),
        )
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_point_id(chunk: dict[str, Any]) -> str:
    """为 Qdrant point 生成稳定 UUID。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, build_chunk_key(chunk)))


def build_chunk_payload(chunk: dict[str, Any]) -> dict[str, Any]:
    """构造写入 Qdrant 的业务 payload。"""
    return {
        "build_chunk_key": build_chunk_key(chunk),
        "company_ticker": str(chunk.get("stock_code") or ""),
        "deal_id": str(chunk.get("deal_id") or ""),
        "company_name": str(chunk.get("company_name") or ""),
        "file_name": str(chunk.get("file_name") or ""),
        "file_path": str(chunk.get("file_path") or ""),
        "page_no": int(chunk.get("page_no") or 0),
        "chunk_index": int(chunk.get("chunk_index") or 0),
        "report_period": str(chunk.get("report_period") or ""),
        "content": str(chunk.get("content") or ""),
        "embedding_model": str(
            chunk.get("embedding_model") or LOCAL_EMBEDDING_MODEL
        ),
    }


def upsert_chunks(
    chunks: Iterable[dict[str, Any]],
    batch_size: int = 256,
) -> int:
    """批量写入或更新向量。"""
    if vector_backend() == "memory":
        return 0
    usable = []
    for chunk in chunks:
        try:
            vector = json.loads(str(chunk.get("embedding_json") or "[]"))
        except Exception:
            continue
        if not isinstance(vector, list) or len(vector) != LOCAL_EMBEDDING_DIM:
            continue
        usable.append(
            PointStruct(
                id=build_point_id(chunk),
                vector=vector,
                payload=build_chunk_payload(chunk),
            )
        )
    if not usable:
        return 0
    client = get_client()
    with _CLIENT_LOCK:
        ensure_collection()
        for start in range(0, len(usable), max(1, batch_size)):
            client.upsert(
                collection_name=QDRANT_COLLECTION,
                points=usable[start : start + batch_size],
                wait=True,
            )
    return len(usable)


def delete_file_chunks(file_path: str) -> None:
    """删除指定文件的全部向量。"""
    if vector_backend() == "memory":
        return
    client = get_client()
    with _CLIENT_LOCK:
        ensure_collection()
        client.delete(
            collection_name=QDRANT_COLLECTION,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="file_path",
                        match=MatchValue(value=str(file_path)),
                    )
                ]
            ),
            wait=True,
        )


def _query(
    query_vector: list[float],
    company_ticker: str,
    deal_id: str,
    limit: int,
) -> list[Any]:
    """执行一次 Qdrant 过滤查询。"""
    must = [
        FieldCondition(
            key="company_ticker",
            match=MatchValue(value=company_ticker),
        )
    ]
    if deal_id:
        must.append(
            FieldCondition(
                key="deal_id",
                match=MatchValue(value=deal_id),
            )
        )
    client = get_client()
    with _CLIENT_LOCK:
        ensure_collection()
        response = client.query_points(
            collection_name=QDRANT_COLLECTION,
            query=query_vector,
            query_filter=Filter(must=must),
            limit=max(1, int(limit)),
            with_payload=True,
            with_vectors=False,
        )
    return list(response.points)


def search_vectors(
    query_vector: list[float],
    company_ticker: str,
    deal_id: str = "",
    limit: int = 80,
) -> list[dict[str, Any]]:
    """按公司和交易过滤执行向量召回。"""
    ticker = str(company_ticker or "").strip()
    if not ticker or vector_backend() == "memory":
        return []
    results = _query(query_vector, ticker, deal_id, limit)
    if not results and deal_id:
        results = _query(query_vector, ticker, "", limit)
    return [
        {
            "score": float(point.score),
            "payload": dict(point.payload or {}),
        }
        for point in results
    ]


def count_vectors() -> int:
    """返回 Qdrant 集合中的向量数量；连不上就直接抛，由调用方决定怎么报。

    原来出错也返回 0：嵌入式 Qdrant 的文件目录被另一个进程锁住时，
    状态接口照样报 ready:true / count:0，看起来像「向量库空了」，
    实际是根本没读成功 —— 会误导人去重建索引。
    """
    if vector_backend() == "memory":
        return 0
    client = get_client()
    with _CLIENT_LOCK:
        ensure_collection()
        return int(
            client.count(
                collection_name=QDRANT_COLLECTION,
                exact=True,
            ).count
        )


def reset_collection() -> None:
    """删除并重建向量集合。"""
    if vector_backend() == "memory":
        return
    client = get_client()
    with _CLIENT_LOCK:
        if client.collection_exists(QDRANT_COLLECTION):
            client.delete_collection(QDRANT_COLLECTION)
        # 本地(嵌入式)模式陷阱：delete_collection 并不把 storage.sqlite 从磁盘删掉，
        # 紧接着 ensure_collection 重建同名集合时旧段被重新接回，点数一点没少
        # （实测 qdrant-client 1.19：24888 → 删 → 重建 → 仍 24888）。结果是界面上的
        # 「重建索引」永远清不掉孤儿向量点，RAG 会检索到已从库里删除的旧 chunk。
        # 服务端模式(delete_collection 走真正的 HTTP 删除)不需要这一步。
        if not QDRANT_URL:
            for sub in ("collection", "collections"):
                stale_dir = Path(QDRANT_PATH) / sub / QDRANT_COLLECTION
                if stale_dir.exists():
                    shutil.rmtree(stale_dir, ignore_errors=True)
        ensure_collection()


def get_status() -> dict[str, Any]:
    """返回 Qdrant 健康状态。"""
    base = {
        "enabled": vector_backend() == "qdrant",
        "backend": vector_backend(),
        "mode": "server" if QDRANT_URL else "local",
        "url": QDRANT_URL,
        "path": str(QDRANT_PATH),
        "collection": QDRANT_COLLECTION,
        "dimension": LOCAL_EMBEDDING_DIM,
    }
    if vector_backend() == "memory":
        base.update({"ready": False, "count": 0, "error": ""})
        return base
    try:
        base.update({"ready": True, "count": count_vectors(), "error": ""})
    except Exception as exc:
        base.update({"ready": False, "count": 0, "error": str(exc)})
    return base
