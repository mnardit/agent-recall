"""Entity deduplication utilities — fuzzy name matching and merge logic."""
import re

from claude_memory.store import MemoryStore


def normalize_name(name: str) -> str:
    """Strip parenthetical disambiguators and normalize whitespace."""
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()
    return cleaned


def extract_first_name(name: str) -> str:
    """Extract first name from full name."""
    parts = normalize_name(name).split()
    return parts[0] if parts else name


def extract_all_name_parts(name: str) -> set[str]:
    """Extract all name tokens including those in parentheses."""
    tokens = re.findall(r"[a-zA-Z\u00c0-\u024f\u0400-\u04ff]+", name.lower())
    return set(tokens)


def name_similarity(name1: str, name2: str) -> tuple[float, str]:
    """Calculate similarity score between two names. Returns (score, reason).

    Score: 0.0 = no match, 1.0 = very likely same person.
    """
    parts1 = extract_all_name_parts(name1)
    parts2 = extract_all_name_parts(name2)

    # Exact normalized match
    if normalize_name(name1).lower() == normalize_name(name2).lower():
        return 1.0, "exact match (ignoring disambiguators)"

    # Shared first name
    first1 = extract_first_name(name1).lower()
    first2 = extract_first_name(name2).lower()
    if first1 == first2 and len(first1) > 2:
        overlap = parts1 & parts2
        if len(overlap) >= 2:
            return 0.8, f"shared names: {overlap}"
        return 0.5, f"same first name: {first1}"

    # One name is a subset of the other
    if parts1 and parts2:
        if parts1 < parts2 or parts2 < parts1:
            return 0.7, f"name subset: {parts1 & parts2}"

    # Significant token overlap (>50%)
    if parts1 and parts2:
        overlap = parts1 & parts2
        ratio = len(overlap) / min(len(parts1), len(parts2))
        if ratio > 0.5 and len(overlap) >= 2:
            return 0.6, f"token overlap: {overlap}"

    return 0.0, ""


def get_not_same_pairs(store: MemoryStore) -> set[tuple[int, int]]:
    """Get all entity ID pairs marked as not_same_as."""
    pairs = set()
    for r in store.get_relations_by_type("not_same_as"):
        pair = (min(r["from_id"], r["to_id"]), max(r["from_id"], r["to_id"]))
        pairs.add(pair)
    return pairs


def find_candidates(store: MemoryStore) -> list[dict]:
    """Find potential duplicate person pairs."""
    people = store.list_entities(entity_type="person")
    not_same = get_not_same_pairs(store)
    candidates = []

    for i, p1 in enumerate(people):
        for p2 in people[i + 1:]:
            pair_key = (min(p1["id"], p2["id"]), max(p1["id"], p2["id"]))
            if pair_key in not_same:
                continue

            score, reason = name_similarity(p1["name"], p2["name"])
            if score >= 0.5:
                candidates.append({
                    "id1": p1["id"], "name1": p1["name"],
                    "id2": p2["id"], "name2": p2["name"],
                    "score": score, "reason": reason,
                })

    candidates.sort(key=lambda c: -c["score"])
    return candidates


def merge_entities(store: MemoryStore, keep_id: int, remove_id: int) -> None:
    """Merge remove_id into keep_id: move slots, observations, relations."""
    # Move slots (skip duplicates)
    existing_keys = {(s["key"], s["scope"]) for s in store.get_raw_slots(keep_id)}
    for slot in store.get_raw_slots(remove_id):
        if (slot["key"], slot["scope"]) not in existing_keys:
            store.set_slot(keep_id, slot["key"], slot["value"],
                           scope=slot["scope"], confidence=slot["confidence"],
                           source=slot["source"])

    # Move observations (skip duplicates by text+scope)
    existing_obs = {(o["text"], o.get("scope", "global"))
                    for o in store.get_observations(keep_id)}
    for obs in store.get_observations(remove_id):
        if (obs["text"], obs.get("scope", "global")) not in existing_obs:
            store.add_observation(keep_id, obs["text"],
                                 scope=obs.get("scope", "global"))

    # Move relations (outgoing + incoming)
    outgoing, incoming = store.get_all_relations(remove_id)
    for rel in outgoing:
        if rel["to_id"] != keep_id:
            store.add_relation(keep_id, rel["to_id"], rel["type"],
                               scope=rel["scope"], context=rel["context"])
    for rel in incoming:
        if rel["from_id"] != keep_id:
            store.add_relation(rel["from_id"], keep_id, rel["type"],
                               scope=rel["scope"], context=rel["context"])

    # Log the merge
    remove_entity = store.get_entity(remove_id)
    remove_name = remove_entity["name"] if remove_entity else f"id={remove_id}"
    store.add_log(keep_id, f"Merged with '{remove_name}' (id={remove_id})")

    # Delete the old entity (cascades via FK)
    store.delete_entity(remove_id)
    store.commit()
