from __future__ import annotations


EVOLUTION_STORE_SCHEMA_VERSION = 19


class UnsupportedEvolutionStoreSchemaError(RuntimeError):
    """The optional local catalog was created by an unsupported schema."""
