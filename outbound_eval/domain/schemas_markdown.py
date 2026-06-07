from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MdModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarkdownBullet(MdModel):
    id: str
    text: str
    indent: int = 0
    ordered: bool = False
    line_no: int


class MarkdownNode(MdModel):
    id: str
    heading: str
    normalized_heading: str
    level: int
    path: list[str] = Field(default_factory=list)
    body: str = ""
    bullets: list[MarkdownBullet] = Field(default_factory=list)
    children: list["MarkdownNode"] = Field(default_factory=list)
    start_line: int = 0
    end_line: int = 0
    raw_text: str = ""


class MarkdownAst(MdModel):
    root: MarkdownNode
    nodes: list[MarkdownNode] = Field(default_factory=list)
    source_text: str = ""
    parse_warnings: list[str] = Field(default_factory=list)


class SourceRef(MdModel):
    source_node_id: str
    heading_path: list[str] = Field(default_factory=list)
    start_line: int = 0
    end_line: int = 0
    quote: str = ""
