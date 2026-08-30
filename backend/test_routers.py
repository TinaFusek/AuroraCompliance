"""
Regression tests for Aurora's two routers.

These guard *behaviour*, not syntax: given a well-formed LLM reply the router
must return the mode it was told, and given garbage it must fall back — for
the stated reason, not because an unrelated error got swallowed by a broad
`except`.

Written after this bug: route_trace_question() referenced `_json`, a name
that only existed inside route(). Every trace question raised NameError,
`except Exception` ate it, and the router silently degraded to AGGREGATE.
Ruff F821 catches that specific typo; these tests catch the whole class of
"router quietly stopped routing".

Run from backend/:  pytest test_routers.py -v
CI needs ANTHROPIC_API_KEY set to any non-empty dummy value: the client is
constructed at import time, but no test here ever calls the API.
"""

from dataclasses import dataclass

import pytest

import main


@dataclass
class _Block:
    text: str


@dataclass
class _Msg:
    content: list


class _FakeLLM:
    """Stands in for anthropic.Anthropic() and returns one canned reply."""

    def __init__(self, reply: str):
        self.reply = reply
        self.messages = self  # so llm.messages.create(...) resolves here

    def create(self, **_kwargs):
        return _Msg(content=[_Block(text=self.reply)])


@pytest.fixture
def fake_llm(monkeypatch):
    def _install(reply: str):
        monkeypatch.setattr(main, "llm", _FakeLLM(reply))

    return _install


# ------------------------------------------------------- trace router
def test_trace_router_returns_hotspot(fake_llm):
    fake_llm('{"mode": "HOTSPOT", "keyword": ""}')
    q = "Which articles does the agent rely on most?"
    assert main.route_trace_question(q) == ("HOTSPOT", "")


def test_trace_router_returns_empty_only(fake_llm):
    fake_llm('{"mode": "EMPTY_ONLY", "keyword": ""}')
    q = "What do the empty-context runs have in common?"
    assert main.route_trace_question(q) == ("EMPTY_ONLY", "")


def test_trace_router_strips_code_fences(fake_llm):
    fake_llm('```json\n{"mode": "SPECIFIC", "keyword": "deployer"}\n```')
    assert main.route_trace_question("...") == ("SPECIFIC", "deployer")


def test_trace_router_falls_back_on_bad_json(fake_llm):
    fake_llm("I think this is a HOTSPOT question!")
    assert main.route_trace_question("...") == ("AGGREGATE", "")


def test_trace_router_falls_back_on_unknown_mode(fake_llm):
    fake_llm('{"mode": "SUMMARY", "keyword": "x"}')
    mode, _ = main.route_trace_question("...")
    assert mode == "AGGREGATE"


def test_trace_router_survives_json_that_is_not_an_object(fake_llm):
    # Valid JSON, wrong shape: .get() then raises AttributeError. This is why
    # AttributeError has to stay in the except tuple after narrowing it.
    fake_llm('["HOTSPOT"]')
    assert main.route_trace_question("...") == ("AGGREGATE", "")


# --------------------------------------------------------- law router
def test_law_router_returns_strategy_and_translation(fake_llm):
    fake_llm('{"strategy": "TRAVERSAL", "query_en": "What must a deployer do?"}')
    assert main.route("Čo musí robiť deployer?") == ("TRAVERSAL", "What must a deployer do?")


def test_law_router_keeps_question_when_translation_missing(fake_llm):
    fake_llm('{"strategy": "VECTOR"}')
    assert main.route("What is an AI system?") == ("VECTOR", "What is an AI system?")


def test_law_router_falls_back_on_bad_json(fake_llm):
    fake_llm("TRAVERSAL")
    assert main.route("...") == ("HYBRID", "...")


def test_law_router_survives_json_that_is_not_an_object(fake_llm):
    # FAILS until route()'s except also catches AttributeError — currently it
    # only catches JSONDecodeError, so this shape returns a 500 from /ask.
    fake_llm('["TRAVERSAL"]')
    assert main.route("...") == ("HYBRID", "...")
