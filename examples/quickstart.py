#!/usr/bin/env python3
"""Quick start — create entities, add observations, query with scopes."""

from agent_recall import MemoryStore, ScopedView

# Create an in-memory store (use a path for persistence)
with MemoryStore(":memory:") as store:
    # Create entities
    alice = store.resolve_entity("Alice", "person")
    acme = store.resolve_entity("Acme Corp", "client")

    # Set scoped slots
    store.set_slot(alice, "role", "Engineer", scope="global")
    store.set_slot(alice, "role", "Lead Engineer", scope="acme")
    store.add_observation(alice, "Prefers async communication", scope="acme")

    # Create a relation
    store.add_relation(alice, acme, "works_at")

    # Query with scope chain — local overrides global
    view = ScopedView(store, ["global", "acme"])
    entity = view.get_entity("Alice")
    print(f"{entity['name']} ({entity['type']})")
    print(f"  Role: {entity['slots']['role']}")  # "Lead Engineer" (acme overrides global)
    print(f"  Observations: {[o['text'] for o in entity['observations']]}")

    # Search
    results = store.search("engineer")
    print(f"\nSearch 'engineer': {[r['name'] for r in results]}")
