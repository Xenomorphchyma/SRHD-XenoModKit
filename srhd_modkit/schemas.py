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
_SUPPORTED_KEYWORDS = {
    "$schema", "$id", "$defs", "$ref", "title", "description", "default", "examples",
    "type", "const", "enum", "required", "properties", "additionalProperties",
    "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength",
    "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minProperties", "maxProperties", "allOf", "anyOf", "oneOf", "not",
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


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's ``True == 1`` coercion."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right


def _resolve_local_ref(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"Поддерживаются только локальные JSON Schema $ref: {reference}")
    current: Any = root
    for raw in reference[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or key not in current:
            raise ValueError(f"JSON Schema $ref не найден: {reference}")
        current = current[key]
    if not isinstance(current, Mapping):
        raise ValueError(f"JSON Schema $ref не указывает на схему: {reference}")
    return current


def _branch_errors(value: Any, schema: Mapping[str, Any], path: str, root: Mapping[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    _validate(value, schema, path, result, root)
    return result


def _unsupported_schema_keywords(
    schema: Mapping[str, Any],
    path: str = "$schema",
    *,
    visited: set[int] | None = None,
) -> list[dict[str, str]]:
    """Preflight every schema branch so unsupported semantics are never hidden."""

    seen = visited if visited is not None else set()
    identity = id(schema)
    if identity in seen:
        return []
    seen.add(identity)
    errors = [
        {
            "path": path,
            "code": "schema-keyword-unsupported",
            "message": f"ключевое слово JSON Schema не поддерживается: {keyword}",
        }
        for keyword in sorted(set(schema) - _SUPPORTED_KEYWORDS)
    ]
    for keyword in ("properties", "$defs"):
        children = schema.get(keyword)
        if isinstance(children, Mapping):
            for name, child in children.items():
                if isinstance(child, Mapping):
                    errors.extend(
                        _unsupported_schema_keywords(
                            child,
                            f"{path}/{keyword}/{name}",
                            visited=seen,
                        )
                    )
    for keyword in ("items", "additionalProperties", "not"):
        child = schema.get(keyword)
        if isinstance(child, Mapping):
            errors.extend(
                _unsupported_schema_keywords(child, f"{path}/{keyword}", visited=seen)
            )
    for keyword in ("allOf", "anyOf", "oneOf"):
        children = schema.get(keyword)
        if isinstance(children, list):
            for index, child in enumerate(children):
                if isinstance(child, Mapping):
                    errors.extend(
                        _unsupported_schema_keywords(
                            child,
                            f"{path}/{keyword}/{index}",
                            visited=seen,
                        )
                    )
    return errors


def _validate(
    value: Any,
    schema: Mapping[str, Any],
    path: str,
    errors: list[dict[str, str]],
    root: Mapping[str, Any],
) -> None:
    if "$ref" in schema:
        try:
            resolved = _resolve_local_ref(root, str(schema["$ref"]))
        except ValueError as exc:
            errors.append({"path": path, "code": "schema-ref", "message": str(exc)})
            return
        _validate(value, resolved, path, errors, root)
        return
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        results = [
            _branch_errors(value, branch, path, root)
            for branch in branches
            if isinstance(branch, Mapping)
        ]
        matches = sum(not result for result in results)
        valid = matches == len(results) if keyword == "allOf" else matches >= 1 if keyword == "anyOf" else matches == 1
        if not valid:
            errors.append(
                {
                    "path": path,
                    "code": f"schema-{keyword.casefold()}",
                    "message": f"условие {keyword} не выполнено (совпало ветвей: {matches})",
                }
            )
    negated = schema.get("not")
    if isinstance(negated, Mapping) and not _branch_errors(value, negated, path, root):
        errors.append({"path": path, "code": "schema-not", "message": "значение совпало с запрещённой схемой"})
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
    if "const" in schema and not _json_equal(value, schema["const"]):
        errors.append(
            {"path": path, "code": "schema-const", "message": f"ожидалось {schema['const']!r}"}
        )
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        errors.append(
            {"path": path, "code": "schema-enum", "message": "значение не входит в enum"}
        )
    if isinstance(value, dict):
        for keyword, comparator in (("minProperties", lambda size, limit: size < limit), ("maxProperties", lambda size, limit: size > limit)):
            if keyword in schema and comparator(len(value), int(schema[keyword])):
                errors.append({"path": path, "code": f"schema-{keyword.casefold()}", "message": f"ограничение {keyword} не выполнено"})
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
                _validate(child, properties[field], f"{path}/{field}", errors, root)
            elif schema.get("additionalProperties") is False:
                errors.append(
                    {
                        "path": f"{path}/{field}",
                        "code": "schema-additional-property",
                        "message": "неизвестное поле",
                    }
                )
            elif isinstance(schema.get("additionalProperties"), Mapping):
                _validate(child, schema["additionalProperties"], f"{path}/{field}", errors, root)
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append({"path": path, "code": "schema-minitems", "message": "массив короче minItems"})
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append({"path": path, "code": "schema-maxitems", "message": "массив длиннее maxItems"})
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                errors.append({"path": path, "code": "schema-uniqueitems", "message": "элементы массива не уникальны"})
        if isinstance(schema.get("items"), Mapping):
            for index, child in enumerate(value):
                _validate(child, schema["items"], f"{path}/{index}", errors, root)
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append({"path": path, "code": "schema-minlength", "message": "строка короче minLength"})
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append({"path": path, "code": "schema-maxlength", "message": "строка длиннее maxLength"})
        if schema.get("pattern") and re.search(str(schema["pattern"]), value) is None:
            errors.append(
                {"path": path, "code": "schema-pattern", "message": "строка не соответствует pattern"}
            )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append({"path": path, "code": "schema-minimum", "message": "число меньше minimum"})
        if "maximum" in schema and value > schema["maximum"]:
            errors.append({"path": path, "code": "schema-maximum", "message": "число больше maximum"})
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append({"path": path, "code": "schema-exclusiveminimum", "message": "число не больше exclusiveMinimum"})
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append({"path": path, "code": "schema-exclusivemaximum", "message": "число не меньше exclusiveMaximum"})
        if "multipleOf" in schema:
            divisor = schema["multipleOf"]
            if not divisor or abs((value / divisor) - round(value / divisor)) > 1e-12:
                errors.append({"path": path, "code": "schema-multipleof", "message": "число не кратно multipleOf"})


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
    errors = _unsupported_schema_keywords(schema)
    _validate(value, schema, "$", errors, schema)
    return {
        "schema": SCHEMA_VALIDATION_SCHEMA,
        "document": source,
        "document_schema": str(selected),
        "valid": not errors,
        "errors": errors,
    }


__all__ = ["list_schemas", "load_schema", "validate_schema_document"]
