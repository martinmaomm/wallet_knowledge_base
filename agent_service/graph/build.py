from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from agent_service.graph.nodes import (
    GraphDependencies,
    initialize_task,
    make_nodes,
)
from agent_service.graph.state import AgentState


__all__ = ["GraphDependencies", "build_graph"]

THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
INVALID_THREAD_MESSAGE = (
    "thread_id must be a 1-128 character identifier"
)


def _validate_config(config: RunnableConfig | None) -> RunnableConfig:
    if not isinstance(config, Mapping):
        raise ValueError(INVALID_THREAD_MESSAGE)
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        raise ValueError(INVALID_THREAD_MESSAGE)
    thread_id = configurable.get("thread_id")
    if (
        not isinstance(thread_id, str)
        or THREAD_ID_PATTERN.fullmatch(thread_id) is None
    ):
        raise ValueError(INVALID_THREAD_MESSAGE)
    return config


class ValidatedCompiledGraph:
    def __init__(self, compiled_graph: Any) -> None:
        self.__compiled_graph = compiled_graph

    async def ainvoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self.__compiled_graph.ainvoke(
            input,
            config=_validate_config(config),
            **kwargs,
        )

    def get_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> Any:
        return self.__compiled_graph.get_state(
            _validate_config(config),
            subgraphs=subgraphs,
        )

    async def aget_state(
        self,
        config: RunnableConfig,
        *,
        subgraphs: bool = False,
    ) -> Any:
        return await self.__compiled_graph.aget_state(
            _validate_config(config),
            subgraphs=subgraphs,
        )

    def update_state(
        self,
        config: RunnableConfig,
        values: dict[str, Any] | Any | None,
        as_node: str | None = None,
        task_id: str | None = None,
    ) -> RunnableConfig:
        return self.__compiled_graph.update_state(
            _validate_config(config),
            values,
            as_node=as_node,
            task_id=task_id,
        )

    async def aupdate_state(
        self,
        config: RunnableConfig,
        values: dict[str, Any] | Any | None,
        as_node: str | None = None,
        task_id: str | None = None,
    ) -> RunnableConfig:
        return await self.__compiled_graph.aupdate_state(
            _validate_config(config),
            values,
            as_node=as_node,
            task_id=task_id,
        )

    def get_graph(
        self,
        config: RunnableConfig | None = None,
        *,
        xray: int | bool = False,
    ) -> Any:
        if config is not None:
            config = _validate_config(config)
        return self.__compiled_graph.get_graph(config=config, xray=xray)


def build_graph(
    deps: GraphDependencies,
    checkpointer: Any,
):
    nodes = make_nodes(deps)
    builder = StateGraph(AgentState)
    builder.add_node("initialize_task", initialize_task)
    for name, node in nodes.items():
        builder.add_node(name, node)

    builder.add_edge(START, "initialize_task")
    builder.add_edge("initialize_task", "load_sources")
    builder.add_edge("load_sources", "extract_requirements")
    builder.add_edge("extract_requirements", "analyze_risks")
    builder.add_edge("analyze_risks", "retrieve_bugs")
    builder.add_edge("retrieve_bugs", "generate_test_plan")
    builder.add_edge("generate_test_plan", "human_review")
    builder.add_edge("human_review", END)
    return ValidatedCompiledGraph(
        builder.compile(checkpointer=checkpointer)
    )
