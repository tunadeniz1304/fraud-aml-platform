"""Vector behaviour store over ChromaDB — retrieval for the LLM (real RAG).

Fixes bug #14:

* the query sentence no longer embeds ``transaction_id`` / ``ts`` noise — only
  behavioural attributes (amount band, channel, location, device, hour band);
* the retrieved document is **returned as context** (:meth:`retrieve`) and fed
  to the LLM narrative/copilot prompt instead of being discarded;
* Chroma is kept off the synchronous scoring path (latency budget).

Embeddings use Chroma's local ONNX MiniLM model; if it cannot be loaded (e.g.
offline container) a deterministic hashing embedding is used instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import config

logger = logging.getLogger("fraud.vector")

try:
    import chromadb
except Exception:  # noqa: BLE001  # pragma: no cover — chromadb opsiyonel
    chromadb = None  # type: ignore[assignment]

COLLECTION_NAME = "customer_behaviors"
_WORD_RE = re.compile(r"\w+", re.UNICODE)


class HashEmbedding:
    """Deterministic 256-d signed feature hashing (no model download)."""

    DIM = 256

    def __init__(self) -> None:  # chroma inspects the signature
        pass

    @staticmethod
    def name() -> str:
        return "anil3-hash-256"

    def get_config(self) -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> HashEmbedding:
        return HashEmbedding()

    def is_legacy(self) -> bool:
        return False

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self(input)

    def __call__(self, input: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in input:
            vec = [0.0] * self.DIM
            for token in _WORD_RE.findall(text.casefold()):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "little") % self.DIM
                vec[idx] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


def _amount_band(amount: float) -> str:
    for limit, label in ((500, "çok küçük"), (2_500, "küçük"), (10_000, "orta"), (50_000, "büyük")):
        if amount < limit:
            return label
    return "çok büyük"


def _hour_band(hour: int) -> str:
    if hour < 6:
        return "gece"
    if hour < 12:
        return "sabah"
    if hour < 18:
        return "öğleden sonra"
    return "akşam"


def behaviour_document(record: dict[str, Any]) -> str:
    """Natural-language description of a customer's normal behaviour."""
    hours = sorted(int(h) for h in record.get("typical_hours", []))
    bands = sorted({_hour_band(h) for h in hours})
    return (
        f"Müşteri genellikle {_amount_band(float(record.get('avg_amount', 0)))} tutarlı "
        f"(ortalama {record.get('avg_amount')} TRY) işlem yapar. "
        f"Cihazlar: {', '.join(record.get('known_device_ids', []))}. "
        f"Lokasyonlar: {', '.join(record.get('known_locations', []))}. "
        f"Saat bandı: {', '.join(bands)}."
    )


def transaction_document(tx: dict[str, Any]) -> str:
    """Behavioural sentence for a transfer — deliberately without id/timestamp."""
    try:
        from datetime import datetime

        hour = datetime.fromisoformat(str(tx.get("ts"))).hour
        band = _hour_band(hour)
    except (TypeError, ValueError):
        band = "bilinmeyen saat"
    amount = float(tx.get("amount_try") or tx.get("amount") or 0)
    return (
        f"Müşteri {_amount_band(amount)} tutarlı ({amount:.0f} TRY) işlem yapar. "
        f"Cihaz: {tx.get('device_id', '')}. Lokasyon: {tx.get('location', '')}. "
        f"Kanal: {tx.get('channel', '')}. Saat bandı: {band}."
    )


@dataclass(frozen=True)
class Retrieved:
    document: str
    distance: float


class BehaviorStore:
    """Persistent semantic store of per-customer behaviour embeddings."""

    def __init__(
        self,
        customers_path: Path | None = None,
        vector_dir: Path | None = None,
        *,
        embedding: str | None = None,
    ) -> None:
        settings = config.get_settings()
        self.customers_path = customers_path or settings.resolved_customers_path
        self.vector_dir = vector_dir or settings.resolved_vector_dir
        self.embedding = embedding or settings.vector_embedding
        self._collection: Any = None
        self._disabled = False
        self._init_collection()

    # --- lifecycle ---------------------------------------------------------
    def _embedding_function(self) -> Any:
        if self.embedding == "hash":
            return HashEmbedding()
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

            ef = DefaultEmbeddingFunction()
            ef(["ısınma"])  # force model load now, not on the first query
            return ef
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - ağ/model önbelleği
            logger.warning("[Vector] ONNX gömme yüklenemedi (%s) — hash gömmeye düşüldü", exc)
            self.embedding = "hash"
            return HashEmbedding()

    def _init_collection(self) -> None:
        if chromadb is None:  # pragma: no cover
            self._disabled = True
            logger.warning("[Vector] chromadb import edilemedi — RAG kapalı")
            return
        try:
            self.vector_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(self.vector_dir),
                settings=chromadb.config.Settings(anonymized_telemetry=False),
            )
            name = f"{COLLECTION_NAME}_{self.embedding}"
            self._collection = client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=self._embedding_function(),
            )
        except Exception as exc:  # pragma: no cover
            self._disabled = True
            logger.exception("[Vector] ChromaDB başlatılamadı: %s", exc)

    @property
    def enabled(self) -> bool:
        return not self._disabled

    def seed(self, force: bool = False) -> int:
        """Embed every customer's behaviour doc (idempotent)."""
        if self._disabled:
            return 0
        try:
            existing: set[str] = set(self._collection.get(limit=100_000)["ids"])
            with self.customers_path.open("r", encoding="utf-8") as fh:
                records = json.load(fh)
            documents, ids, metadatas = [], [], []
            for record in records:
                cid = str(record.get("customer_id", ""))
                if not cid or (not force and cid in existing):
                    continue
                documents.append(behaviour_document(record))
                ids.append(cid)
                metadatas.append({"customer_id": cid, "kind": "profile"})
            if documents:
                self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
                logger.info("[Vector] %d müşteri davranışı gömüldü", len(documents))
            return len(documents)
        except Exception as exc:  # pragma: no cover
            self._disabled = True
            logger.exception("[Vector] seeding hatası: %s", exc)
            return 0

    def retrieve(self, transaction: dict[str, Any]) -> Retrieved | None:
        """Retrieve the customer's behaviour document most similar to ``transaction``."""
        if self._disabled:
            return None
        customer_id = str(transaction.get("customer_id", ""))
        try:
            if not customer_id or self._collection.count() == 0:
                return None
            results = self._collection.query(
                query_texts=[transaction_document(transaction)],
                n_results=1,
                where={"customer_id": customer_id},
            )
            docs, dists = results.get("documents"), results.get("distances")
            if not docs or not docs[0]:
                return None
            return Retrieved(document=str(docs[0][0]), distance=float(dists[0][0]))  # type: ignore[index]
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            logger.debug("[Vector] sorgu hatası: %s", exc)
            return None

    def semantic_distance(self, transaction: dict[str, Any]) -> float | None:
        hit = self.retrieve(transaction)
        return hit.distance if hit else None

    @property
    def count(self) -> int:
        if self._disabled:
            return 0
        try:
            return int(self._collection.count())
        except Exception:  # noqa: BLE001  # pragma: no cover
            return 0
