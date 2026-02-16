"""Schema Layer — frame definitions with typed slots."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SlotDef:
    name: str
    slot_type: str = "str"        # str, ref, list, int, float, bool, text
    cascade: str = "none"          # none, archive_related, clear
    required: bool = False
    default: str | None = None


@dataclass(frozen=True)
class FrameSchema:
    name: str
    slots: tuple[SlotDef, ...] = ()
    description: str = ""


BUILTIN_SCHEMAS: dict[str, FrameSchema] = {
    "person": FrameSchema(
        name="person",
        description="A person (team member, client contact, etc.)",
        slots=(
            SlotDef("name", required=True),
            SlotDef("email"),
            SlotDef("phone"),
            SlotDef("role"),
            SlotDef("company", slot_type="ref", cascade="archive_related"),
            SlotDef("language"),
            SlotDef("timezone"),
            SlotDef("notes", slot_type="text"),
        ),
    ),
    "client": FrameSchema(
        name="client",
        description="A client company/organization",
        slots=(
            SlotDef("name", required=True),
            SlotDef("status", default="active"),
            SlotDef("description"),
            SlotDef("notes", slot_type="text"),
        ),
    ),
    "project": FrameSchema(
        name="project",
        description="A project within a client or organization",
        slots=(
            SlotDef("name", required=True),
            SlotDef("client", slot_type="ref"),
            SlotDef("status", default="active"),
            SlotDef("team", slot_type="list"),
            SlotDef("notes", slot_type="text"),
        ),
    ),
    "agency": FrameSchema(
        name="agency",
        description="An agency or organization managing clients",
        slots=(
            SlotDef("name", required=True),
            SlotDef("owner", slot_type="ref"),
            SlotDef("clients", slot_type="list"),
            SlotDef("notes", slot_type="text"),
        ),
    ),
    "decision": FrameSchema(
        name="decision",
        description="A technical or business decision",
        slots=(
            SlotDef("name", required=True),
            SlotDef("status", default="planned"),
            SlotDef("tags", slot_type="list"),
        ),
    ),
}
