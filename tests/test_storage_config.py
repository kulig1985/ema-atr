"""Storage.initialize config handling, against an in-memory stand-in for Mongo."""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import DEFAULT_CONFIG
from app.storage import COLLECTIONS, Storage

NEW_KEYS = ["logStatusSec", "maxSymbols", "minQuoteVolume24h", "quoteAsset", "symbolAutoPopulate"]


class FakeCollection:
    def __init__(self, document: dict[str, Any] | None = None) -> None:
        self.document = document
        self.index_names: list[str] = []

    async def create_index(self, keys, **kwargs) -> None:
        self.index_names.append(kwargs.get("name", ""))

    async def find_one(self, query):
        return dict(self.document) if self.document is not None else None

    async def insert_one(self, document):
        self.document = dict(document)

    async def update_one(self, query, update, upsert: bool = False):
        self.document = {**(self.document or {}), **update["$set"]}


class FakeDb:
    def __init__(self, config_document: dict[str, Any] | None) -> None:
        self.config = FakeCollection(config_document)
        self.collections = {name: FakeCollection() for name in COLLECTIONS if name != "config"}
        self.created: list[str] = []

    async def list_collection_names(self) -> list[str]:
        return ["config", *self.collections]

    async def create_collection(self, name: str) -> None:
        self.created.append(name)

    def __getattr__(self, name: str):
        return self.collections[name]


class FakeAdmin:
    async def command(self, *args, **kwargs) -> dict[str, Any]:
        return {"ok": 1}


def initialize_with(config_document: dict[str, Any] | None):
    storage = Storage.__new__(Storage)          # skip the real Mongo client
    storage.db = FakeDb(config_document)
    storage.client = type("C", (), {"admin": FakeAdmin()})()
    config = asyncio.run(storage.initialize())
    return config, storage.db.config.document


def test_empty_config_collection_is_seeded_with_every_default_key() -> None:
    config, stored = initialize_with(None)
    for key in NEW_KEYS:
        assert key in stored, f"{key} missing from the inserted document"
        assert stored[key] == DEFAULT_CONFIG[key]
    assert config.symbol_auto_populate is DEFAULT_CONFIG["symbolAutoPopulate"]
    assert config.max_symbols == DEFAULT_CONFIG["maxSymbols"]
    assert config.quote_asset == DEFAULT_CONFIG["quoteAsset"]


def test_config_document_from_an_older_version_gains_the_new_keys() -> None:
    legacy = {key: value for key, value in DEFAULT_CONFIG.items() if key not in NEW_KEYS}
    legacy["xEntry"] = 2.25
    legacy["symbols"] = ["SOLUSDT"]

    config, stored = initialize_with(legacy)

    for key in NEW_KEYS:
        assert stored[key] == DEFAULT_CONFIG[key]
    # Existing settings survive the merge.
    assert stored["xEntry"] == 2.25
    assert config.symbols == ["SOLUSDT"]


def test_existing_values_are_never_overwritten_by_defaults() -> None:
    customized = {**DEFAULT_CONFIG, "maxSymbols": 12, "symbolAutoPopulate": True}
    config, stored = initialize_with(customized)
    assert stored["maxSymbols"] == 12
    assert config.symbol_auto_populate is True
