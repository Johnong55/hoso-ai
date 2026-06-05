"""
Smoke test Phase 3: chạy `crawl_single_procedure` end-to-end (qua DB +
Qdrant) cho 1 mã thủ tục để verify pipeline mới.

Chạy:
    python -m scripts.smoke_single 1.001020
"""
from __future__ import annotations

import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from sqlalchemy import select

from app.db.base import AsyncSessionLocal, engine
from app.models.procedure import Procedure, ProcedureFee, ProcedureRequirement
from app.models.document import DocumentChunk
from app.worker.tasks import _crawl_single_async, _ensure_manual_source


class _FakeTask:
    def retry(self, exc=None):
        raise exc


async def main(code: str) -> int:
    print(f"\n=== SMOKE single | code={code} ===\n")

    try:
        result = await _crawl_single_async(_FakeTask(), code)
        print(f"task result: {result}")

        # Verify DB state
        async with AsyncSessionLocal() as db:
            p = (await db.execute(
                select(Procedure).where(Procedure.code == code)
            )).scalar_one_or_none()
            if not p:
                print("[FAIL] no Procedure row written")
                return 1
            print(f"[DB] procedure.id={p.id}")
            print(f"     name={p.name[:80]}...")
            print(f"     domain={p.domain}")
            print(f"     source_updated_at={p.source_updated_at}")
            print(f"     processing_time={p.processing_time}")
            print(f"     fee={p.fee}")

            fees = (await db.execute(
                select(ProcedureFee).where(ProcedureFee.procedure_id == p.id)
            )).scalars().all()
            print(f"     fees: {len(fees)}")

            reqs = (await db.execute(
                select(ProcedureRequirement).where(ProcedureRequirement.procedure_id == p.id)
            )).scalars().all()
            print(f"     requirements: {len(reqs)}")
            with_form = sum(1 for r in reqs if r.form_url)
            print(f"     requirements with form_url: {with_form}")

            chunks = (await db.execute(
                select(DocumentChunk).where(
                    DocumentChunk.procedure_id == p.id,
                    DocumentChunk.is_current == True,  # noqa: E712
                )
            )).scalars().all()
            print(f"     current chunks: {len(chunks)}")

        # Run again — should hit SKIPPED_UNCHANGED branch
        print("\n--- re-running to test change detection ---")
        result2 = await _crawl_single_async(_FakeTask(), code)
        print(f"second result: {result2}")
        # The 2nd run: parsed flows through _process_parsed_procedure which
        # returns SKIPPED_UNCHANGED (-1). _crawl_single_async returns 'chunks=-1'.
        if result2.get("chunks") == -1:
            print("[OK] change detection hit (SKIPPED_UNCHANGED)")
        else:
            print(f"[WARN] expected skip, got chunks={result2.get('chunks')}")

        print("\n=== SMOKE OK ===\n")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "1.001020"
    rc = asyncio.run(main(arg))
    sys.exit(rc)
