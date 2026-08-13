# -*- coding: utf-8 -*-
"""M1/S2: LangGraph 流程游标图（plan→policy→审批暂停→执行→观察→验证→决策）。

设计要点：
- LangGraph 只是流程游标：权威状态永远在 MySQL 的 Run/Step 表里，
  checkpoint 永不覆盖权威状态。
- State 是紧凑游标：只含 ID、阶段、循环计数与短摘要；凭据、完整
  命令、原始日志与完整提示词禁止进图。
- approval_pause 是唯一中断点，节点体本身无任何副作用：resume 从
  节点开头重新执行，带副作用就会重复执行。
- graph_version 注册表保证暂停中的旧 Run 按其落库版本重建，绝不
  跳进新版本节点。
- 节点实现函数经 handlers 注入（执行器切片提供真实实现，测试用
  替身）；本模块只负责拓扑、暂停/恢复契约与版本选择。
"""
from typing import Any, Callable, Dict, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

DEFAULT_GRAPH_VERSION = 'v1'

# roadmap 固定流程顺序；approval_pause 是唯一中断点。
NODE_SEQUENCE = (
    'plan', 'policy', 'approval_pause',
    'execute', 'observe', 'verify', 'decide',
)


class AutonomyGraphError(Exception):
    """未知图版本或缺少节点 handler。"""


class GraphCursor(TypedDict, total=False):
    """紧凑流程游标：进 checkpoint 的只有这些字段。

    禁止包含凭据、完整命令、原始日志、目标文本或完整提示词。
    ``goal`` 只为读取未发布 v1 checkpoint 保留；新 Driver 不写入。
    """

    run_id: str
    graph_version: str
    owner: str
    goal: str
    phase: str
    loops: int
    proposed_steps: int
    pending_step_id: str
    decision: str
    done: bool
    summary: str


def _wrap(name: str, handler: Callable) -> Callable:
    def node(state: GraphCursor) -> Dict[str, Any]:
        update = dict(handler(state) or {})
        update['phase'] = name
        return update
    return node


def _approval_pause(state: GraphCursor) -> Dict[str, Any]:
    """唯一中断点：节点体只读，resume 从节点开头重新执行。"""
    decision = interrupt({
        'run_id': state.get('run_id', ''),
        'pending_step_id': state.get('pending_step_id', ''),
        'loops': int(state.get('loops', 0)),
    })
    return {
        'phase': 'approval_pause',
        'decision': str(decision),
    }


def _route_after_decide(state: GraphCursor) -> str:
    if state.get('done'):
        return END
    return 'plan'


def _build_v1(handlers: Dict[str, Callable]) -> StateGraph:
    # approval_pause 是内置唯一中断点，不由调用方提供 handler。
    required = [name for name in NODE_SEQUENCE if name != 'approval_pause']
    missing = [name for name in required if name not in handlers]
    if missing:
        raise AutonomyGraphError('missing handlers: %s' % (missing,))

    builder = StateGraph(GraphCursor)
    for name in NODE_SEQUENCE:
        if name == 'approval_pause':
            builder.add_node(name, _approval_pause)
        elif name == 'decide':
            decide_handler = handlers[name]

            def decide_node(state, _handler=decide_handler):
                update = dict(_handler(state) or {})
                update['phase'] = 'decide'
                update['loops'] = int(state.get('loops', 0)) + 1
                return update

            builder.add_node(name, decide_node)
        else:
            builder.add_node(name, _wrap(name, handlers[name]))

    builder.add_edge(START, 'plan')
    builder.add_edge('plan', 'policy')
    builder.add_edge('policy', 'approval_pause')
    builder.add_edge('approval_pause', 'execute')
    builder.add_edge('execute', 'observe')
    builder.add_edge('observe', 'verify')
    builder.add_edge('verify', 'decide')
    builder.add_conditional_edges(
        'decide', _route_after_decide, {'plan': 'plan', END: END},
    )
    return builder


# 暂停中的 Run 只能用它落库的 graph_version 重建；新版本必须以新
# 键注册，不得改写既有版本的拓扑。
_GRAPH_BUILDERS: Dict[str, Callable] = {
    DEFAULT_GRAPH_VERSION: _build_v1,
}


def list_graph_versions():
    return sorted(_GRAPH_BUILDERS.keys())


def build_graph(version: str, handlers: Dict[str, Callable]) -> StateGraph:
    """按 Run 落库的 graph_version 构建未编译图。

    调用方（worker/恢复切片）负责用 saver 编译；未知版本抛错，
    绝不默认降级到新版本。
    """
    builder = _GRAPH_BUILDERS.get(str(version or ''))
    if builder is None:
        raise AutonomyGraphError('unknown graph version: %r' % (version,))
    return builder(handlers)
