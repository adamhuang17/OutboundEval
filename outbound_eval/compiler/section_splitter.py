from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict


class SectionBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    level: int
    header_line: str
    body: str


SECTION_ALIASES = {
    "role": "role",
    "persona": "role",
    "task": "task",
    "objective": "task",
    "opening line": "opening_line",
    "opening": "opening_line",
    "call flow": "call_flow",
    "conversation flow": "call_flow",
    "flow": "call_flow",
    "knowledge points": "faq",
    "knowledge points (faq)": "faq",
    "faq": "faq",
    "constraints": "constraints",
    "constraint": "constraints",
}


def normalize_heading(title: str) -> str:
    title = title.strip().strip(":").strip()
    title = re.sub(r"\s+", " ", title).lower()
    if title in SECTION_ALIASES:
        return SECTION_ALIASES[title]
    for alias, key in SECTION_ALIASES.items():
        if title.startswith(alias + ":"):
            return key
    return title.replace(" ", "_")


def split_sections(markdown: str) -> list[SectionBlock]:
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    headers: list[tuple[int, int, str, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,3})\s*(.+?)\s*$", line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            headers.append((index, level, line, title))
    blocks: list[SectionBlock] = []
    for pos, (index, level, header_line, title) in enumerate(headers):
        next_index = len(lines)
        for candidate_index, candidate_level, _, _ in headers[pos + 1 :]:
            if candidate_level <= level:
                next_index = candidate_index
                break
        body = "\n".join(lines[index + 1 : next_index]).strip()
        key = normalize_heading(title)
        inline = title.split(":", 1)
        if len(inline) == 2 and inline[1].strip() and not body:
            body = inline[1].strip()
            key = normalize_heading(inline[0])
        elif len(inline) == 2:
            key = normalize_heading(inline[0])
            if inline[1].strip():
                body = inline[1].strip() + ("\n" + body if body else "")
        blocks.append(
            SectionBlock(
                key=key,
                title=title,
                level=level,
                header_line=header_line,
                body=body,
            )
        )
    return blocks


def section_map(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    for block in split_sections(markdown):
        sections.setdefault(block.key, []).append(block.body)
    return {key: "\n\n".join(part for part in parts if part).strip() for key, parts in sections.items()}

