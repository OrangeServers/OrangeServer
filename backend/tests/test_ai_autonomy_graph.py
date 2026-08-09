# -*- coding: utf-8 -*-
"""M1/S2: LangGraph 流程游标图契约测试（Issue #13）。

MemorySaver 验证拓扑与暂停/恢复语义；真实 Redis 8 上的
ShallowRedisSaver 中断/恢复/重启恢复已由 WP0 门槛脚本验证。
"""
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.ai.autonomy import graph as graph_mod
from app.ai.autonomy.graph import (
    DEFAULT_GRAPH_VERSION,
    NODE_SEQUENCE,
    AutonomyGraphError,
    GraphCursor,
    build_graph,
    list_graph_versions,
)


def make_handlers(trace, decide_done=None):
    """记录调用顺序的替身 handlers；decide_done 控制每轮是否结束。"""
    done_flags = list(decide_done if decide_done is not None else [True])

    def handler(name):
        def fn(state):
            trace.append(name)
            return {}
        return fn

    handlers = {name: handler(name) for name in NODE_SEQUENCE}

    def decide(state):
        trace.append('decide')
        flag = done_flags.pop(0) if done_flags else True
        return {'done': flag}

    handlers['decide'] = decide
    return handlers


def compile_v1(handlers):
    return build_graph(
        DEFAULT_GRAPH_VERSION, handlers,
    ).compile(checkpointer=MemorySaver())


def thread(run_id='run-graph-1'):
    return {'configurable': {'thread_id': run_id}}


def initial_input(run_id='run-graph-1', pending_step_id='step-7'):
    return {
        'run_id': run_id,
        'graph_version': DEFAULT_GRAPH_VERSION,
        'loops': 0,
        'pending_step_id': pending_step_id,
    }


# ---------------------------------------------------------------------------
# 拓扑与紧凑 State 契约
# ---------------------------------------------------------------------------

def test_node_sequence_matches_roadmap_flow():
    assert NODE_SEQUENCE == (
        'plan', 'policy', 'approval_pause',
        'execute', 'observe', 'verify', 'decide',
    )


def test_cursor_state_is_compact():
    """进 checkpoint 的字段是固定白名单：无凭据/命令/日志/提示词。"""
    assert set(GraphCursor.__annotations__.keys()) == {
        'run_id', 'graph_version', 'phase', 'loops',
        'pending_step_id', 'decision', 'done', 'summary',
    }


def test_build_graph_rejects_unknown_version():
    handlers = make_handlers([])
    with pytest.raises(AutonomyGraphError):
        build_graph('v2', handlers)
    with pytest.raises(AutonomyGraphError):
        build_graph('', handlers)
    assert list_graph_versions() == ['v1']


def test_build_graph_rejects_missing_handlers():
    with pytest.raises(AutonomyGraphError):
        build_graph(DEFAULT_GRAPH_VERSION, {})


# ---------------------------------------------------------------------------
# 中断 / 恢复语义
# ---------------------------------------------------------------------------

def test_graph_pauses_at_approval_after_plan_and_policy():
    trace = []
    compiled = compile_v1(make_handlers(trace))
    result = compiled.invoke(initial_input(), thread())

    assert '__interrupt__' in result
    payload = result['__interrupt__'][0].value
    assert payload['pending_step_id'] == 'step-7'
    assert payload['run_id'] == 'run-graph-1'
    # 中断前只走过 plan/policy；执行侧节点一律未触碰。
    assert trace == ['plan', 'policy']
    assert result['phase'] == 'policy'
    assert 'decision' not in result


def test_resume_continues_to_end_with_decision():
    trace = []
    compiled = compile_v1(make_handlers(trace))
    compiled.invoke(initial_input(), thread())
    final = compiled.invoke(Command(resume='approve'), thread())

    assert '__interrupt__' not in final
    assert final['decision'] == 'approve'
    assert final['phase'] == 'decide'
    assert final['loops'] == 1
    assert final['done'] is True
    assert trace == ['plan', 'policy', 'execute', 'observe', 'verify', 'decide']


def test_interrupt_node_reexecutes_from_node_start_on_resume():
    """resume 时 approval_pause 从节点开头重新执行（interrupt 被调两次），
    因此该节点体必须无副作用；暂停前的节点不重复执行。"""
    trace = []
    calls = []
    original_interrupt = graph_mod.interrupt

    def counting_interrupt(payload):
        calls.append(payload)
        return original_interrupt(payload)

    compiled = compile_v1(make_handlers(trace))
    graph_mod.interrupt = counting_interrupt
    try:
        compiled.invoke(initial_input(), thread())
        compiled.invoke(Command(resume='reject'), thread())
    finally:
        graph_mod.interrupt = original_interrupt

    assert len(calls) == 2
    assert trace.count('plan') == 1
    assert trace.count('policy') == 1


def test_decide_loops_back_until_done():
    """decide 未结束则回到 plan 再来一轮，每轮都在审批点暂停。"""
    trace = []
    compiled = compile_v1(make_handlers(trace, decide_done=[False, True]))

    compiled.invoke(initial_input(), thread())
    mid = compiled.invoke(Command(resume='approve'), thread())
    assert '__interrupt__' in mid  # 第二轮再次停在审批点

    final = compiled.invoke(Command(resume='approve'), thread())
    assert '__interrupt__' not in final
    assert final['loops'] == 2
    assert final['done'] is True
    assert trace.count('plan') == 2
    assert trace.count('decide') == 2


def test_separate_threads_do_not_share_state():
    trace = []
    handlers = make_handlers(trace)
    compiled = build_graph(
        DEFAULT_GRAPH_VERSION, handlers,
    ).compile(checkpointer=MemorySaver())

    a = compiled.invoke(initial_input(run_id='run-a'), thread('run-a'))
    b = compiled.invoke(initial_input(run_id='run-b'), thread('run-b'))
    assert a['__interrupt__'][0].value['run_id'] == 'run-a'
    assert b['__interrupt__'][0].value['run_id'] == 'run-b'
