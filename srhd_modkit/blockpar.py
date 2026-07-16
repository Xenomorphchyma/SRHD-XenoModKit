from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .textio import DecodedText, read_text


class BlockParError(ValueError):
    pass


@dataclass
class BlockParParameter:
    key: str
    value: str
    indent: str = ""
    original_line: str | None = None
    modified: bool = False

    def render(self) -> str:
        if self.original_line is not None and not self.modified:
            return self.original_line
        return f"{self.indent}{self.key}={self.value}"

    def as_dict(self) -> dict[str, str]:
        return {"type": "parameter", "key": self.key, "value": self.value}


@dataclass
class BlockParRaw:
    text: str

    def render(self) -> str:
        return self.text

    def as_dict(self) -> dict[str, str]:
        return {"type": "raw", "text": self.text}


BlockParEntry = "BlockParNode | BlockParParameter | BlockParRaw"


@dataclass
class BlockParNode:
    name: str
    operator: str = "^"
    indent: str = ""
    entries: list[BlockParNode | BlockParParameter | BlockParRaw] = field(default_factory=list)
    opening_line: str | None = None
    closing_line: str | None = None

    @property
    def children(self) -> list[BlockParNode]:
        return [entry for entry in self.entries if isinstance(entry, BlockParNode)]

    @property
    def parameters(self) -> list[BlockParParameter]:
        return [entry for entry in self.entries if isinstance(entry, BlockParParameter)]

    def render_lines(self, *, include_raw: bool = True) -> list[str]:
        opening = self.opening_line if self.opening_line is not None else f"{self.indent}{self.name} {self.operator}{{"
        closing = self.closing_line if self.closing_line is not None else f"{self.indent}}}"
        lines = [opening]
        for entry in self.entries:
            if isinstance(entry, BlockParNode):
                lines.extend(entry.render_lines(include_raw=include_raw))
            elif isinstance(entry, BlockParRaw) and not include_raw:
                continue
            else:
                lines.append(entry.render())
        lines.append(closing)
        return lines

    def as_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        entries = []
        for entry in self.entries:
            if isinstance(entry, BlockParRaw) and not include_raw:
                continue
            entries.append(entry.as_dict(include_raw=include_raw) if isinstance(entry, BlockParNode) else entry.as_dict())
        return {"type": "node", "name": self.name, "operator": self.operator, "entries": entries}

    def semantic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "operator": self.operator,
            "entries": [
                entry.semantic() if isinstance(entry, BlockParNode) else {"key": entry.key, "value": entry.value}
                for entry in self.entries
                if not isinstance(entry, BlockParRaw)
            ],
        }

    def canonical_semantic(self) -> dict[str, Any]:
        """Semantic form normalized like BlockParEditor (stable sort by entry name)."""
        meaningful = [entry for entry in self.entries if not isinstance(entry, BlockParRaw)]
        meaningful.sort(
            key=lambda entry: (entry.name if isinstance(entry, BlockParNode) else entry.key).casefold()
        )
        return {
            "name": self.name,
            "operator": self.operator,
            "entries": [
                entry.canonical_semantic()
                if isinstance(entry, BlockParNode)
                else {"key": entry.key, "value": entry.value}
                for entry in meaningful
            ],
        }

    def parameters_named(self, key: str, *, case_sensitive: bool = False) -> list[BlockParParameter]:
        if case_sensitive:
            return [item for item in self.parameters if item.key == key]
        folded = key.casefold()
        return [item for item in self.parameters if item.key.casefold() == folded]

    def set_parameter(
        self,
        key: str,
        value: str,
        *,
        create: bool = False,
        all_matches: bool = False,
    ) -> int:
        matches = self.parameters_named(key)
        if not matches:
            if not create:
                raise KeyError(f"Параметр не найден: {key}")
            indent = next((item.indent for item in self.parameters), self.indent + "    ")
            self.entries.append(BlockParParameter(key=key, value=value, indent=indent, modified=True))
            return 1
        targets = matches if all_matches else matches[:1]
        for item in targets:
            item.value = value
            item.modified = True
        return len(targets)

    def delete_parameter(self, key: str, *, all_matches: bool = False) -> int:
        matches = self.parameters_named(key)
        if not matches:
            raise KeyError(f"Параметр не найден: {key}")
        targets = set(id(item) for item in (matches if all_matches else matches[:1]))
        self.entries[:] = [entry for entry in self.entries if id(entry) not in targets]
        return len(targets)


