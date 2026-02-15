from claude_memory.schema import SlotDef, FrameSchema, BUILTIN_SCHEMAS


def test_person_schema_exists():
    assert "person" in BUILTIN_SCHEMAS


def test_person_has_required_slots():
    person = BUILTIN_SCHEMAS["person"]
    slot_names = {s.name for s in person.slots}
    assert "name" in slot_names
    assert "company" in slot_names
    assert "email" in slot_names


def test_slot_def_has_type_and_cascade():
    person = BUILTIN_SCHEMAS["person"]
    company = next(s for s in person.slots if s.name == "company")
    assert company.slot_type == "ref"
    assert company.cascade == "archive_related"


def test_client_schema():
    client = BUILTIN_SCHEMAS["client"]
    slot_names = {s.name for s in client.slots}
    assert "name" in slot_names
    assert "status" in slot_names


def test_project_schema():
    project = BUILTIN_SCHEMAS["project"]
    slot_names = {s.name for s in project.slots}
    assert "name" in slot_names
    assert "client" in slot_names
    assert "status" in slot_names
