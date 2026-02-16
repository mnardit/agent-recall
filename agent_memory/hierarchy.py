"""Hierarchical scope queries — chain-based inheritance using store methods only."""
from agent_memory.store import MemoryStore


class ScopedView:
    """View into a MemoryStore filtered by scope chain.

    Chain order: ["global", "acme", "client-a"] — last scope is most local.
    Local scope overrides parent for same entity+slot.
    All operations go through MemoryStore public API — no _conn access.
    """

    def __init__(self, store: MemoryStore, chain: list[str]) -> None:
        if not chain:
            raise ValueError("Chain must have at least one scope")
        self._store = store
        self._chain = chain

    @property
    def local_scope(self) -> str:
        return self._chain[-1]

    def get(self, entity_name: str, key: str) -> str | None:
        entity_id = self._store.find_entity(entity_name)
        if entity_id is None:
            return None
        return self._store.get_slot(entity_id, key, scope_chain=self._chain)

    def get_entity(self, name: str) -> dict | None:
        entity_id = self._store.find_entity(name)
        if entity_id is None:
            return None
        entity = self._store.get_entity(entity_id)
        if entity is None:
            return None
        entity["slots"] = self._store.get_slots(entity_id, scope_chain=self._chain)
        return entity

    def list_entities(self, entity_type: str | None = None) -> list[dict]:
        return self._store.list_entities_in_scopes(self._chain, entity_type=entity_type)

    def set(self, entity_name: str, entity_type: str, key: str, value: str,
            **kwargs) -> None:
        entity_id = self._store.resolve_entity(entity_name, entity_type)
        self._store.set_slot(entity_id, key, value, scope=self.local_scope, **kwargs)

    def add_log(self, entity_name: str, text: str, **kwargs) -> None:
        entity_id = self._store.find_entity(entity_name)
        if entity_id is not None:
            self._store.add_log(entity_id, text, **kwargs)
