"""Tests for entity deduplication utilities."""
import pytest
from agent_recall.store import MemoryStore
from agent_recall.contrib.dedup import (
    normalize_name, extract_first_name, extract_all_name_parts,
    name_similarity, find_candidates, merge_entities,
)


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    yield s
    s.close()


# --- Name normalization ---

def test_normalize_name_basic():
    assert normalize_name("Alice Smith") == "Alice Smith"


def test_normalize_name_strips_disambiguator():
    assert normalize_name("Alice (marketing)") == "Alice"


def test_normalize_name_multiple_disambiguators():
    assert normalize_name("Alice (marketing) Smith (team lead)") == "Alice Smith"


def test_extract_first_name():
    assert extract_first_name("Alice Smith") == "Alice"


def test_extract_first_name_with_disambiguator():
    assert extract_first_name("Alice (marketing) Smith") == "Alice"


def test_extract_all_name_parts():
    parts = extract_all_name_parts("Alice (Marketing) Smith")
    assert parts == {"alice", "marketing", "smith"}


def test_extract_all_name_parts_unicode():
    parts = extract_all_name_parts("Müller Straße")
    assert "müller" in parts
    assert "straße" in parts


# --- Similarity scoring ---

def test_similarity_exact_match():
    score, reason = name_similarity("Alice Smith", "Alice Smith")
    assert score == 1.0


def test_similarity_exact_ignoring_disambiguator():
    score, reason = name_similarity("Alice Smith", "Alice (marketing) Smith")
    assert score == 1.0
    assert "exact" in reason


def test_similarity_shared_first_name():
    score, reason = name_similarity("Alice Smith", "Alice Jones")
    assert score == 0.5
    assert "first name" in reason


def test_similarity_shared_multiple_names():
    score, reason = name_similarity("Alice Maria Smith", "Alice Maria Jones")
    assert score == 0.8
    assert "shared names" in reason


def test_similarity_name_subset():
    # "Alice" vs "Alice Smith": first-name match fires first (score 0.5)
    # Subset check only triggers when first names differ
    score, reason = name_similarity("Alice", "Alice Smith")
    assert score == 0.5

    # True subset case: different first names
    score2, reason2 = name_similarity("Maria Elena", "Maria Elena Rodriguez")
    assert score2 == 0.8  # shared names >= 2


def test_similarity_no_match():
    score, reason = name_similarity("Alice", "Bob")
    assert score == 0.0


def test_similarity_short_first_name():
    """Short first names (<=2 chars) don't trigger same-first-name match."""
    score, _ = name_similarity("Li Wei", "Li Chen")
    assert score == 0.0


# --- Candidate finding ---

def test_find_candidates_basic(store):
    id1 = store.resolve_entity("Alice Smith", "person")
    id2 = store.resolve_entity("Alice (marketing) Smith", "person")
    store.resolve_entity("Bob Jones", "person")

    candidates = find_candidates(store)
    assert len(candidates) == 1
    assert candidates[0]["score"] == 1.0
    names = {candidates[0]["name1"], candidates[0]["name2"]}
    assert names == {"Alice Smith", "Alice (marketing) Smith"}


def test_find_candidates_respects_not_same(store):
    id1 = store.resolve_entity("Alice Smith", "person")
    id2 = store.resolve_entity("Alice Jones", "person")
    # Mark as not same
    store.add_relation(id1, id2, "not_same_as")

    candidates = find_candidates(store)
    assert len(candidates) == 0


def test_find_candidates_sorted_by_score(store):
    store.resolve_entity("Alice Smith", "person")
    store.resolve_entity("Alice (marketing) Smith", "person")  # exact=1.0
    store.resolve_entity("Alice Jones", "person")  # first name=0.5

    candidates = find_candidates(store)
    assert len(candidates) >= 2
    assert candidates[0]["score"] >= candidates[1]["score"]


# --- Merge ---

def test_merge_moves_slots(store):
    id1 = store.resolve_entity("Alice Smith", "person")
    id2 = store.resolve_entity("Alice (marketing)", "person")
    store.set_slot(id1, "role", "Engineer")
    store.set_slot(id2, "email", "alice@example.com")

    merge_entities(store, keep_id=id1, remove_id=id2)

    # Keep entity has both slots
    slots = store.get_slots(id1)
    assert slots.get("role") == "Engineer"
    assert slots.get("email") == "alice@example.com"
    # Removed entity is gone
    assert store.get_entity(id2) is None


def test_merge_moves_observations(store):
    id1 = store.resolve_entity("Alice Smith", "person")
    id2 = store.resolve_entity("Alice (marketing)", "person")
    store.add_observation(id1, "Fact A")
    store.add_observation(id2, "Fact B")

    merge_entities(store, keep_id=id1, remove_id=id2)

    obs = store.get_observations(id1)
    texts = [o["text"] for o in obs]
    assert "Fact A" in texts
    assert "Fact B" in texts


def test_merge_skips_duplicate_observations(store):
    id1 = store.resolve_entity("Alice Smith", "person")
    id2 = store.resolve_entity("Alice (marketing)", "person")
    store.add_observation(id1, "Same fact")
    store.add_observation(id2, "Same fact")

    merge_entities(store, keep_id=id1, remove_id=id2)

    obs = store.get_observations(id1)
    texts = [o["text"] for o in obs]
    assert texts.count("Same fact") == 1


def test_merge_moves_relations(store):
    id1 = store.resolve_entity("Alice Smith", "person")
    id2 = store.resolve_entity("Alice (marketing)", "person")
    acme_id = store.resolve_entity("Acme", "client")
    store.add_relation(id2, acme_id, "contact_for")

    merge_entities(store, keep_id=id1, remove_id=id2)

    rels = store.get_relations(id1)
    assert any(r["to_name"] == "Acme" and r["type"] == "contact_for"
               for r in rels)


def test_merge_logs_action(store):
    id1 = store.resolve_entity("Alice Smith", "person")
    id2 = store.resolve_entity("Alice (marketing)", "person")

    merge_entities(store, keep_id=id1, remove_id=id2)

    logs = store.get_logs(id1)
    assert any("Merged" in log["text"] for log in logs)


def test_merge_skips_duplicate_slots(store):
    id1 = store.resolve_entity("Alice Smith", "person")
    id2 = store.resolve_entity("Alice (marketing)", "person")
    store.set_slot(id1, "role", "Engineer")
    store.set_slot(id2, "role", "Manager")  # same key, same scope

    merge_entities(store, keep_id=id1, remove_id=id2)

    # Keep entity retains its own value (duplicate key+scope skipped)
    slots = store.get_slots(id1)
    assert slots["role"] == "Engineer"
