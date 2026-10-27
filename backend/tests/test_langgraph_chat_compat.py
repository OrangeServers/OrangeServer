"""Isolated compatibility probes; this graph is not the production chat runner."""

import json
import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import convert_to_openai_messages
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.config import get_stream_writer

from app.ai.runner import AgentRunner, sse_event
from app.ai.tools import ToolNotAllowed, ToolRegistry


def test_standard_openai_conversion_drops_deepseek_reasoning():
    message = AIMessage(
        content="", additional_kwargs={"reasoning_content": "required on next call"},
        tool_calls=[{"name": "inspect_asset", "args": {}, "id": "call-1",
                     "type": "tool_call"}],
    )
    wire = convert_to_openai_messages([message])[0]
    assert "reasoning_content" not in wire
    assert wire["tool_calls"][0]["id"] == "call-1"


def test_existing_provider_wire_can_preserve_reasoning_with_explicit_mapping():
    message = AIMessage(
        content="", additional_kwargs={"reasoning_content": "required on next call"},
        tool_calls=[{"name": "inspect_asset", "args": {}, "id": "call-1",
                     "type": "tool_call"}],
    )
    wire = AgentRunner._provider_messages({"summary": "", "state": {}, "messages": [{
        "role": "assistant", "content": message.content,
        "reasoning_content": message.additional_kwargs["reasoning_content"],
        "tool_calls": [{"id": call["id"], "type": "function", "function": {
            "name": call["name"], "arguments": json.dumps(call["args"]),
        }} for call in message.tool_calls],
    }]})
    assert wire[-1]["reasoning_content"] == "required on next call"
    assert wire[-1]["tool_calls"][0]["id"] == "call-1"


def test_graph_custom_events_can_use_existing_sse_envelope():
    def node(_state):
        writer = get_stream_writer()
        writer(sse_event("assistant.delta", run_id="run-1", content="part"))
        writer(sse_event("run.completed", run_id="run-1", conversation_id="c-1"))
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("emit", node)
    graph.add_edge(START, "emit")
    graph.add_edge("emit", END)
    events = list(graph.compile().stream({"messages": []}, stream_mode="custom"))
    assert [event.splitlines()[0] for event in events] == [
        "event: assistant.delta", "event: run.completed"
    ]


def test_tool_node_preserves_result_pairing_and_reasoning():
    """Exercise prebuilt orchestration without replacing the provider adapter."""
    seen = []

    def inspect_asset(asset_id: int) -> str:
        seen.append(asset_id)
        return json.dumps({"asset_id": asset_id, "status": "ok"})

    tool = StructuredTool.from_function(
        inspect_asset, name="inspect_asset", description="Read one asset."
    )

    def model(state):
        if len(state["messages"]) == 1:
            return {"messages": [AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "inspect first"},
                tool_calls=[{"name": "inspect_asset", "args": {"asset_id": 7},
                             "id": "call-1", "type": "tool_call"}],
            )]}
        return {"messages": [AIMessage(content="Inspection complete.")]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", model)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model", lambda state: "tools" if state["messages"][-1].tool_calls else END
    )
    graph.add_edge("tools", "model")
    result = graph.compile().invoke({"messages": [HumanMessage(content="Inspect")]})

    messages = result["messages"]
    assert seen == [7]
    assert messages[1].additional_kwargs["reasoning_content"] == "inspect first"
    assert messages[2].tool_call_id == "call-1"
    assert json.loads(messages[2].content) == {"asset_id": 7, "status": "ok"}
    assert messages[3].content == "Inspection complete."


def test_tool_node_cannot_bypass_registry_role_check():
    registry = ToolRegistry(store=None, platform=None, owner="alice", role="user",
                            conversation_id="demo")

    def search_accounts() -> str:
        return json.dumps(registry.execute("search_accounts", {}))

    tool = StructuredTool.from_function(
        search_accounts, name="search_accounts", description="Read accounts."
    )
    node = ToolNode([tool], handle_tool_errors=False)
    request = AIMessage(content="", tool_calls=[{
        "name": "search_accounts", "args": {}, "id": "call-denied", "type": "tool_call"
    }])
    graph = StateGraph(MessagesState)
    graph.add_node("tools", node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)

    with pytest.raises(ToolNotAllowed):
        graph.compile().invoke({"messages": [request]})


def test_tool_node_executes_multiple_calls_concurrently():
    barrier = threading.Barrier(2)
    threads = set()

    def inspect_asset(asset_id: int) -> str:
        threads.add(threading.get_ident())
        barrier.wait(timeout=5)
        return str(asset_id)

    tool = StructuredTool.from_function(
        inspect_asset, name="inspect_asset", description="Read one asset."
    )
    request = AIMessage(content="", tool_calls=[
        {"name": "inspect_asset", "args": {"asset_id": n}, "id": f"call-{n}",
         "type": "tool_call"} for n in (1, 2)
    ])
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    result = graph.compile().invoke({"messages": [request]})

    assert len(threads) == 2
    assert [message.tool_call_id for message in result["messages"][1:]] == ["call-1", "call-2"]


def test_tool_node_max_concurrency_one_serializes_calls():
    seen = []

    def inspect_asset(asset_id: int) -> str:
        seen.append(asset_id)
        return str(asset_id)

    tool = StructuredTool.from_function(
        inspect_asset, name="inspect_asset", description="Read one asset."
    )
    request = AIMessage(content="", tool_calls=[
        {"name": "inspect_asset", "args": {"asset_id": n}, "id": f"call-{n}",
         "type": "tool_call"} for n in (1, 2)
    ])
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    graph.compile().invoke({"messages": [request]}, config={"max_concurrency": 1})
    assert seen == [1, 2]
