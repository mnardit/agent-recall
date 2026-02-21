import pytest
from agent_recall.store import MemoryStore
from agent_recall.hierarchy import ScopedView


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    e_max = s.resolve_entity("Jordan", "person")
    s.set_slot(e_max, "role", "owner", scope="global")
    s.set_slot(e_max, "timezone", "UTC", scope="global")

    e_alice = s.resolve_entity("Alice", "person")
    s.set_slot(e_alice, "role", "PM", scope="acme")

    e_acme = s.resolve_entity("Acme", "agency")
    s.set_slot(e_acme, "status", "active", scope="acme")

    e_bob = s.resolve_entity("Bob", "person")
    s.set_slot(e_bob, "role", "contact", scope="client-a")

    s.set_slot(e_acme, "status", "paused", scope="client-a")
    yield s
    s.close()


def test_scoped_get_local(store):
    view = ScopedView(store, chain=["global", "acme", "client-a"])
    assert view.get("Bob", "role") == "contact"


def test_scoped_get_inherited(store):
    view = ScopedView(store, chain=["global", "acme", "client-a"])
    assert view.get("Jordan", "role") == "owner"
    assert view.get("Alice", "role") == "PM"


def test_local_overrides_parent(store):
    view = ScopedView(store, chain=["global", "acme", "client-a"])
    assert view.get("Acme", "status") == "paused"


def test_narrow_chain(store):
    view = ScopedView(store, chain=["global", "acme"])
    assert view.get("Bob", "role") is None
    assert view.get("Acme", "status") == "active"


def test_scoped_list_entities(store):
    view = ScopedView(store, chain=["global", "acme", "client-a"])
    persons = view.list_entities(entity_type="person")
    names = {p["name"] for p in persons}
    assert names == {"Jordan", "Alice", "Bob"}


def test_scoped_set_writes_to_local(store):
    view = ScopedView(store, chain=["global", "acme", "client-a"])
    view.set("NewPerson", "person", "name", "Test")
    eid = store.find_entity("NewPerson", "person")
    assert store.get_slot(eid, "name", scope_chain=["client-a"]) == "Test"
    assert store.get_slot(eid, "name", scope_chain=["acme"]) is None
