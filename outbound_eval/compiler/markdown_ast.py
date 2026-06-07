"""MarkdownAstParser — 把任意半结构化 Markdown 解析成层级 AST。

参考 OpenSpec markdown-parser.ts 的 stack-based section builder：
- 先构建 code fence mask，避免误判 code block 内的标题
- 用 heading 层级 stack 构建 children
- 保留 start_line / end_line / raw_text / bullets
- 无标题时生成虚拟 root

禁止出现任何业务词（骑手、飞毛腿、直播等）。
"""
from __future__ import annotations

import re
import uuid
from typing import Iterator

from outbound_eval.domain.schemas_markdown import (
    MarkdownAst,
    MarkdownBullet,
    MarkdownNode,
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)")
_ORDERED_BULLET_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)")
_FENCE_START_RE = re.compile(r"^```|^~~~")


def _normalize(heading: str) -> str:
    """Lower-case + strip punctuation."""
    return re.sub(r"[^\w\s]", "", heading).strip().lower()


def _short_id(prefix: str, seq: int) -> str:
    return f"{prefix}_{seq:04d}"


class MarkdownAstParser:
    """Parse a Markdown string into a MarkdownAst tree."""

    def parse(self, source_text: str) -> MarkdownAst:
        lines = source_text.splitlines()
        mask = self._build_fence_mask(lines)
        nodes_flat: list[MarkdownNode] = []
        warnings: list[str] = []

        # Stack for building children: [(level, node)]
        stack: list[tuple[int, MarkdownNode]] = []
        seq = 0
        current_body_lines: list[str] = []
        current_node: MarkdownNode | None = None

        def _flush_body():
            nonlocal current_node
            if current_node is not None:
                body = "\n".join(current_body_lines).strip()
                bullets = _extract_bullets(current_body_lines, current_node.start_line + 1)
                object.__setattr__(current_node, "body", body)
                object.__setattr__(current_node, "bullets", bullets)
                # end_line = last non-empty body line index, or start_line if empty
                end = current_node.start_line
                for i, ln in enumerate(current_body_lines):
                    if ln.strip():
                        end = current_node.start_line + 1 + i
                object.__setattr__(current_node, "end_line", end)
                raw = current_node.raw_text + "\n" + "\n".join(current_body_lines)
                object.__setattr__(current_node, "raw_text", raw.strip())

        for line_idx, line in enumerate(lines):
            # Skip masked (fenced) lines for heading detection
            if mask[line_idx]:
                if current_node is not None:
                    current_body_lines.append(line)
                continue

            m = _HEADING_RE.match(line)
            if m:
                _flush_body()
                current_body_lines = []
                level = len(m.group(1))
                heading = m.group(2).strip()
                seq += 1
                node_id = _short_id("node", seq)
                # Build path from stack
                path = [s[1].heading for s in stack if s[0] < level]
                path.append(heading)

                node = MarkdownNode(
                    id=node_id,
                    heading=heading,
                    normalized_heading=_normalize(heading),
                    level=level,
                    path=path,
                    body="",
                    bullets=[],
                    children=[],
                    start_line=line_idx,
                    end_line=line_idx,
                    raw_text=line,
                )
                # Attach to parent
                while stack and stack[-1][0] >= level:
                    stack.pop()
                if stack:
                    parent = stack[-1][1]
                    # Pydantic models are immutable-ish; use list directly
                    parent.children.append(node)
                else:
                    nodes_flat.insert(0, node)  # will be reset below

                stack.append((level, node))
                nodes_flat.append(node)
                current_node = node
            else:
                if current_node is not None:
                    current_body_lines.append(line)

        _flush_body()

        # Build root-level nodes (level 1, or all if no level-1)
        top_level = [n for n in nodes_flat if not any(n in p[1].children for p in stack if p[1] is not n)]
        # Better: collect nodes that are direct children of no other
        all_child_ids = set()
        for n in nodes_flat:
            for c in n.children:
                all_child_ids.add(id(c))
        roots = [n for n in nodes_flat if id(n) not in all_child_ids]

        if not roots:
            warnings.append("No headings found; creating virtual root.")
            root = MarkdownNode(
                id="node_root",
                heading="Task Instruction",
                normalized_heading="task instruction",
                level=0,
                path=["Task Instruction"],
                body=source_text.strip(),
                bullets=_extract_bullets(lines, 0),
                children=[],
                start_line=0,
                end_line=max(0, len(lines) - 1),
                raw_text=source_text,
            )
            return MarkdownAst(root=root, nodes=[root], source_text=source_text, parse_warnings=warnings)

        # Create a virtual root that holds all top-level nodes
        root = MarkdownNode(
            id="node_root",
            heading="Task Instruction",
            normalized_heading="task instruction",
            level=0,
            path=[],
            body="",
            bullets=[],
            children=roots,
            start_line=0,
            end_line=max((n.end_line for n in nodes_flat), default=0),
            raw_text="",
        )
        return MarkdownAst(root=root, nodes=nodes_flat, source_text=source_text, parse_warnings=warnings)

    def flatten(self, ast: MarkdownAst) -> list[MarkdownNode]:
        """Return all nodes in DFS order."""
        result: list[MarkdownNode] = []
        stack = list(ast.root.children) if ast.root.children else [ast.root]
        if ast.root.id == "node_root" and ast.root.children:
            stack = list(ast.root.children)
        elif not ast.root.children:
            return [ast.root]
        while stack:
            node = stack.pop(0)
            result.append(node)
            stack = list(node.children) + stack
        return result


def _extract_bullets(lines: list[str], start_line: int) -> list[MarkdownBullet]:
    bullets = []
    seq = 0
    for i, line in enumerate(lines):
        mb = _BULLET_RE.match(line)
        mo = _ORDERED_BULLET_RE.match(line)
        if mb or mo:
            m = mb or mo
            indent = len(m.group(1))
            text = m.group(2).strip()
            seq += 1
            bullets.append(
                MarkdownBullet(
                    id=f"bullet_{start_line + i:04d}_{seq:02d}",
                    text=text,
                    indent=indent,
                    ordered=bool(mo),
                    line_no=start_line + i,
                )
            )
    return bullets
