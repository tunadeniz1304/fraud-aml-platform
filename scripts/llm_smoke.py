"""LLM smoke test (§3.3).

With a key in ``.env`` (``Anil3/.env`` → ``../.env``) it makes **one** real
call and prints ``OK model=… latency=…ms``; without a key it prints
``DEMO modu``. Exit code 0 on success, 1 on failure. The key is never printed.

    python scripts/llm_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel

from app.llm.config import LLMConfigError, LLMSettings
from app.llm.service import LLMService


class SmokeReply(BaseModel):
    status: str


async def run() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    settings = LLMSettings()
    print(settings.startup_line())
    try:
        service = LLMService(settings)
    except LLMConfigError as exc:
        print(f"HATA: {exc}")
        return 1
    if service.mode == "demo":
        print("DEMO modu — canlı çağrı yapılmadı (anahtar yok veya LLM_MODE=demo)")
        return 0
    result = await service.generate(
        task="smoke",
        system="Sen bir sağlık kontrolü yanıtlayıcısısın.",
        payload={"ping": "pong"},
        schema=SmokeReply,
        instruction='Yalnızca {"status": "ok"} JSON nesnesini döndür.',
    )
    if result.llm_mode != "live":
        print(f"BAŞARISIZ: canlı çağrı düştü ({result.llm_error_kind}) — demo yanıtı kullanıldı")
        return 1
    print(
        f"OK model={result.model} latency={result.latency_ms:.0f}ms "
        f"tokens={result.usage.prompt_tokens}/{result.usage.completion_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
