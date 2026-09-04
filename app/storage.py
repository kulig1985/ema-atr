from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, UpdateOne

from .config import DEFAULT_CONFIG, StrategyConfig, validate_config_document

logger = logging.getLogger(__name__)

COLLECTIONS = ["config", "candles", "flow_1m", "signals", "heartbeats"]


class Storage:
    def __init__(self, mongo_uri: str):
        self.client = AsyncMongoClient(mongo_uri, tz_aware=True)
        self.db = self.client.get_default_database(default="shadow_signals")

    async def initialize(self) -> StrategyConfig:
        await self.client.admin.command("ping")
        existing = set(await self.db.list_collection_names())
        for name in COLLECTIONS:
            if name not in existing:
                await self.db.create_collection(name)
        unexpected = set(await self.db.list_collection_names()) - set(COLLECTIONS)
        if unexpected:
            logger.info(
                "Database has %d collection(s) this project does not use (ignored): %s",
                len(unexpected),
                sorted(unexpected),
            )

        await self.db.candles.create_index(
            [("symbol", ASCENDING), ("timeframe", ASCENDING), ("openTime", ASCENDING)],
            unique=True,
            name="uq_candle",
        )
        await self.db.flow_1m.create_index(
            [("symbol", ASCENDING), ("bucketStart", ASCENDING)],
            unique=True,
            name="uq_flow_bucket",
        )
        await self.db.signals.create_index(
            [("symbol", ASCENDING), ("signalAt", DESCENDING)], name="ix_signal_symbol_time"
        )
        await self.db.signals.create_index(
            [("measurementStatus", ASCENDING), ("signalAt", ASCENDING)], name="ix_signal_measurement"
        )
        await self.db.heartbeats.create_index([("ts", DESCENDING)], name="ix_heartbeat_ts")

        document = await self.db.config.find_one({"_id": "strategy"})
        if document is None:
            document = dict(DEFAULT_CONFIG)
            await self.db.config.insert_one(document)
            logger.info("Inserted default strategy config into MongoDB")
        else:
            added = {key: value for key, value in DEFAULT_CONFIG.items() if key not in document}
            if added:
                await self.db.config.update_one({"_id": "strategy"}, {"$set": added})
                document.update(added)
                logger.info("Added new config fields with defaults: %s", sorted(added))
        validate_config_document(document)
        return StrategyConfig(document=document)

    async def save_candles(self, candles: list[dict[str, Any]]) -> None:
        if not candles:
            return
        operations = []
        for candle in candles:
            doc = dict(candle)
            doc["updatedAt"] = datetime.now(timezone.utc)
            operations.append(
                UpdateOne(
                    {
                        "symbol": doc["symbol"],
                        "timeframe": doc["timeframe"],
                        "openTime": doc["openTime"],
                    },
                    {"$set": doc},
                    upsert=True,
                )
            )
        await self.db.candles.bulk_write(operations, ordered=False)

    async def save_flow_bucket(self, bucket: dict[str, Any]) -> None:
        doc = dict(bucket)
        doc["updatedAt"] = datetime.now(timezone.utc)
        await self.db.flow_1m.update_one(
            {"symbol": doc["symbol"], "bucketStart": doc["bucketStart"]},
            {"$set": doc},
            upsert=True,
        )

    async def insert_signal(self, document: dict[str, Any]):
        result = await self.db.signals.insert_one(document)
        return result.inserted_id

    async def update_signal(self, signal_id, fields: dict[str, Any]) -> None:
        fields = dict(fields)
        fields["updatedAt"] = datetime.now(timezone.utc)
        await self.db.signals.update_one({"_id": signal_id}, {"$set": fields})

    async def latest_signal(self, symbol: str) -> dict[str, Any] | None:
        return await self.db.signals.find_one({"symbol": symbol}, sort=[("signalAt", DESCENDING)])

    async def mark_stale_active_measurements_interrupted(self) -> int:
        result = await self.db.signals.update_many(
            {"measurementStatus": "ACTIVE"},
            {
                "$set": {
                    "measurementStatus": "INTERRUPTED",
                    "measurementInterruptedAt": datetime.now(timezone.utc),
                    "measurementInterruptReason": "process_restart",
                }
            },
        )
        return int(result.modified_count)

    async def insert_heartbeat(self, document: dict[str, Any]) -> None:
        await self.db.heartbeats.insert_one(document)

    async def close(self) -> None:
        await self.client.close()
