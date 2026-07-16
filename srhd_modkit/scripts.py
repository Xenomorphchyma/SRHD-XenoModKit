from __future__ import annotations

import json
import re
import struct
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


RSON_FILE_ID = 573785173
RSON_FILE_VERSION = 8
STATE_EVENTS_RE = re.compile(
    r"^\s*\[([A-Za-z_][A-Za-z0-9_]*(?:,[A-Za-z_][A-Za-z0-9_]*)*)\|((?:-?\d+)?)\](?:\r?\n|$)"
)
EVENT_NAME_RE = re.compile(r"^t_[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class ScriptIssue:
    severity: str
    code: str
    message: str
    location: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "location": self.location,
        }


@dataclass
class RsonProject:
    data: dict[str, Any]
    path: Path | None = None

    @property
    def name(self) -> str:
        return str(self.data.get("ScriptName", ""))

    def iter_objects(self) -> Iterable[dict[str, Any]]:
        groups = self.data.get("Visual.Objects", [])
        if not isinstance(groups, list):
            return
        for group in groups:
            if not isinstance(group, dict):
                continue
            for value in group.values():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, dict) and "Type" in item:
                        yield item

    def object_by_id(self, object_id: int) -> dict[str, Any]:
        for item in self.iter_objects():
            if item.get("#") == object_id:
                return item
        raise KeyError(f"Объект с #={object_id} не найден")

    def _object_container(self, object_id: int) -> tuple[list[Any], int, dict[str, Any]]:
        groups = self.data.get("Visual.Objects", [])
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for value in group.values():
                    if not isinstance(value, list):
                        continue
                    for index, item in enumerate(value):
                        if isinstance(item, dict) and item.get("#") == object_id and "Type" in item:
                            return value, index, item
        raise KeyError(f"Объект с #={object_id} не найден")

    def next_object_id(self) -> int:
        identifiers = [item.get("#") for item in self.iter_objects() if isinstance(item.get("#"), int)]
        return max(identifiers, default=-1) + 1

    def clone_object(self, object_id: int, *, name: str | None = None) -> dict[str, Any]:
        """Clone a proven object shape into the same RScript object group."""
        container, _, source = self._object_container(object_id)
        clone = deepcopy(source)
        clone["#"] = self.next_object_id()
        if name is not None:
            if not name.strip():
                raise ValueError("Имя клонированного объекта не может быть пустым")
            clone["Name"] = name
        elif isinstance(clone.get("Name"), str) and clone["Name"]:
            clone["Name"] = f"{clone['Name']} Copy"
        container.append(clone)
        return clone

    def add_link(self, begin: int, end: int, *, nom: int = 0, arrow: bool = True) -> dict[str, Any]:
        self.object_by_id(begin)
        self.object_by_id(end)
        if not isinstance(nom, int) or isinstance(nom, bool) or nom < 0:
            raise ValueError("Nom связи должен быть неотрицательным целым числом")
        links = self.data.get("Visual.Links")
        if not isinstance(links, list):
            raise ValueError("Visual.Links должен быть массивом")
        for link in links:
            if (
                isinstance(link, dict)
                and link.get("Begin") == begin
                and link.get("End") == end
                and link.get("Nom") == nom
            ):
                raise ValueError(f"Связь #{begin} -> #{end} с Nom={nom} уже существует")
        link = {"Type": "TGraphLink", "Begin": begin, "End": end, "Nom": nom, "Arrow": bool(arrow)}
        links.append(link)
        return link

    def delete_link(self, index: int) -> dict[str, Any]:
        links = self.data.get("Visual.Links")
        if not isinstance(links, list):
            raise ValueError("Visual.Links должен быть массивом")
        if index < 0 or index >= len(links):
            raise IndexError(f"Индекс связи вне диапазона 0..{len(links) - 1}: {index}")
        link = links.pop(index)
        if not isinstance(link, dict):
            raise ValueError(f"Visual.Links[{index}] не является объектом")
        return link

    def delete_object(self, object_id: int, *, detach_references: bool = False) -> dict[str, Any]:
        container, index, item = self._object_container(object_id)
        children = [
            child.get("#")
            for child in self.iter_objects()
            if child.get("#") != object_id and child.get("Parent") == object_id
        ]
        links = self.data.get("Visual.Links")
        if not isinstance(links, list):
            raise ValueError("Visual.Links должен быть массивом")
        link_indexes = [
            link_index
            for link_index, link in enumerate(links)
            if isinstance(link, dict) and object_id in (link.get("Begin"), link.get("End"))
        ]
        if (children or link_indexes) and not detach_references:
            details: list[str] = []
            if children:
                details.append("дочерние объекты " + ", ".join(f"#{value}" for value in children))
            if link_indexes:
                details.append("связи " + ", ".join(str(value) for value in link_indexes))
            raise ValueError(
                f"Объект #{object_id} используется ({'; '.join(details)}); "
                "укажите detach_references=True для безопасного отвязывания"
            )
        if detach_references:
            for child in self.iter_objects():
                if child.get("Parent") == object_id:
                    child["Parent"] = -1
            links[:] = [
                link
                for link in links
                if not (isinstance(link, dict) and object_id in (link.get("Begin"), link.get("End")))
            ]
        container.pop(index)
        return {
            "object": item,
            "detached_children": children if detach_references else [],
            "removed_links": len(link_indexes) if detach_references else 0,
        }

    def validate(self) -> list[ScriptIssue]:
        issues: list[ScriptIssue] = []
        if self.data.get("FileID") != RSON_FILE_ID:
            issues.append(ScriptIssue("error", "rson-file-id", f"Ожидался FileID {RSON_FILE_ID}"))
        if self.data.get("FileVersion") != RSON_FILE_VERSION:
            issues.append(ScriptIssue("error", "rson-version", f"Ожидалась FileVersion {RSON_FILE_VERSION}"))
        if not self.name.strip():
            issues.append(ScriptIssue("error", "rson-name", "ScriptName пуст"))
        if not isinstance(self.data.get("Visual.Objects"), list):
            issues.append(ScriptIssue("error", "rson-objects", "Visual.Objects должен быть массивом"))
        if not isinstance(self.data.get("Visual.Links"), list):
            issues.append(ScriptIssue("error", "rson-links", "Visual.Links должен быть массивом"))

        objects = list(self.iter_objects())
        identifiers: list[int] = []
        for index, item in enumerate(objects):
            location = f"object[{index}]"
            if not isinstance(item.get("#"), int):
                issues.append(ScriptIssue("error", "rson-object-id", "У объекта нет целочисленного #", location))
            else:
                identifiers.append(item["#"])
            if not isinstance(item.get("Type"), str) or not item["Type"]:
                issues.append(ScriptIssue("error", "rson-object-type", "У объекта нет Type", location))
            for field in ("Code", "ActCode", "LinkCode"):
                if field in item and (
                    not isinstance(item[field], list)
                    or not all(isinstance(line, str) for line in item[field])
                ):
                    issues.append(ScriptIssue("error", "rson-code", f"{field} должен быть массивом строк", location))
            if item.get("Type") == "TState":
                on_act_code = item.get("OnActCode", "")
                if not isinstance(on_act_code, str):
                    issues.append(ScriptIssue("error", "rson-state-code", "OnActCode должен быть строкой", location))
                elif on_act_code.lstrip().startswith("[") and not STATE_EVENTS_RE.match(on_act_code):
                    issues.append(
                        ScriptIssue(
                            "error",
                            "rson-state-events",
                            "Некорректная сигнатура событий в начале OnActCode",
                            location,
                        )
                    )

        duplicates = sorted({value for value in identifiers if identifiers.count(value) > 1})
        for value in duplicates:
            issues.append(ScriptIssue("error", "rson-duplicate-id", f"Повторяется номер объекта #{value}"))
        known = set(identifiers)
        for item in objects:
            parent = item.get("Parent", -1)
            if parent not in (-1, None) and parent not in known:
                issues.append(
                    ScriptIssue("error", "rson-parent", f"Parent #{parent} не существует", f"object #{item.get('#')}")
                )
        links = self.data.get("Visual.Links", [])
        if isinstance(links, list):
            for index, link in enumerate(links):
                if not isinstance(link, dict):
                    issues.append(ScriptIssue("error", "rson-link", "Связь должна быть объектом", f"link[{index}]"))
                    continue
                if link.get("Type") != "TGraphLink":
                    issues.append(
                        ScriptIssue("error", "rson-link-type", "Type связи должен быть TGraphLink", f"link[{index}]")
                    )
                if not isinstance(link.get("Nom"), int) or isinstance(link.get("Nom"), bool) or link["Nom"] < 0:
                    issues.append(
                        ScriptIssue("error", "rson-link-nom", "Nom связи должен быть неотрицательным целым", f"link[{index}]")
                    )
                if not isinstance(link.get("Arrow"), bool):
                    issues.append(
                        ScriptIssue("error", "rson-link-arrow", "Arrow связи должен быть bool", f"link[{index}]")
                    )
                for field in ("Begin", "End"):
                    if link.get(field) not in known:
                        issues.append(
                            ScriptIssue(
                                "error",
                                "rson-link-ref",
                                f"{field} ссылается на отсутствующий объект #{link.get(field)}",
                                f"link[{index}]",
                            )
                        )
        return issues

    def summary(self) -> dict[str, Any]:
        objects = list(self.iter_objects())
        types: dict[str, int] = {}
        code_lines = 0
        for item in objects:
            kind = str(item.get("Type", "unknown"))
            types[kind] = types.get(kind, 0) + 1
            for field in ("Code", "ActCode", "LinkCode"):
                if isinstance(item.get(field), list):
                    code_lines += len(item[field])
        subscriptions: list[dict[str, Any]] = []
        for item in objects:
            if item.get("Type") != "TState" or not isinstance(item.get("OnActCode"), str):
                continue
            match = STATE_EVENTS_RE.match(item["OnActCode"])
            if match:
                subscriptions.append(
                    {"object_id": item.get("#"), "name": item.get("Name"), "events": match.group(1).split(",")}
                )
        return {
            "path": str(self.path) if self.path else None,
            "name": self.name,
            "file_id": self.data.get("FileID"),
            "file_version": self.data.get("FileVersion"),
            "objects": len(objects),
            "links": len(self.data.get("Visual.Links", [])) if isinstance(self.data.get("Visual.Links"), list) else 0,
            "code_lines": code_lines,
            "types": dict(sorted(types.items())),
            "state_event_subscriptions": subscriptions,
            "scr_output": self.data.get("ScriptFileOut"),
            "lang_output": self.data.get("ScriptTextOut"),
        }

    def search_code(self, query: str, *, case_sensitive: bool = False) -> list[dict[str, Any]]:
        needle = query if case_sensitive else query.casefold()
        results: list[dict[str, Any]] = []
        for item in self.iter_objects():
            for field in ("Code", "ActCode", "LinkCode"):
                lines = item.get(field)
                if not isinstance(lines, list):
                    continue
                for number, line in enumerate(lines, start=1):
                    haystack = line if case_sensitive else line.casefold()
                    if needle in haystack:
                        results.append(
                            {
                                "object_id": item.get("#"),
                                "type": item.get("Type"),
                                "name": item.get("Name"),
                                "field": field,
                                "line": number,
                                "text": line,
                            }
                        )
        return results

    def set_code(self, object_id: int, lines: list[str], *, field: str = "Code") -> None:
        if field not in {"Code", "ActCode", "LinkCode", "OnActCode"}:
            raise ValueError("Поле кода: Code, ActCode, LinkCode или OnActCode")
        item = self.object_by_id(object_id)
        if field == "OnActCode":
            if item.get("Type") != "TState":
                raise ValueError(f"OnActCode можно менять только у TState, объект #{object_id}: {item.get('Type')}")
            existing = item.get("OnActCode", "")
            if not isinstance(existing, str):
                raise ValueError(f"TState #{object_id}: OnActCode должен быть строкой")
            handler = "\n".join(str(line) for line in lines)
            if handler.lstrip().startswith("["):
                raise ValueError("Файл обработчика не должен содержать сигнатуру событий; используйте script set-events")
            match = STATE_EVENTS_RE.match(existing)
            signature = match.group(0).rstrip("\r\n") if match else ""
            item[field] = signature + (f"\n{handler}" if signature and handler else handler)
            return
        item[field] = [str(line) for line in lines]
        if "Total.Lines" in item:
            item["Total.Lines"] = len(lines)

    def set_field(self, object_id: int, field: str, value: Any) -> None:
        """Set a JSON field while protecting the graph's primary key."""
        if not field or field == "#":
            raise ValueError("Поле # нельзя менять отдельно: на него ссылается граф")
        item = self.object_by_id(object_id)
        item[field] = value

    def state_events(self, object_id: int) -> list[str]:
        """Return action subscriptions encoded at the start of TState.OnActCode."""
        item = self.object_by_id(object_id)
        if item.get("Type") != "TState":
            raise ValueError(f"Объект #{object_id} имеет тип {item.get('Type')}, ожидался TState")
        on_act_code = item.get("OnActCode", "")
        if not isinstance(on_act_code, str):
            raise ValueError(f"TState #{object_id}: OnActCode должен быть строкой")
        match = STATE_EVENTS_RE.match(on_act_code)
        return match.group(1).split(",") if match else []

    def set_state_events(self, object_id: int, events: Iterable[str]) -> None:
        """Set TState action subscriptions while preserving its handler code.

        RScript stores the subscription signature as the first line of
        ``OnActCode``: ``[t_OnEnteringForm,t_OnPlayerBuyEq|]``. The CLI
        compiler consumes this representation without opening the editor.
        """
        item = self.object_by_id(object_id)
        if item.get("Type") != "TState":
            raise ValueError(f"Объект #{object_id} имеет тип {item.get('Type')}, ожидался TState")
        on_act_code = item.get("OnActCode", "")
        if not isinstance(on_act_code, str):
            raise ValueError(f"TState #{object_id}: OnActCode должен быть строкой")

        normalized: list[str] = []
        for raw in events:
            event = str(raw).strip()
            if not EVENT_NAME_RE.fullmatch(event):
                raise ValueError(f"Некорректное имя события RScript: {event!r}")
            if event not in normalized:
                normalized.append(event)

        match = STATE_EVENTS_RE.match(on_act_code)
        handler = on_act_code[match.end():] if match else on_act_code
        handler = handler.lstrip("\r\n")
        if normalized:
            suffix = match.group(2) if match else ""
            signature = f"[{','.join(normalized)}|{suffix}]"
            item["OnActCode"] = signature + (f"\n{handler}" if handler else "")
        else:
            item["OnActCode"] = handler

    def save(self, path: str | Path) -> Path:
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


def load_rson(path: str | Path) -> RsonProject:
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Корень RSON должен быть JSON-объектом")
    return RsonProject(data=data, path=path)


def inspect_scr(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    data = path.read_bytes()
    if len(data) < 4:
        raise ValueError(f"SCR слишком короткий: {path}")
    version = struct.unpack_from("<I", data)[0]
    strings: list[str] = []
    for match in re.finditer(rb"(?:[\x20-\x7e]\x00){4,}", data):
        strings.append(match.group().decode("utf-16-le"))
    event_signatures = [
        value
        for value in strings
        if re.fullmatch(r"\[t_[A-Za-z0-9_]+(?:,t_[A-Za-z0-9_]+)*\|(?:-?\d+)?\]", value)
    ]
    return {
        "path": str(path),
        "name": path.stem,
        "size": len(data),
        "version": version,
        "supported_version": version in {6, 7, 8},
        "utf16_strings": len(strings),
        "event_signatures": event_signatures,
        "code_samples": [
            value for value in strings if any(token in value for token in (";", "if(", "while(", "for("))
        ][:20],
    }
