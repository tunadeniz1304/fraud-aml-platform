"""Download the public fraud datasets used by the validation (docs/DATA.md).

    python scripts/fetch_public_fraud_data.py                 # PaySim + Elliptic + ULB
    python scripts/fetch_public_fraud_data.py --only paysim
    python scripts/fetch_public_fraud_data.py --list

* Streaming, resumable downloads (HTTP ``Range``) with a progress bar.
* Checksums: PaySim's MD5 comes from the Zenodo API; the other files are
  pinned by SHA-256 in ``scripts/public_data_checksums.json`` (recorded on the
  first verified download, compared on every later run).
* Everything lands in ``data/external/<set>/`` (git-ignored). This is the only
  part of the project that touches the network; tests use the small fixtures
  under ``tests/fixtures/``.
* Kaggle-only sets (IEEE-CIS, BAF, IBM AML) are used only when
  ``KAGGLE_USERNAME`` and ``KAGGLE_KEY`` exist in the environment — their
  values are never read or printed. Otherwise they are reported as skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = ROOT / "data" / "external"
CHECKSUMS = ROOT / "scripts" / "public_data_checksums.json"
CHUNK = 1 << 20
TIMEOUT = httpx.Timeout(60.0, read=300.0)


@dataclass
class RemoteFile:
    name: str
    url: str
    size: int | None = None
    md5: str | None = None  # published by the source (Zenodo)
    unzip: bool = False


@dataclass
class Dataset:
    key: str
    title: str
    source: str
    license: str
    citation: str
    files: list[RemoteFile] = field(default_factory=list)


ZENODO_RECORD = "22761688"
PYG_ELLIPTIC = "https://data.pyg.org/datasets/elliptic"


def paysim() -> Dataset:
    api = f"https://zenodo.org/api/records/{ZENODO_RECORD}"
    record = httpx.get(api, timeout=TIMEOUT, follow_redirects=True).json()
    files = [
        RemoteFile(
            name=f["key"],
            url=f["links"]["self"],
            size=int(f["size"]),
            md5=str(f["checksum"]).removeprefix("md5:"),
        )
        for f in record["files"]
        if f["key"].endswith(".csv")
    ]
    return Dataset(
        key="paysim",
        title="PaySim — synthetic mobile money transactions",
        source=f"https://zenodo.org/records/{ZENODO_RECORD}",
        license=str((record.get("metadata") or {}).get("license", {}).get("id", "cc-by-4.0")),
        citation=(
            "E. A. Lopez-Rojas, A. Elmir, S. Axelsson. PaySim: A financial mobile money "
            "simulator for fraud detection. 28th European Modeling and Simulation "
            "Symposium (EMSS), 2016."
        ),
        files=files,
    )


def elliptic() -> Dataset:
    names = ("elliptic_txs_features.csv", "elliptic_txs_edgelist.csv", "elliptic_txs_classes.csv")
    return Dataset(
        key="elliptic",
        title="Elliptic Bitcoin transaction graph (illicit / licit)",
        source=f"{PYG_ELLIPTIC}/ (mirror used by torch_geometric.datasets.EllipticBitcoinDataset)",
        license=(
            "CC BY-NC-ND 4.0 (Kaggle: ellipticco/elliptic-data-set) — ticari olmayan kullanım, "
            "türev paylaşımı yok"
        ),
        citation=(
            "M. Weber, G. Domeniconi, J. Chen, D. K. I. Weidele, C. Bellei, T. Robinson, "
            "C. E. Leiserson. Anti-Money Laundering in Bitcoin: Experimenting with Graph "
            "Convolutional Networks for Financial Forensics. KDD '19 Workshop on Anomaly "
            "Detection in Finance, 2019. arXiv:1908.02591."
        ),
        files=[RemoteFile(n + ".zip", f"{PYG_ELLIPTIC}/{n}.zip", unzip=True) for n in names],
    )


def ulb_meta() -> Dataset:
    return Dataset(
        key="ulb",
        title="ULB Credit Card Fraud Detection (OpenML 1597)",
        source="https://www.openml.org/d/1597",
        license="OpenML lisans alanı: Public",
        citation=(
            "A. Dal Pozzolo, O. Caelen, R. A. Johnson, G. Bontempi. Calibrating Probability "
            "with Undersampling for Unbalanced Classification. IEEE SSCI, 2015."
        ),
    )


KAGGLE_SETS = {
    "ieee_cis": "IEEE-CIS Fraud Detection (Kaggle competition)",
    "baf": "Bank Account Fraud (NeurIPS 2022, Kaggle)",
    "ibm_aml": "IBM Transactions for Anti Money Laundering (Kaggle)",
}


# --- helpers ------------------------------------------------------------------------------
def _hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _progress(done: int, total: int | None, name: str) -> None:
    if total:
        pct = done / total
        bar = "#" * int(pct * 30)
        sys.stdout.write(f"\r  {name[:38]:<38} [{bar:<30}] {pct:6.1%} {done / 1e6:8.1f} MB")
    else:
        sys.stdout.write(f"\r  {name[:38]:<38} {done / 1e6:8.1f} MB")
    sys.stdout.flush()


def download(remote: RemoteFile, target: Path) -> Path:
    """Resumable streaming download into ``target`` (``.part`` while running)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (remote.size is None or target.stat().st_size == remote.size):
        print(f"  {remote.name}: mevcut, indirme atlandı")
        return target
    part = target.with_suffix(target.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.stream(
        "GET", remote.url, headers=headers, timeout=TIMEOUT, follow_redirects=True
    ) as resp:
        if resp.status_code == 416:  # already complete
            part.rename(target)
            return target
        resp.raise_for_status()
        resumed = resp.status_code == 206
        if not resumed:
            have = 0
        length = int(resp.headers.get("content-length") or 0)
        total = remote.size or (have + length if length else None)
        with part.open("ab" if resumed else "wb") as fh:
            done = have
            for chunk in resp.iter_bytes(CHUNK):
                fh.write(chunk)
                done += len(chunk)
                _progress(done, total, remote.name)
    sys.stdout.write("\n")
    part.replace(target)
    return target


def _load_pins() -> dict[str, str]:
    if CHECKSUMS.exists():
        return dict(json.loads(CHECKSUMS.read_text(encoding="utf-8")))
    return {}


def verify(dataset: Dataset, path: Path, remote: RemoteFile, pins: dict[str, str]) -> str:
    """Return the SHA-256 of ``path`` after checking MD5 / pinned SHA-256."""
    sha = _hash(path, "sha256")
    if remote.md5 is not None:
        md5 = _hash(path, "md5")
        if md5 != remote.md5:
            path.unlink()
            raise SystemExit(f"{remote.name}: MD5 uyuşmuyor ({md5} ≠ {remote.md5}) — silindi")
    key = f"{dataset.key}/{remote.name}"
    pinned = pins.get(key)
    if pinned and pinned != sha:
        raise SystemExit(f"{remote.name}: SHA-256 sabitlenen değerle uyuşmuyor ({key})")
    pins[key] = sha
    return sha


def fetch_files(dataset: Dataset, pins: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for remote in dataset.files:
        path = download(remote, EXTERNAL / dataset.key / remote.name)
        sha = verify(dataset, path, remote, pins)
        if remote.unzip:
            with zipfile.ZipFile(path) as archive:
                archive.extractall(path.parent)
        out.append(
            {"file": remote.name, "bytes": path.stat().st_size, "sha256": sha, "md5": remote.md5}
        )
    return out


def fetch_ulb(pins: dict[str, str]) -> list[dict[str, Any]]:
    from sklearn.datasets import fetch_openml

    target = EXTERNAL / "ulb" / "creditcard.csv"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        print("  OpenML 1597 indiriliyor (sklearn.fetch_openml)…")
        frame = fetch_openml(data_id=1597, as_frame=True, data_home=str(EXTERNAL / "ulb"))
        df = frame.frame
        df.to_csv(target, index=False)
    sha = _hash(target, "sha256")
    key = "ulb/creditcard.csv"
    if pins.get(key) and pins[key] != sha:
        raise SystemExit("ulb/creditcard.csv: SHA-256 sabitlenen değerle uyuşmuyor")
    pins[key] = sha
    return [{"file": target.name, "bytes": target.stat().st_size, "sha256": sha, "md5": None}]


def kaggle_status() -> dict[str, str]:
    available = bool(os.environ.get("KAGGLE_USERNAME")) and bool(os.environ.get("KAGGLE_KEY"))
    reason = "kimlik bilgisi var (manuel indirme gerekir)" if available else "atlandı"
    return {key: f"{title}: {reason}" for key, title in KAGGLE_SETS.items()}


def write_manifest(entries: dict[str, Any]) -> Path:
    path = EXTERNAL / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    previous.update(entries)
    path.write_text(json.dumps(previous, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", choices=["paysim", "elliptic", "ulb"], action="append")
    parser.add_argument("--list", action="store_true", help="kaynakları yazdır ve çık")
    args = parser.parse_args()
    wanted = args.only or ["paysim", "elliptic", "ulb"]
    if args.list:
        for ds in (elliptic(), ulb_meta()):
            print(f"{ds.key}: {ds.title} — {ds.source} ({ds.license})")
        print(f"paysim: Zenodo record {ZENODO_RECORD}")
        return
    pins = _load_pins()
    manifest: dict[str, Any] = {}
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for key in wanted:
        print(f"[{key}]")
        if key == "ulb":
            meta, files = ulb_meta(), fetch_ulb(pins)
        else:
            meta = paysim() if key == "paysim" else elliptic()
            files = fetch_files(meta, pins)
        manifest[key] = {
            "title": meta.title,
            "source": meta.source,
            "license": meta.license,
            "citation": meta.citation,
            "downloaded_at": now,
            "files": files,
        }
    manifest["kaggle"] = kaggle_status()
    CHECKSUMS.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Manifest: {write_manifest(manifest)}")
    print(f"Checksum'lar: {CHECKSUMS}")
    for line in manifest["kaggle"].values():
        print(f"  Kaggle — {line}")


if __name__ == "__main__":
    main()
