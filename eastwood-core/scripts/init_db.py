#!/usr/bin/env python3
"""
One-shot DB init. Run inside the container:
  docker compose exec eastwood-core python scripts/init_db.py
"""
import asyncio
import logging

from db.models import Base
from db.session import _get_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    logger.info("all tables created (or already existed)")


if __name__ == "__main__":
    asyncio.run(main())