@dataclass
class BlockParDocument:
    entries: list[BlockParNode | BlockParParameter | BlockParRaw]
    encoding: str = "utf-8"
    had_bom: bool = False
    newline: str = "\r\n"
    trailing_newline: bool = True

    @property
    def roots(self) -> list[BlockParNode]:
        return [entry for entry in self.entries if isinstance(entry, BlockParNode)]

    def to_text(self, *, include_raw: bool = True) -> str:
        lines: list[str] = []
        for entry in self.entries:
            if isinstance(entry, BlockParNode):
                lines.extend(entry.render_lines(include_raw=include_raw))
            elif isinstance(entry, BlockParRaw) and not include_raw:
                continue
            else:
                lines.append(entry.render())
        result = self.newline.join(lines)
        return result + self.newline if self.trailing_newline else result

    def save(
        self,
        path: str | Path,
        *,
        encoding: str | None = None,
        include_raw: bool = True,
        bom: bool | None = None,
    ) -> Path:
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_text(include_raw=include_raw)
        selected = encoding or self.encoding
        normalized = selected.casefold().replace("_", "-")
        errors = "surrogateescape" if normalized == "utf-8-surrogateescape" or normalized == "utf-8" else "strict"
        if normalized == "utf-8-surrogateescape":
            normalized = "utf-8"
        if bom is None:
            bom = self.had_bom if encoding is None else normalized in {"utf-16-le", "utf-16-be", "utf-8-sig"}
        prefix = b""
        codec = normalized
        if normalized == "utf-16-le" and bom:
            prefix = b"\xff\xfe"
        elif normalized == "utf-16-be" and bom:
            prefix = b"\xfe\xff"
        elif normalized == "utf-8-sig":
            codec = "utf-8"
            if bom:
                prefix = b"\xef\xbb\xbf"
        path.write_bytes(prefix + text.encode(codec, errors=errors))
        return path

    def as_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        entries = []
        for entry in self.entries:
            if isinstance(entry, BlockParRaw) and not include_raw:
                continue
            entries.append(entry.as_dict(include_raw=include_raw) if isinstance(entry, BlockParNode) else entry.as_dict())
        return {"encoding": self.encoding, "entries": entries}

    def semantic(self) -> list[dict[str, Any]]:
        return [
            entry.semantic() if isinstance(entry, BlockParNode) else {"key": entry.key, "value": entry.value}
            for entry in self.entries
            if not isinstance(entry, BlockParRaw)
        ]

    def canonical_semantic(self) -> list[dict[str, Any]]:
        """Order-normalized tree used for codec roundtrip verification.

        BlockParEditor sorts differently named entries while preserving the order
        of equal names. The game addresses them by name; this form detects losses
        and value changes without rejecting that documented codec normalization.
        """
        meaningful = [entry for entry in self.entries if not isinstance(entry, BlockParRaw)]
        meaningful.sort(
            key=lambda entry: (entry.name if isinstance(entry, BlockParNode) else entry.key).casefold()
        )
        return [
            entry.canonical_semantic()
            if isinstance(entry, BlockParNode)
            else {"key": entry.key, "value": entry.value}
            for entry in meaningful
        ]

    def find_node(self, path: str) -> BlockParNode:
        """Find a node by slash path. Duplicate names use one-based Name[2]."""
        parts = [part.strip() for part in path.replace("\\", "/").split("/") if part.strip()]
        if not parts:
            raise KeyError("Путь узла пуст")
        candidates = self.roots
        current: BlockParNode | None = None
        for part in parts:
            match = re.fullmatch(r"(.*?)(?:\[(\d+)\])?", part)
            assert match is not None
            name = match.group(1)
            occurrence = int(match.group(2) or "1")
            if occurrence < 1:
                raise KeyError("Номер повторения начинается с 1")
            matches = [node for node in candidates if node.name.casefold() == name.casefold()]
            if len(matches) < occurrence:
                raise KeyError(f"Узел не найден: {part} в {path}")
            current = matches[occurrence - 1]
            candidates = current.children
        assert current is not None
        return current

    def add_node(self, parent_path: str, name: str, *, operator: str = "^") -> BlockParNode:
        if operator not in {"^", "~"}:
            raise ValueError("Оператор блока должен быть ^ или ~")
        if not name.strip():
            raise ValueError("Имя блока пусто")
        parent = self.find_node(parent_path)
        node = BlockParNode(name=name, operator=operator, indent=parent.indent + "    ")
        parent.entries.append(node)
        return node

    def ensure_node(self, path: str, *, operator: str = "^") -> BlockParNode:
        """Return a path, creating only its missing nodes."""
        if operator not in {"^", "~"}:
            raise ValueError("Оператор блока должен быть ^ или ~")
        parts = [part.strip() for part in path.replace("\\", "/").split("/") if part.strip()]
        if not parts:
            raise ValueError("Путь узла пуст")
        current: BlockParNode | None = None
        candidates = self.roots
        for index, part in enumerate(parts):
            if "[" in part or "]" in part:
                raise ValueError("ensure_node не создаёт пути с индексами повторов")
            match = next((node for node in candidates if node.name.casefold() == part.casefold()), None)
            if match is None:
                indent = "" if current is None else current.indent + "    "
                match = BlockParNode(name=part, operator=operator, indent=indent)
                if current is None:
                    self.entries.append(match)
                else:
                    current.entries.append(match)
            current = match
            candidates = current.children
        assert current is not None
        return current

    def delete_node(self, path: str) -> None:
        target = self.find_node(path)

        def remove(entries: list[BlockParNode | BlockParParameter | BlockParRaw]) -> bool:
            for index, entry in enumerate(entries):
                if entry is target:
                    del entries[index]
                    return True
                if isinstance(entry, BlockParNode) and remove(entry.entries):
                    return True
            return False

        if not remove(self.entries):
            raise KeyError(path)

    def apply_operations(self, operations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise ValueError(f"Операция #{index + 1} должна быть JSON-объектом")
            kind = operation.get("op")
            if kind == "set":
                node = self.find_node(str(operation["node"]))
                count = node.set_parameter(
                    str(operation["key"]),
                    str(operation["value"]),
                    create=bool(operation.get("create", False)),
                    all_matches=bool(operation.get("all", False)),
                )
                results.append({"op": kind, "changed": count})
            elif kind == "delete-parameter":
                node = self.find_node(str(operation["node"]))
                count = node.delete_parameter(str(operation["key"]), all_matches=bool(operation.get("all", False)))
                results.append({"op": kind, "changed": count})
            elif kind == "add-node":
                node = self.add_node(str(operation["parent"]), str(operation["name"]), operator=str(operation.get("operator", "^")))
                results.append({"op": kind, "name": node.name})
            elif kind == "delete-node":
                self.delete_node(str(operation["node"]))
                results.append({"op": kind, "changed": 1})
            else:
                raise ValueError(f"Неизвестная операция #{index + 1}: {kind}")
        return results


_OPEN_RE = re.compile(r"^(?P<indent>\s*)(?P<name>.*?)\s+(?P<operator>[\^~])\{\s*$")
_CLOSE_RE = re.compile(r"^\s*}\s*$")


def parse_blockpar(text: str, *, encoding: str = "utf-8", had_bom: bool = False) -> BlockParDocument:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith(("\r", "\n"))
    lines = text.splitlines()
    root_entries: list[BlockParNode | BlockParParameter | BlockParRaw] = []
    stack: list[BlockParNode] = []

    for number, line in enumerate(lines, start=1):
        opening = _OPEN_RE.match(line)
        if opening:
            name = opening.group("name").rstrip()
            if not name:
                raise BlockParError(f"Пустое имя блока, строка {number}")
            node = BlockParNode(
                name=name,
                operator=opening.group("operator"),
                indent=opening.group("indent"),
                opening_line=line,
            )
            target = stack[-1].entries if stack else root_entries
            target.append(node)
            stack.append(node)
            continue
        if _CLOSE_RE.match(line):
            if not stack:
                raise BlockParError(f"Лишняя закрывающая скобка, строка {number}")
            stack.pop().closing_line = line
            continue

        target = stack[-1].entries if stack else root_entries
        stripped = line.lstrip()
        if "=" in stripped and not stripped.startswith(("//", "/*", "*")):
            indent = line[: len(line) - len(stripped)]
            key, value = stripped.split("=", 1)
            target.append(
                BlockParParameter(
                    key=key.rstrip(),
                    value=value,
                    indent=indent,
                    original_line=line,
                )
            )
        else:
            target.append(BlockParRaw(line))

    if stack:
        path = "/".join(node.name for node in stack)
        raise BlockParError(f"Блок не закрыт: {path}")
    if not any(isinstance(item, BlockParNode) for item in root_entries):
        raise BlockParError("В документе нет блоков ^{ или ~{")
    return BlockParDocument(
        entries=root_entries,
        encoding=encoding,
        had_bom=had_bom,
        newline=newline,
        trailing_newline=trailing_newline,
    )


def load_blockpar(path: str | Path) -> BlockParDocument:
    decoded: DecodedText = read_text(path)
    return parse_blockpar(decoded.text, encoding=decoded.encoding, had_bom=decoded.had_bom)
