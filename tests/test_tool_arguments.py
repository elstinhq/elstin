"""forge/agents/tool_arguments.py — repairing a model's tool-call
arguments where the tool's schema makes the intent unambiguous, then
reporting whatever is still wrong. Pure functions, no loop."""
from __future__ import annotations

from forge.agents.tool_arguments import argument_errors, repair_arguments
from forge.tools.dev_tools import TOOL_SCHEMAS


def _schema(tool: str) -> dict:
    return next(s["function"]["parameters"] for s in TOOL_SCHEMAS if s["function"]["name"] == tool)


def test_the_regressions_that_motivated_it_are_repaired():
    write = repair_arguments({"path": "a.py", "content": "x", "overwrite": "false"}, _schema("write_file_tool"))
    read = repair_arguments({"path": "a.py", "offset": "10", "limit": "200"}, _schema("read_file_tool"))

    assert write["overwrite"] is False
    assert read == {"path": "a.py", "offset": 10, "limit": 200}


def test_booleans_are_repaired_in_any_case_and_nested_items_too():
    edits = repair_arguments(
        {"edits": [{"path": "a.py", "target_content": "a", "replacement_content": "b", "allow_multiple": "False"}]},
        _schema("apply_edits_tool"),
    )

    assert edits["edits"][0]["allow_multiple"] is False


def test_a_json_encoded_array_or_object_is_decoded():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "integer"}}, "opts": {"type": "object"}},
    }

    repaired = repair_arguments({"tags": '["1", 2]', "opts": '{"a": 1}'}, schema)

    assert repaired == {"tags": [1, 2], "opts": {"a": 1}}


def test_null_or_empty_placeholders_for_optional_arguments_are_dropped():
    repaired = repair_arguments({"path": "a.py", "offset": None, "limit": {}}, _schema("read_file_tool"))

    assert repaired == {"path": "a.py"}


def test_ambiguous_or_untyped_values_are_left_alone():
    schema = {
        "type": "object",
        "properties": {"either": {"type": ["integer", "string"]}, "anything": {}, "choice": {"anyOf": [{"type": "integer"}]}},
    }
    arguments = {"either": "5", "anything": "5", "choice": "5"}

    assert repair_arguments(arguments, schema) == arguments


def test_nothing_is_invented_for_a_value_that_is_not_a_number():
    assert repair_arguments({"path": "a.py", "limit": "lots"}, _schema("read_file_tool"))["limit"] == "lots"


def test_errors_name_every_problem():
    errors = argument_errors({"limit": "lots"}, _schema("read_file_tool"))

    assert "path is required" in errors
    assert any(e.startswith("limit must be integer") for e in errors)


def test_unknown_arguments_are_errors_only_where_the_schema_forbids_them():
    closed = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}
    open_ = {"type": "object", "properties": {"a": {"type": "string"}}}

    assert argument_errors({"a": "x", "b": 1}, closed) == ["b is not an accepted argument (accepted: a)"]
    assert argument_errors({"a": "x", "b": 1}, open_) == []


def test_enum_values_are_checked():
    schema = {"type": "object", "properties": {"status": {"type": "string", "enum": ["todo", "done"]}}}

    assert argument_errors({"status": "finished"}, schema) == ['status must be one of ["todo", "done"], got string "finished"']


def test_a_valid_call_and_a_missing_schema_have_no_errors():
    assert argument_errors({"path": "a.py", "limit": 20}, _schema("read_file_tool")) == []
    assert argument_errors({"anything": 1}, None) == []
    assert repair_arguments({"anything": "1"}, None) == {"anything": "1"}


def test_every_dev_tool_schema_accepts_a_minimal_valid_call():
    """Guards the checker against a schema shape it misreads: each tool's
    required arguments, given as strings, must validate (after repair) for
    every string-typed tool argument."""
    for tool in TOOL_SCHEMAS:
        schema = tool["function"]["parameters"]
        properties = schema.get("properties", {})
        call = {
            name: "x" for name in schema.get("required", [])
            if properties.get(name, {}).get("type") == "string"
        }
        if len(call) != len(schema.get("required", [])):
            continue  # a required non-string argument; covered by the specific tests above
        assert argument_errors(repair_arguments(call, schema), schema) == [], tool["function"]["name"]
