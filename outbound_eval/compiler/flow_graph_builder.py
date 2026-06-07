from __future__ import annotations

from outbound_eval.domain.ids import semantic_id, slugify
from outbound_eval.domain.schemas_task import BranchRule, FlowEdge, FlowNode, RequirementItem


BRANCH_HINTS = ("如果", "若", "当", "拒绝", "不同意", "无法", "看不到", "开车", "忙")


def build_flow_graph(flow_steps: list[str], requirements: list[RequirementItem]) -> tuple[list[FlowNode], list[FlowEdge], list[BranchRule]]:
    flow_requirements = [req for req in requirements if req.category == "flow"]
    nodes: list[FlowNode] = []
    edges: list[FlowEdge] = []
    branches: list[BranchRule] = []
    for index, step in enumerate(flow_steps):
        req_id = flow_requirements[index].id if index < len(flow_requirements) else None
        node_id = f"flow.{index + 1}.{slugify(step, 'step')}"
        nodes.append(
            FlowNode(
                id=node_id,
                name=step[:40],
                instruction=step,
                requirement_ids=[req_id] if req_id else [],
                order=index + 1,
            )
        )
        if index > 0:
            edges.append(
                FlowEdge(
                    id=f"edge.{nodes[index - 1].id}.to.{node_id}",
                    source_node_id=nodes[index - 1].id,
                    target_node_id=node_id,
                    condition="next",
                )
            )
        if any(hint in step for hint in BRANCH_HINTS):
            branches.append(
                BranchRule(
                    id=semantic_id("branch", "flow", step),
                    name=step[:40],
                    condition=step,
                    expected_target_node_id=node_id,
                    requirement_id=req_id,
                    source_text=step,
                )
            )
    return nodes, edges, branches

