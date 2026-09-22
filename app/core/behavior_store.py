"""Vector behaviour storage over ChromaDB (Adım 3).

Seeds each customer's historical "normal" spending profile into a persistent
local ChromaDB collection as an embedded document, then exposes semantic
querying so the analysis agent can compare an incoming transfer against past
behaviour (RAG). Embeddings use ChromaDB's default local ONNX MiniLM model —
no API key required. All ChromaDB failures degrade gracefully to ``0.0``
distance so the rule engine keeps working offline.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app import config

logger = logging.getLogger("fraud.vector")

try:
    import chromadb
    from chromadb.api.models.Collection import Collection
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
except Exception:  # pragma: no cover — chromadb optional at runtime
    chromadb = None  # type: ignore[assignment]
    Collection = None  # type: ignore[assignment, misc]
    DefaultEmbeddingFunction = None  # type: ignore[assignment, misc]

COLLECTION_NAME = "customer_behaviors"


def _behaviour_document(record: dict[str, Any]) -> str:
    """Build a natural-language description of a customer's normal behaviour."""
    return (
        f"Müşteri {record.get('customer_id')} ({record.get('name', '')}) "
        f"genellikle {record.get('avg_amount')} ortalama tutarında işlem yapar. "
        f"Kullandığı cihazlar: {', '.join(record.get('known_device_ids', []))}. "
        f"Alışılmış lokasyonlar: {', '.join(record.get('known_locations', []))}. "
        f"Tipik işlem saatleri: {', '.join(map(str, record.get('typical_hours', [])))}."
    )


class BehaviorStore:
    """Persistent semantic store of per-customer behaviour embeddings."""

    def __init__(
        self,
        customers_path: Path | None = None,
        vector_dir: Path | None = None,
    ) -> None:
        self.customers_path = customers_path or config.DATA_DIR / "customers.json"
        self.vector_dir = vector_dir or config.VECTOR_DIR
        self._collection: Any = None
        self._disabled = False
        self._init_collection()

    # --- lifecycle ---------------------------------------------------------
    def _init_collection(self) -> None:
        if chromadb is None:
            self._disabled = True
            logger.warning("[Vector] chromadb import edilemedi — semantik analiz kapalı")
            return
        try:
            self.vector_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(self.vector_dir),
                settings=chromadb.config.Settings(anonymized_telemetry=False),
            )
            self._collection = client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:  # pragma: no cover
            self._disabled = True
            logger.exception("[Vector] ChromaDB başlatılamadı: %s", exc)

    @staticmethod
    def _load_customers(path: Path) -> list[dict[str, Any]]:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else [data]

    def seed(self, force: bool = False) -> int:
        """Embed every customer's behaviour doc into the collection (idempotent).

        Returns the number of documents written. Re-seeding is skipped unless
        ``force=True`` or the collection is empty.
        """
        if self._disabled:
            return 0
        try:
            existing: set[str] = set(self._collection.get(limit=10000)["ids"])
            documents, ids, metadatas = [], [], []
            for record in self._load_customers(self.customers_path):
                cid = str(record.get("customer_id", ""))
                if not cid or (not force and cid in existing):
                    continue
                documents.append(_behaviour_document(record))
                ids.append(cid)
                metadatas.append({"customer_id": cid})
            if documents:
                self._collection.upsert(
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas,
                )
                logger.info("[Vector] %d müşteri davranışı gömüldü", len(documents))
            return len(documents)
        except Exception as exc:  # pragma: no cover
            self._disabled = True
            logger.exception("[Vector] seeding hatası: %s", exc)
            return 0

    def semantic_distance(self, transaction: dict[str, Any]) -> float | None:
        """Cosine distance (0..~2) of the transaction vs. its customer's history.

        Returns ``None`` when the vector store is unavailable or the customer
        has no embedded profile.
        """
        if self._disabled:
            return None
        customer_id = str(transaction.get("customer_id", ""))
        try:
            if not customer_id or self._collection.count() == 0:
                return None
            text = (
                f"İşlem {transaction.get('transaction_id')}: müşteri {customer_id} "
                f"{transaction.get('amount')} {transaction.get('currency')} tutarında "
                f"{transaction.get('location')} lokasyonundan, cihaz "
                f"{transaction.get('device_id')} üzerinden, {transaction.get('ts')}."
            )
            results = self._collection.query(
                query_texts=[text],
                n_results=1,
                where={"customer_id": customer_id},
            )
            distances = results.get("distances")
            if not distances or not distances[0]:
                return None
            return float(distances[0][0])
        except Exception as exc:  # pragma: no cover
            logger.debug("[Vector] sorgu hatası (safely): %s", exc)
            return None

    @property
    def count(self) -> int:
        if self._disabled:
            return 0
        try:
            return int(self._collection.count())
        except Exception:  # pragma: no cover
            return 0
