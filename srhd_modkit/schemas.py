from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VALIDATION_SCHEMA = "srhd-modkit-schema-validation-v1"
_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _schema_directory():
    return resources.files("srhd_modkit").joinpath("schemas")


def list_schemas() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in sorted(_schema_directory().iterdir(), key=lambda value: value.name.casefold()):
        if item.name.endswith(".json"):
            document = json.loads(item.read_text(encoding="utf-8"))
            result.append(
                {
                    "name": item.name[:-5],
                    "title": document.get("title", ""),
                    "id": document.get("$id", ""),
                }
            )
    return result


def load_schema(name: str) -> dict[str, Any]:
    normalized = name[:-5] if name.casefold().endswith(".json") else name
    if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
        raise ValueError(f"Небезопасное имя JSON Schema: {name}")
    target = _schema_directory().joinpath(f"{normalized}.json")
    if not target.is_file():
        available = ", ".join(item["name"] for item in list_schemas())
        raise FileNotFoundError(f"JSON Schema {normalized!r} не найдена; доступны: {available}")
    return json.loads(target.read_text(encoding="utf-8"))


def _matches_type(value: Any, expected: str) -> bool:
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, _TYPE_MAP[expected])


def _validate(value: Any, schema: Mapping[str, Any], path: str, errors: list[dict[str, str]]) -> None:
    expected = schema.get("type")
    if expected:
        choices = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_type(value, item) for item in choices):
            errors.append(
                {
                    "path": path,
                    "code": "schema-type",
                    "message": f"ожидался тип {'|'.join(choices)}, получен {type(value).__name__}",
                }
            )
            return
    if "const" in schema and value != schema["const"]:
        errors.append(
            {"path": path, "code": "schema-const", "message": f"ожидалось {schema['const']!r}"}
        )
    if "enum" in schema and value not in schema["enum"]:
        errors.append(
            {"path": path, "code": "schema-enum", "message": "значение не входит в enum"}
        )
    if isinstance(value, dict):
        for field in schema.get("required", []):
            if field not in value:
                errors.append(
                    {
                        "path": f"{path}/{field}",
                        "code": "schema-required",
                        "message": "обязательное поле отсутствует",
                    }
                )
        properties = schema.get("properties", {})
        for field, child in value.items():
            if field in properties:
                _validate(child, properties[field], f"{path}/{field}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(
                    {
                        "path": f"{path}/{field}",
                        "code": "schema-additional-property",
                        "message": "неизвестное поле",
                    }
                )
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, child in enumerate(value):
            _validate(child, schema["items"], f"{path}/{index}", errors)
    elif isinstance(value, str) and schema.get("pattern"):
        if re.search(str(schema["pattern"]), value) is None:
            errors.append(
                {"path": path, "code": "schema-pattern", "message": "строка не соответствует pattern"}
            )


def validate_schema_document(
    document: Mapping[str, Any] | str | Path,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    if isinstance(document, (str, Path)):
        path = Path(document).resolve()
        value = json.loads(path.read_text(encoding="utf-8"))
        source = str(path)
    else:
        value = dict(document)
        source = None
    selected = name or value.get("schema")
    if not selected:
        raise ValueError("Не указана схема и в документе отсутствует поле schema")
    schema = load_schema(str(selected))
    errors: list[dict[str, str]] = []
    _validate(value, schema, "$", errors)
    return {
        "schema": SCHEMA_VALIDATION_SCHEMA,
        "document": source,
        "document_schema": str(selected),
        "valid": not errors,
        "errors": errors,
    }


__all__ = ["list_schemas", "load_schema", "validate_schema_document"]
