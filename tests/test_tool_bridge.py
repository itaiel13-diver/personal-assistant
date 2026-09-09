"""tool_bridge: the fallback tier's hands. Schemas must mirror the real
signatures, pack selection must pull the right tools for a Hebrew message,
and dispatch must run the real functions - guards and all - or say honestly
why it could not."""

import json

import tool_bridge


def _sample(name: str, count: int, tags: list = None, flag: bool = False) -> str:
    """Does a sample thing. The docstring carries the confirmation rules."""
    return f"did {name} x{count} {tags} {flag}"


def _optional_sample(text: str, limit: int = 5) -> str:
    """Has a defaulted argument."""
    return f"{text}:{limit}"


def test_schema_types_and_required():
    schema = tool_bridge.schema_for(_sample)
    function = schema["function"]
    assert function["name"] == "_sample"
    assert "confirmation rules" in function["description"]
    props = function["parameters"]["properties"]
    assert props["name"] == {"type": "string"}
    assert props["count"] == {"type": "integer"}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["flag"] == {"type": "boolean"}
    # name and count have no default -> required; tags and flag do not.
    assert sorted(function["parameters"]["required"]) == ["count", "name"]


def test_schema_defaults_are_optional():
    schema = tool_bridge.schema_for(_optional_sample)
    assert schema["function"]["parameters"]["required"] == ["text"]


def test_select_packs_hebrew():
    assert "todo" in tool_bridge.select_packs("תוסיף משימה לרשימה")
    assert "calendar" in tool_bridge.select_packs("מה ביומן שלי מחר")
    assert "mail" in tool_bridge.select_packs("קרא את המייל האחרון")
    assert "drive" in tool_bridge.select_packs("מצא את הקובץ בדרייב")
    assert "web" in tool_bridge.select_packs("תחפש בגוגל כמה עולה דירה")
    assert "reminders" in tool_bridge.select_packs("תזכיר לי מחר בבוקר")


def test_select_packs_no_match_means_tool_less():
    assert tool_bridge.select_packs("מה שלומך היום") == []


def _named(name):
    def _fn() -> str:
        return name
    _fn.__name__ = name
    _fn.__doc__ = f"Does {name}."
    return _fn


def test_tools_for_includes_core_and_pack_only():
    registry = {"save_to_long_term_memory": _named("save_to_long_term_memory"),
                "search_web": _named("search_web"), "read_web_page": _named("read_web_page")}
    schemas = tool_bridge.tools_for(["web"], registry)
    names = [s["function"]["name"] for s in schemas]
    assert names == ["save_to_long_term_memory", "search_web", "read_web_page"]


def test_tools_for_skips_unknown_names():
    registry = {"save_to_long_term_memory": _named("save_to_long_term_memory")}
    schemas = tool_bridge.tools_for(["web"], registry)
    assert [s["function"]["name"] for s in schemas] == ["save_to_long_term_memory"]


def test_dispatch_runs_the_real_function():
    registry = {"_sample": _sample}
    result = tool_bridge.dispatch(registry, "_sample", json.dumps(
        {"name": "משימה", "count": 2, "tags": ["א"], "flag": True}))
    assert result == "did משימה x2 ['א'] True"


def test_dispatch_unknown_tool_is_an_error_string():
    assert "לא זמין" in tool_bridge.dispatch({}, "ghost", "{}")


def test_dispatch_bad_json_is_an_error_string():
    assert "לא התפרסרו" in tool_bridge.dispatch({"_sample": _sample}, "_sample", "not-json")


def test_dispatch_exception_becomes_an_error_string():
    def _explode() -> str:
        raise RuntimeError("boom")
    result = tool_bridge.dispatch({"_explode": _explode}, "_explode", "{}")
    assert result.startswith("❌") and "boom" in result
