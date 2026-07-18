from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .blockpar import BlockParDocument, BlockParNode
from .models import ModuleInfo
from .rscript_api import RSCRIPT_RUNTIME_CALLS
from .scripts import RsonProject


IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
FUNCTION_RE = re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.IGNORECASE)
FUNCTION_HEADER_RE = re.compile(
    r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
ASSIGN_ZERO_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*0\s*;")
ASSIGN_ONE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*1\s*;")
ASSIGN_TURN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*CurTurn\s*\(\s*\)\s*;", re.IGNORECASE)
VARIABLE_DECL_RE = re.compile(
    r"\b(?:int|dword|str|float|double|bool)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
VARIABLE_ASSIGN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")

# Calls that need a fully initialized world/player context or can walk a large
# part of the game state. The list is deliberately explicit: unknown calls are
# followed through the local function graph rather than guessed by their name.
WORLD_CALLS = {
    "player",
    "shipstar",
    "getshipplanet",
    "getshipruins",
    "shopitems",
    "storageitems",
    "galaxystars",
    "starnearbystars",
    "starplanets",
    "starruins",
    "starowner",
    "starbattle",
    "itemcost",
    "itemtype",
    "itemlevel",
    "eqmodule",
    "moduletoequipment",
    "itemextraspecialscountbytype",
    "itemextraspecialsaddbytype",
    "itemextraspecialsdeletebytype",
    "addplanetnews",
}

CONTROL_CALLS = {
    "if",
    "while",
    "for",
    "switch",
    "return",
    "function",
}

_KNOWN_UNAVAILABLE_ENGINE_CALLS = {
    "idtostar": (
        "IdToStar отсутствует в игровом API SRHD 2.1.2500; "
        "RScript может собрать такое имя, но игра завершит ход с Not link var :IdToStar. "
        "Определите локальную ограниченную функцию восстановления через "
        "GalaxyStars()/GalaxyStar(i) или храните ID планеты"
    ),
}


@dataclass(frozen=True)
class RuntimeIssue:
    severity: str
    code: str
    message: str
    path: str | None = None
    location: str | None = None
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FunctionBlock:
    name: str
    object_id: int | None
    field: str
    start_line: int
    lines: tuple[str, ...]
    code_type: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def body_text(self) -> str:
        return "\n".join(self.lines[1:])

    @property
    def location(self) -> str:
        return f"object #{self.object_id} {self.field}:{self.start_line} function {self.name}"


@dataclass(frozen=True)
class CodeContainer:
    object_id: int | None
    field: str
    lines: tuple[str, ...]
    code_type: str = ""

    @property
    def location(self) -> str:
        return f"object #{self.object_id} {self.field}"


def _iter_code_containers(project: RsonProject) -> Iterable[CodeContainer]:
    """Yield every executable RSON field, including string OnActCode handlers."""

    for item in project.iter_objects():
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        code_type = str(item.get("Code.Type", "")).casefold()
        for field, value in item.items():
            if not field.casefold().endswith("code"):
                continue
            if isinstance(value, list):
                lines = tuple(str(line) for line in value)
            elif isinstance(value, str):
                lines = tuple(value.splitlines())
            else:
                continue
            yield CodeContainer(object_id, field, lines, code_type)


def _mask_non_code(text: str) -> str:
    """Replace comments and string contents with spaces, preserving offsets."""
    output: list[str] = []
    state = "code"
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "line-comment":
            if char == "\n":
                state = "code"
                output.append(char)
            else:
                output.append(" ")
        elif state == "block-comment":
            if char == "*" and following == "/":
                output.extend((" ", " "))
                index += 1
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
        elif state == "string":
            if char == "\\" and following:
                output.extend((" ", " "))
                index += 1
            elif char == quote:
                output.append(" ")
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
        elif char == "/" and following == "/":
            output.extend((" ", " "))
            index += 1
            state = "line-comment"
        elif char == "/" and following == "*":
            output.extend((" ", " "))
            index += 1
            state = "block-comment"
        elif char in {"'", '"'}:
            quote = char
            output.append(" ")
            state = "string"
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _calls(text: str) -> set[str]:
    masked = _mask_non_code(text)
    return {
        match.group(1)
        for match in CALL_RE.finditer(masked)
        if match.group(1).casefold() not in CONTROL_CALLS
    }


def _brace_delta(text: str) -> int:
    masked = _mask_non_code(text)
    return masked.count("{") - masked.count("}")


def _extract_functions(project: RsonProject) -> tuple[dict[str, FunctionBlock], list[RuntimeIssue]]:
    functions: dict[str, FunctionBlock] = {}
    issues: list[RuntimeIssue] = []
    for item in project.iter_objects():
        for field in ("Code", "ActCode", "LinkCode"):
            lines = item.get(field)
            if not isinstance(lines, list):
                continue
            index = 0
            while index < len(lines):
                match = FUNCTION_RE.match(_mask_non_code(lines[index]))
                if not match:
                    index += 1
                    continue
                start = index
                depth = 0
                opened = False
                while index < len(lines):
                    masked_line = _mask_non_code(lines[index])
                    if "{" in masked_line:
                        opened = True
                    depth += masked_line.count("{") - masked_line.count("}")
                    index += 1
                    if opened and depth <= 0:
                        break
                block = FunctionBlock(
                    match.group(1),
                    item.get("#") if isinstance(item.get("#"), int) else None,
                    field,
                    start + 1,
                    tuple(str(value) for value in lines[start:index]),
                    str(item.get("Code.Type", "")).casefold(),
                )
                key = block.name.casefold()
                if key in functions:
                    issues.append(
                        RuntimeIssue(
                            "error",
                            "runtime-duplicate-function",
                            f"Функция {block.name} определена несколько раз; порядок вызова неоднозначен",
                            str(project.path) if project.path else None,
                            block.location,
                        )
                    )
                else:
                    functions[key] = block
    return functions, issues


def _call_graph(functions: dict[str, FunctionBlock]) -> dict[str, set[str]]:
    known = set(functions)
    return {
        name: {call.casefold() for call in _calls(block.body_text) if call.casefold() in known}
        for name, block in functions.items()
    }


def _local_function_names(lines: list[str]) -> set[str]:
    return {
        match.group(1).casefold()
        for line in lines
        if (match := FUNCTION_RE.match(_mask_non_code(str(line))))
    }


def _lint_cross_block_calls(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject calls to a user function defined only in another code object.

    RScript compiles Top code objects as separate scopes.  Merely seeing a
    function with the requested name somewhere in the RSON therefore does not
    make it linkable from a Turn/ActCode/LinkCode object.  The compiler can
    still emit SCR in this situation, but the game fails later with
    ``Not link var :FunctionName``.
    """
    known = set(functions)
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for item in project.iter_objects():
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        containers: list[tuple[str, list[str]]] = []
        for field, value in item.items():
            if field in {"Code", "ActCode", "LinkCode"} and isinstance(value, list):
                containers.append((field, [str(line) for line in value]))
            elif field.casefold().endswith("code") and isinstance(value, str):
                containers.append((field, value.splitlines()))
        for field, lines in containers:
            local = _local_function_names(lines)
            reported: set[str] = set()
            for line_number, line in enumerate(lines, start=1):
                declaration = FUNCTION_RE.match(_mask_non_code(line))
                declared = declaration.group(1).casefold() if declaration else None
                for call in sorted(value.casefold() for value in _calls(line)):
                    if call == declared or call not in known or call in local or call in reported:
                        continue
                    target = functions[call]
                    # Projects produced by RScript 4.10f use an explicit Init
                    # Top as the shared function library for runtime objects.
                    # Unlabelled/Global code objects remain separate scopes.
                    caller_code_type = str(item.get("Code.Type", "")).casefold()
                    if target.code_type == "init" and field == "Code" and caller_code_type == "turn":
                        continue
                    issues.append(
                        RuntimeIssue(
                            "error",
                            "runtime-cross-block-function-call",
                            f"Вызов {target.name} не слинкуется: функция определена в другом RSON code object; перенесите код или определение в один {field}",
                            path,
                            f"object #{object_id} {field}:{line_number}",
                            line.strip(),
                        )
                    )
                    reported.add(call)
    return issues


def _lint_unavailable_engine_calls(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject engine-like calls proven unavailable at runtime.

    RScript serializes unknown external call names without resolving them
    against the game executable.  A same-project function with that name is
    still valid and is checked separately for code-object scope.
    """

    known = set(functions)
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for item in project.iter_objects():
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        containers: list[tuple[str, list[str]]] = []
        for field, value in item.items():
            if field in {"Code", "ActCode", "LinkCode"} and isinstance(value, list):
                containers.append((field, [str(line) for line in value]))
            elif field.casefold().endswith("code") and isinstance(value, str):
                containers.append((field, value.splitlines()))
        for field, lines in containers:
            local = _local_function_names(lines)
            reported: set[str] = set()
            for line_number, line in enumerate(lines, start=1):
                declaration = FUNCTION_RE.match(_mask_non_code(line))
                declared = declaration.group(1).casefold() if declaration else None
                for call in sorted(value.casefold() for value in _calls(line)):
                    if (
                        call == declared
                        or call not in _KNOWN_UNAVAILABLE_ENGINE_CALLS
                        or call in local
                        or call in known
                        or call in reported
                    ):
                        continue
                    issues.append(
                        RuntimeIssue(
                            "error",
                            "runtime-unsupported-engine-call",
                            _KNOWN_UNAVAILABLE_ENGINE_CALLS[call],
                            path,
                            f"object #{object_id} {field}:{line_number}",
                            line.strip(),
                        )
                    )
                    reported.add(call)
    return issues


def _line_comment_start(line: str) -> int | None:
    """Return the first // outside an RScript string literal."""

    quote = ""
    escaped = False
    for index, char in enumerate(line[:-1]):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "/" and line[index + 1] == "/":
            return index
    return None


def _lint_apostrophes_in_line_comments(project: RsonProject) -> list[RuntimeIssue]:
    """Reject apostrophes that old SRHD runtime linking can parse as quotes."""

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for container in _iter_code_containers(project):
        for line_number, line in enumerate(container.lines, start=1):
            comment_start = _line_comment_start(line)
            comment = line[comment_start + 2 :] if comment_start is not None else ""
            if comment_start is None or comment.count("'") % 2 == 0:
                continue
            issues.append(
                RuntimeIssue(
                    "error",
                    "runtime-apostrophe-in-line-comment",
                    "Непарный апостроф в //-комментарии может нарушить линковку старого runtime и завершить ход с Not link var; замените его типографским апострофом, дефисом или переформулируйте комментарий",
                    path,
                    f"{container.location}:{line_number}",
                    line.strip(),
                )
            )
    return issues


def _callable_tvars(project: RsonProject) -> set[str]:
    result = {
        str(item.get("Name", "")).casefold()
        for item in project.iter_objects()
        if str(item.get("Type", "")).casefold() == "tvar" and str(item.get("Name", "")).strip()
    }
    imported_assignment = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*ImportedFunction\s*\(",
        re.IGNORECASE,
    )
    for container in _iter_code_containers(project):
        for line in container.lines:
            result.update(
                match.group(1).casefold()
                for match in imported_assignment.finditer(_mask_non_code(line))
            )
    return result


def _lint_unresolved_user_functions(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject calls absent from the local scope, imports and SRHD API registry."""

    known_project = set(functions)
    shared_init = {name for name, block in functions.items() if block.code_type == "init"}
    callables = _callable_tvars(project)
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for container in _iter_code_containers(project):
        local = _local_function_names(list(container.lines))
        available = set(RSCRIPT_RUNTIME_CALLS) | callables | local
        if container.field == "Code" and container.code_type == "turn":
            available.update(shared_init)
        reported: set[str] = set()
        for line_number, line in enumerate(container.lines, start=1):
            declaration = FUNCTION_RE.match(_mask_non_code(line))
            declared = declaration.group(1).casefold() if declaration else None
            for original in sorted(_calls(line), key=str.casefold):
                call = original.casefold()
                if call == declared or call in available or call in known_project or call in reported:
                    continue
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-unresolved-user-function",
                        f"Вызов {original} не определён в этом code object, не импортирован и отсутствует в реестре API SRHD 2.1.2500; игра может завершить ход с Not link var :{original}",
                        path,
                        f"{container.location}:{line_number}",
                        line.strip(),
                    )
                )
                reported.add(call)
    return issues


def _rscript_array_names(project: RsonProject) -> set[str]:
    names: set[str] = set()
    new_array = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*newarray\s*\(",
        re.IGNORECASE,
    )
    for item in project.iter_objects():
        if str(item.get("Type", "")).casefold() == "tvar":
            init = str(item.get("Init", ""))
            if re.search(r"\bnewarray\s*\(\s*1\s*\)", init, re.IGNORECASE):
                name = str(item.get("Name", "")).strip()
                if name:
                    names.add(name.casefold())
    for container in _iter_code_containers(project):
        for line in container.lines:
            masked = _mask_non_code(line)
            for match in new_array.finditer(masked):
                arguments = _call_arguments(match.group(0) + masked[match.end() :], "newarray")
                if arguments and arguments[0][1] and arguments[0][1][0].strip() == "1":
                    names.add(match.group(1).casefold())
            for call in ("ArrayAdd", "ArrayClear", "ArrayDelete"):
                for _position, arguments in _call_arguments(masked, call):
                    if arguments and (name := _simple_identifier(arguments[0])):
                        names.add(name)
    return names


def _brace_block_end(lines: tuple[str, ...], start: int) -> int:
    depth = 0
    opened = False
    for index in range(start, len(lines)):
        masked = _mask_non_code(lines[index])
        opened |= "{" in masked
        depth += masked.count("{") - masked.count("}")
        if opened and depth <= 0:
            return index
    return start


def _lint_rscript_arrays(project: RsonProject) -> list[RuntimeIssue]:
    """Enforce the one-based data ABI of RScript dynamic arrays."""

    arrays = _rscript_array_names(project)
    shared_tvars = _shared_tvars(project)
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    reported: set[tuple[int | None, str, int, str]] = set()

    def report(
        container: CodeContainer,
        line_number: int,
        code: str,
        severity: str,
        message: str,
        evidence: str,
    ) -> None:
        key = (container.object_id, container.field, line_number, code)
        if key in reported:
            return
        reported.add(key)
        issues.append(
            RuntimeIssue(
                severity,
                code,
                message,
                path,
                f"{container.location}:{line_number}",
                evidence.strip(),
            )
        )

    direct_zero = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*0+\s*\]",
        re.IGNORECASE,
    )
    dim_compare = re.compile(
        r"\bArrayDim\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*"
        r"(<=|>=|==|!=|<|>)\s*([01])\b",
        re.IGNORECASE,
    )
    reversed_dim_compare = re.compile(
        r"\b([01])\s*(<=|>=|==|!=|<|>)\s*"
        r"ArrayDim\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        re.IGNORECASE,
    )
    boolean_dim = re.compile(
        r"\bif\s*\(\s*!?\s*ArrayDim\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\)",
        re.IGNORECASE,
    )
    for_header = re.compile(r"\bfor\s*\((.*)\)", re.IGNORECASE)

    for container in _iter_code_containers(project):
        masked_lines = tuple(_mask_non_code(line) for line in container.lines)
        for index, (line, masked) in enumerate(zip(container.lines, masked_lines)):
            for match in direct_zero.finditer(masked):
                if match.group(1).casefold() not in arrays:
                    continue
                report(
                    container,
                    index + 1,
                    "runtime-rscript-array-service-index",
                    "error",
                    f"{match.group(1)}[0] обращается к служебному vtUnknown-элементу RScript; реальные записи массива начинаются с индекса 1",
                    line,
                )

            for match in dim_compare.finditer(masked):
                name, operator, constant = match.group(1).casefold(), match.group(2), match.group(3)
                unsafe = constant == "0" and operator in {">", "<=", "==", "!=", "<"}
                unsafe |= constant == "1" and operator == ">="
                if name in arrays and unsafe:
                    report(
                        container,
                        index + 1,
                        "runtime-rscript-array-empty-dimension",
                        "error",
                        f"ArrayDim({match.group(1)}) сравнивается как у нулевого массива, но пустой newarray(1)/ArrayClear имеет размер 1; используйте > 1 для наличия записей и <= 1 для пустоты",
                        line,
                    )
            for match in reversed_dim_compare.finditer(masked):
                constant, operator, name = match.group(1), match.group(2), match.group(3).casefold()
                # Reverse the operator to the ArrayDim-left form.
                reverse = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}
                normalized = reverse[operator]
                unsafe = constant == "0" and normalized in {">", "<=", "==", "!=", "<"}
                unsafe |= constant == "1" and normalized == ">="
                if name in arrays and unsafe:
                    report(
                        container,
                        index + 1,
                        "runtime-rscript-array-empty-dimension",
                        "error",
                        f"Проверка размера {match.group(3)} использует нулевую модель массива; у пустого RScript-массива ArrayDim равен 1",
                        line,
                    )
            for match in boolean_dim.finditer(masked):
                if match.group(1).casefold() in arrays:
                    report(
                        container,
                        index + 1,
                        "runtime-rscript-array-empty-dimension",
                        "error",
                        "ArrayDim используется как boolean, но пустой RScript-массив уже имеет истинный размер 1; сравните размер с 1 явно",
                        line,
                    )

            loop = for_header.search(masked)
            if not loop:
                continue
            clauses = loop.group(1).split(";")
            if len(clauses) != 3:
                continue
            iterator_match = re.search(
                r"(?:\bint\s+)?\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([^;]+)",
                clauses[0],
                re.IGNORECASE,
            )
            if not iterator_match:
                continue
            iterator = iterator_match.group(1).casefold()
            initial = re.sub(r"\s+", "", iterator_match.group(2)).casefold()
            condition = re.sub(r"\s+", "", clauses[1]).casefold()
            end = _brace_block_end(container.lines, index)
            body = _mask_non_code("\n".join(container.lines[index : end + 1]))
            indexed = {
                match.group(1).casefold()
                for match in re.finditer(
                    rf"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*{re.escape(iterator)}\s*\]",
                    body,
                    re.IGNORECASE,
                )
            }
            indexed &= arrays
            if indexed and (
                initial in {"0", "+0"}
                or re.search(rf"\b{re.escape(iterator)}>=0\b", condition)
                or re.search(rf"\b{re.escape(iterator)}>-1\b", condition)
            ):
                report(
                    container,
                    index + 1,
                    "runtime-rscript-array-service-index",
                    "error",
                    f"Цикл разыменовывает {', '.join(sorted(indexed))}[{iterator}] и допускает служебный индекс 0; начните прямой обход с 1, а обратный завершайте на >= 1",
                    line,
                )

            shared_indexed = indexed & shared_tvars
            if len(shared_indexed) < 2:
                continue
            prefix = _mask_non_code("\n".join(container.lines[: index + 1]))
            pairs = {
                frozenset((left.casefold(), right.casefold()))
                for left, right in re.findall(
                    r"ArrayDim\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*(?:==|!=)\s*"
                    r"ArrayDim\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
                    prefix,
                    re.IGNORECASE,
                )
            }
            missing = [
                (left, right)
                for pos, left in enumerate(sorted(shared_indexed))
                for right in sorted(shared_indexed)[pos + 1 :]
                if frozenset((left, right)) not in pairs
            ]
            if missing:
                left, right = missing[0]
                report(
                    container,
                    index + 1,
                    "runtime-rscript-paired-array-dimension",
                    "warning",
                    f"Один индекс читает persistent-массивы {left} и {right} без доказанного равенства ArrayDim; старое сохранение или частичное обновление может разъединить пары",
                    line,
                )
    return issues


_ITEM_RETURN_CALLS = {
    "createequipment",
    "createhull",
    "createitem",
    "createquestitem",
    "getitemfromship",
    "groupitem",
    "idtoitem",
    "planetitems",
    "shipeqinslot",
    "shopitems",
    "staritems",
    "storageitems",
}


def _item_returning_functions(functions: dict[str, FunctionBlock]) -> set[str]:
    """Infer helpers whose result is a proven Item object."""

    result: set[str] = set()
    changed = True
    assignment = re.compile(
        r"(?:\b(?:int|dword|str|float|double|bool|unknown)\s+)?"
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )
    while changed:
        changed = False
        sources = _ITEM_RETURN_CALLS | result
        for name, block in functions.items():
            item_vars: set[str] = set()
            returns_item = False
            for line in block.lines[1:]:
                masked = _mask_non_code(line)
                for match in assignment.finditer(masked):
                    target = match.group(1).casefold()
                    expression = match.group(2)
                    calls = {call.casefold() for call in _calls(expression)}
                    identifiers = {value.casefold() for value in IDENTIFIER_RE.findall(expression)}
                    is_item = bool(calls & sources or identifiers & item_vars)
                    if target == "result" and is_item:
                        returns_item = True
                    elif target != "result":
                        if is_item:
                            item_vars.add(target)
                        else:
                            item_vars.discard(target)
            if returns_item and name not in result:
                result.add(name)
                changed = True
    return result


def _lint_rndobject_anchor_types(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject an Item passed as UtilityFunctions.RndObject's object anchor."""

    sources = _ITEM_RETURN_CALLS | _item_returning_functions(functions)
    graph_items = {
        str(item.get("Name", "")).casefold()
        for item in project.iter_objects()
        if str(item.get("Type", "")).casefold() == "titem" and str(item.get("Name", "")).strip()
    }
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    assignment = re.compile(
        r"(?:\b(?:int|dword|str|float|double|bool|unknown)\s+)?"
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )
    for container in _iter_code_containers(project):
        item_vars = set(graph_items)
        for line_number, line in enumerate(container.lines, start=1):
            masked = _mask_non_code(line)
            # Evaluate anchors before assignments on the same statement so a
            # previous value is not accidentally replaced by the result lhs.
            for _position, arguments in _call_arguments(masked, "RndObject"):
                if len(arguments) < 3:
                    continue
                anchor = arguments[2]
                anchor_calls = {call.casefold() for call in _calls(anchor)}
                anchor_identifiers = {
                    value.casefold() for value in IDENTIFIER_RE.findall(anchor)
                }
                if not (anchor_calls & sources or anchor_identifiers & item_vars):
                    continue
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-rndobject-anchor-type",
                        "Третий аргумент UtilityFunctions.RndObject доказан как Item, но DLL принимает только поддерживаемый мировой anchor (Player/Ship/Planet/Star); для независимого броска используйте Rnd",
                        path,
                        f"{container.location}:{line_number}",
                        line.strip(),
                    )
                )
            for match in assignment.finditer(masked):
                target = match.group(1).casefold()
                expression = match.group(2)
                calls = {call.casefold() for call in _calls(expression)}
                identifiers = {value.casefold() for value in IDENTIFIER_RE.findall(expression)}
                if calls & sources or identifiers & item_vars:
                    item_vars.add(target)
                else:
                    item_vars.discard(target)
    return issues


_PROVEN_NON_STRING_STATE_RE = re.compile(
    r"(?:[-+]?\d+(?:\.\d+)?|true|false|null)",
    re.IGNORECASE,
)


def _has_id_to_ship_guard(prefix: str, variable: str) -> bool:
    """Prove that a simple IdToShip argument is greater than reserved IDs.

    SRHD 2.1.2500 does not return a safe null handle for ``IdToShip(0)``.
    The scripting reference also explicitly requires an ID greater than 1.
    Accept only an enclosing positive check or an early-exit negative guard;
    a plain ``if(id)`` still permits the reserved ID 1.
    """

    escaped = re.escape(variable)
    positive = re.compile(
        rf"\bif\s*\(\s*(?:{escaped}\s*>\s*1|{escaped}\s*>=\s*2)\s*\)",
        re.IGNORECASE,
    )
    negative_exit = re.compile(
        rf"\bif\s*\(\s*(?:{escaped}\s*<=\s*1|{escaped}\s*<\s*2)\s*\)"
        rf"\s*(?:\{{\s*)?(?:exit|return)\b",
        re.IGNORECASE | re.DOTALL,
    )
    return bool(positive.search(prefix) or negative_exit.search(prefix))


def _lint_id_to_ship_guards(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject IdToShip calls that can receive the reserved IDs 0 or 1."""

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for block in functions.values():
        masked_lines = [_mask_non_code(line) for line in block.lines]
        for line_offset, masked in enumerate(masked_lines):
            for _position, arguments in _call_arguments(masked, "IdToShip"):
                if not arguments:
                    continue
                argument = arguments[0].strip()
                literal = re.fullmatch(r"[-+]?\d+", argument)
                if literal:
                    if int(argument) > 1:
                        continue
                else:
                    variable = _simple_identifier(argument)
                    if variable is None:
                        # Expressions such as Id(ship) carry their own object
                        # provenance and are outside this simple guard proof.
                        continue
                    prefix = "\n".join(masked_lines[: line_offset + 1])
                    if _has_id_to_ship_guard(prefix, variable):
                        continue
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-id-to-ship-reserved-id",
                        "IdToShip требует доказанный ID больше 1; при ID 0 движок может вернуть непригодный указатель, а следующий ShipInScript/ShipStar аварийно завершит ход",
                        path,
                        f"{block.location} line {block.start_line + line_offset}",
                        block.lines[line_offset].strip(),
                    )
                )
    return issues


def _lint_suppressed_shipjoin_state(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject locked joined ships whose automatic initial state is disabled.

    A non-string third ShipJoin argument explicitly suppresses automatic state
    entry.  Locking such an NPC without a subsequent ChangeState leaves the
    engine warrior without valid AI state and can crash TWarrior.NextDay.
    """

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for block in functions.values():
        sites = _function_call_sites(block)
        for line_offset, _depth, call, arguments in sites:
            if call != "shipjoin" or len(arguments) < 3:
                continue
            if not _PROVEN_NON_STRING_STATE_RE.fullmatch(arguments[2].strip()):
                continue
            ship = _simple_identifier(arguments[1])
            if ship is None:
                continue
            later_sites = [site for site in sites if site[0] >= line_offset]
            locks_ship = any(
                later_call == "orderlock"
                and len(later_arguments) >= 2
                and _simple_identifier(later_arguments[0]) == ship
                and later_arguments[1].strip() == "1"
                for _later_line, _later_depth, later_call, later_arguments in later_sites
            )
            changes_state = any(
                later_call == "changestate"
                and len(later_arguments) >= 2
                and _simple_identifier(later_arguments[1]) == ship
                for _later_line, _later_depth, later_call, later_arguments in later_sites
            )
            if not locks_ship or changes_state:
                continue
            issues.append(
                RuntimeIssue(
                    "error",
                    "runtime-shipjoin-state-suppressed",
                    "ShipJoin получает нестроковый третий аргумент и отключает начальное State, после чего корабль блокируется через OrderLock без ChangeState; используйте ShipJoin(group, ship), строковое имя State или явно вызовите ChangeState",
                    path,
                    f"{block.location} line {block.start_line + line_offset}",
                    block.lines[line_offset].strip(),
                )
            )
    return issues


_SHIP_IN_CURRENT_GUARD_RE = re.compile(
    r"if\s*\(\s*!\s*ShipInCurScript\s*\(\s*"
    r"(?P<ship>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\)\s*"
    r"ShipJoin\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*(?P=ship)\b",
    re.IGNORECASE,
)


def _lint_shipjoin_guarded_by_script_membership(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject a script-ownership test used as a group-membership test.

    ``ShipInCurScript(ship)`` only says that some object in the current script
    owns the ship.  It does not prove that the ship belongs to the specific
    group passed to ``ShipJoin``.  Guarding the join this way can leave a newly
    bought transport/warrior under vanilla AI while the intended group remains
    empty, so route setup and scripted orders silently never start.
    """

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for block in functions.values():
        masked = _mask_non_code(block.text)
        for match in _SHIP_IN_CURRENT_GUARD_RE.finditer(masked):
            line_offset = masked.count("\n", 0, match.start())
            issues.append(
                RuntimeIssue(
                    "error",
                    "runtime-shipjoin-script-membership-guard",
                    "ShipInCurScript проверяет принадлежность всему скрипту, а не целевой TGroup; такой guard может пропустить обязательный ShipJoin и оставить корабль с ванильным грузом/ИИ. Для нового корабля вызывайте ShipJoin безусловно либо отдельно проверяйте GroupShip целевой группы",
                    path,
                    f"{block.location} line {block.start_line + line_offset}",
                    block.lines[line_offset].strip(),
                )
            )
    return issues


def _variable_definitions(lines: list[str]) -> set[str]:
    masked = _mask_non_code("\n".join(lines))
    return {
        match.group(1).casefold()
        for pattern in (VARIABLE_DECL_RE, VARIABLE_ASSIGN_RE)
        for match in pattern.finditer(masked)
    }


def _lint_runtime_cross_block_variables(project: RsonProject) -> list[RuntimeIssue]:
    """Reject runtime references to variables owned by another code object.

    RScript compiles Turn operations, statements and action handlers as separate
    scopes.  The compiler may still emit SCR when one of them reads a variable
    assigned elsewhere, while the game later stops with
    ``Not link var :variable`` when evaluating that runtime object.
    """
    definitions: dict[str, set[tuple[int | None, str]]] = {}
    shared_tvars = {
        str(item.get("Name", "")).casefold()
        for item in project.iter_objects()
        if str(item.get("Type", "")).casefold() == "tvar" and str(item.get("Name", "")).strip()
    }
    containers: list[tuple[dict[str, Any], str, list[str]]] = []
    for item in project.iter_objects():
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        for field, value in item.items():
            if field in {"Code", "ActCode", "LinkCode"} and isinstance(value, list):
                lines = [str(line) for line in value]
            elif field.casefold().endswith("code") and isinstance(value, str):
                lines = value.splitlines()
            else:
                continue
            containers.append((item, field, lines))
            for variable in _variable_definitions(lines):
                definitions.setdefault(variable, set()).add((object_id, field))

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for item, field, lines in containers:
        code_type = str(item.get("Code.Type", "")).casefold()
        is_turn_code = field == "Code" and code_type == "turn"
        is_action_code = field.casefold() in {"actcode", "linkcode", "onactcode"}
        if not (is_turn_code or is_action_code):
            continue
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        local = _variable_definitions(lines)
        reported: set[str] = set()
        for line_number, line in enumerate(lines, start=1):
            masked = _mask_non_code(line)
            for match in IDENTIFIER_RE.finditer(masked):
                variable = match.group(0).casefold()
                if variable in shared_tvars:
                    continue
                owners = definitions.get(variable, set())
                if not owners or variable in local or variable in reported:
                    continue
                if owners == {(object_id, field)}:
                    continue
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-cross-block-variable-reference",
                        f"Runtime-объект не слинкует переменную {match.group(0)}, определённую в другом RSON code object; объедините чтение и определение в одном объекте",
                        path,
                        f"object #{object_id} {field}:{line_number}",
                        line.strip(),
                    )
                )
                reported.add(variable)
    return issues


def _lint_linked_empty_runtime_code(project: RsonProject) -> list[RuntimeIssue]:
    """Reject empty code arrays on linked Turn graph objects.

    RScript 4.10f can hang indefinitely while compiling even a tiny project
    when an active graph chain reaches a Top/statement with ``Code=[]``.  Empty
    isolated editor templates are ignored because they are not executable.
    """
    objects = {
        item["#"]: item
        for item in project.iter_objects()
        if isinstance(item.get("#"), int)
        and str(item.get("Code.Type", "")).casefold() == "turn"
    }
    if not objects:
        return []

    outgoing: dict[int, set[int]] = {object_id: set() for object_id in objects}
    incoming: dict[int, set[int]] = {object_id: set() for object_id in objects}
    linked: set[int] = set()
    links = project.data.get("Visual.Links", [])
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            begin = link.get("Begin")
            end = link.get("End")
            if begin in objects:
                linked.add(begin)
            if end in objects:
                linked.add(end)
            if begin in objects and end in objects:
                outgoing[begin].add(end)
                incoming[end].add(begin)
    if not linked:
        return []

    roots = {object_id for object_id in linked if not incoming[object_id]}
    active: set[int] = set()
    pending = list(roots)
    while pending:
        object_id = pending.pop()
        if object_id in active:
            continue
        active.add(object_id)
        pending.extend(outgoing[object_id])
    # A closed linked cycle has no syntactic root but is still unsafe if the
    # engine can enter it through an implicit runtime edge.
    active.update(linked - active)

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for object_id in sorted(active):
        item = objects[object_id]
        for field in ("Code", "ActCode", "LinkCode"):
            value = item.get(field)
            if isinstance(value, list) and not value:
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-linked-empty-code",
                        f"Связанный runtime-объект #{object_id} содержит пустой {field}=[]; RScript может зависнуть, добавьте рабочий код или уникальную no-op строку",
                        path,
                        f"object #{object_id} {field}",
                        f"{field}=[]",
                    )
                )
    return issues


def _reachable(starts: Iterable[str], graph: dict[str, set[str]]) -> set[str]:
    pending = [name.casefold() for name in starts if name.casefold() in graph]
    result: set[str] = set()
    while pending:
        name = pending.pop()
        if name in result:
            continue
        result.add(name)
        pending.extend(graph.get(name, ()))
    return result


def _direct_world_calls(text: str) -> set[str]:
    return {call for call in _calls(text) if call.casefold() in WORLD_CALLS}


def _risky_functions(functions: dict[str, FunctionBlock], graph: dict[str, set[str]]) -> set[str]:
    risky = {name for name, block in functions.items() if _direct_world_calls(block.body_text)}
    changed = True
    while changed:
        changed = False
        for name, callees in graph.items():
            if name not in risky and callees & risky:
                risky.add(name)
                changed = True
    return risky


def _first_risky_line(block: FunctionBlock, risky: set[str]) -> int | None:
    for index, line in enumerate(block.lines[1:], start=1):
        calls = {value.casefold() for value in _calls(line)}
        if calls & WORLD_CALLS or calls & risky:
            return index
    return None


def _has_exit_guard(lines: tuple[str, ...], variable: str, before: int) -> bool:
    wanted = variable.casefold()
    for index in range(1, max(1, before)):
        window = "\n".join(lines[index : min(before, index + 7)])
        masked = _mask_non_code(window)
        folded = masked.casefold()
        if "if" not in folded or "exit" not in folded:
            continue
        negative = re.search(rf"!\s*{re.escape(wanted)}\b", folded)
        zero = re.search(rf"\b{re.escape(wanted)}\s*==\s*0\b", folded)
        if negative or zero:
            return True
    return False


def _has_turn_grace(lines: tuple[str, ...], variable: str, before: int) -> bool:
    prefix = _mask_non_code("\n".join(lines[1:before])).casefold()
    wanted = re.escape(variable.casefold())
    comparisons = (
        rf"curturn\s*\(\s*\)\s*<=\s*{wanted}\b",
        rf"\b{wanted}\s*>=\s*curturn\s*\(\s*\)",
    )
    return "exit" in prefix and any(re.search(pattern, prefix) for pattern in comparisons)


def _find_recursion_cycles(graph: dict[str, set[str]], starts: set[str]) -> list[tuple[str, ...]]:
    cycles: set[tuple[str, ...]] = set()
    active: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in active:
            cycle = active[active.index(name) :] + [name]
            core = cycle[:-1]
            if core:
                rotations = [tuple(core[index:] + core[:index]) for index in range(len(core))]
                cycles.add(min(rotations))
            return
        if name in visited:
            return
        active.append(name)
        for child in graph.get(name, ()):
            visit(child)
        active.pop()
        visited.add(name)

    for start in starts:
        visit(start)
    return sorted(cycles)


def _loop_profile(
    block: FunctionBlock,
    known_functions: set[str],
) -> tuple[int, str | None, dict[str, int]]:
    """Return deepest world access and local-call depths for structured loops."""
    token_re = re.compile(
        r"\b(?:(for|while)\s*\(|([A-Za-z_][A-Za-z0-9_]*)\s*\()|[{}]",
        re.IGNORECASE,
    )
    stack: list[bool] = []
    pending_loop = False
    best_world_depth = -1
    evidence: str | None = None
    call_depths: dict[str, int] = {}
    for line in block.lines[1:]:
        masked = _mask_non_code(line)
        for match in token_re.finditer(masked):
            token = match.group(0)
            if token == "}":
                if stack:
                    stack.pop()
            elif token == "{":
                stack.append(pending_loop)
                pending_loop = False
            elif match.group(1):
                pending_loop = True
            else:
                call = match.group(2)
                if not call:
                    continue
                name = call.casefold()
                depth = sum(stack)
                if name in WORLD_CALLS and depth > best_world_depth:
                    best_world_depth = depth
                    evidence = line.strip()
                if name in known_functions:
                    call_depths[name] = max(call_depths.get(name, -1), depth)
    return best_world_depth, evidence, call_depths


def _runtime_loop_depths(
    starts: set[str],
    graph: dict[str, set[str]],
    functions: dict[str, FunctionBlock],
) -> dict[str, tuple[int, int, str | None]]:
    known = set(functions)
    profiles = {name: _loop_profile(block, known) for name, block in functions.items()}
    result: dict[str, tuple[int, int, str | None]] = {}

    def walk(name: str, inherited: int, path: frozenset[str]) -> None:
        if name in path:
            return
        world_depth, evidence, calls = profiles.get(name, (-1, None, {}))
        if world_depth >= 0:
            total = inherited + world_depth
            previous = result.get(name)
            if previous is None or total > previous[0]:
                result[name] = (total, world_depth, evidence)
        next_path = path | {name}
        for child in graph.get(name, ()):
            walk(child, inherited + calls.get(child, 0), next_path)

    for start in starts:
        walk(start, 0, frozenset())
    return result


def _global_initialization_lines(project: RsonProject) -> list[tuple[int | None, int, str]]:
    result: list[tuple[int | None, int, str]] = []
    for item in project.iter_objects():
        code_type = str(item.get("Code.Type", "")).casefold()
        if code_type not in {"", "global", "init"}:
            continue
        if not code_type and str(item.get("Type", "")).casefold() != "top":
            continue
        lines = item.get("Code")
        if not isinstance(lines, list):
            continue
        in_function = False
        function_opened = False
        depth = 0
        for index, line in enumerate(lines, start=1):
            masked = _mask_non_code(line)
            if not in_function and FUNCTION_RE.match(masked):
                in_function = True
                function_opened = False
                depth = 0
            if in_function:
                if "{" in masked:
                    function_opened = True
                depth += masked.count("{") - masked.count("}")
                if function_opened and depth <= 0:
                    in_function = False
                continue
            result.append((item.get("#") if isinstance(item.get("#"), int) else None, index, line))
    return result


def _dialog_scoped_turn_objects(project: RsonProject) -> set[int]:
    """Find Turn graph nodes reached from dialog events, not the game clock."""

    objects = {
        item["#"]: item
        for item in project.iter_objects()
        if isinstance(item.get("#"), int)
    }
    turns = {
        object_id
        for object_id, item in objects.items()
        if str(item.get("Code.Type", "")).casefold() == "turn"
    }
    dialog_sources = {
        object_id
        for object_id, item in objects.items()
        if str(item.get("Type", "")).casefold().startswith("tdialog")
        or str(item.get("Code.Type", "")).casefold() == "dialogbegin"
    }
    outgoing: dict[int, set[int]] = {object_id: set() for object_id in objects}
    incoming: dict[int, set[int]] = {object_id: set() for object_id in turns}
    links = project.data.get("Visual.Links", [])
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            begin = link.get("Begin")
            end = link.get("End")
            if begin in objects and end in objects:
                outgoing[begin].add(end)
                if end in turns:
                    incoming[end].add(begin)

    entry_states: dict[int, set[bool]] = {object_id: set() for object_id in turns}
    pending: list[tuple[int, bool]] = []
    for object_id in turns:
        parent = objects[object_id].get("Parent")
        if parent in dialog_sources:
            pending.append((object_id, True))
        elif parent not in (-1, None):
            pending.append((object_id, False))
        if not incoming[object_id]:
            pending.append((object_id, False))
        for source in incoming[object_id]:
            if source in dialog_sources:
                pending.append((object_id, True))
            elif source not in turns:
                pending.append((object_id, False))

    seen: set[tuple[int, bool]] = set()
    while pending:
        current, dialog_scoped = pending.pop()
        state = (current, dialog_scoped)
        if state in seen:
            continue
        seen.add(state)
        entry_states[current].add(dialog_scoped)
        for child in outgoing.get(current, ()):
            if child in turns:
                pending.append((child, dialog_scoped))
    return {
        object_id
        for object_id, states in entry_states.items()
        if states == {True}
    }


def _call_arguments(text: str, function_name: str) -> list[tuple[int, list[str]]]:
    """Return balanced argument lists for calls in already masked code."""

    pattern = re.compile(rf"\b{re.escape(function_name)}\s*\(", re.IGNORECASE)
    result: list[tuple[int, list[str]]] = []
    for match in pattern.finditer(text):
        depth = 1
        start = match.end()
        index = start
        argument_start = start
        arguments: list[str] = []
        while index < len(text) and depth:
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    arguments.append(text[argument_start:index].strip())
                    result.append((match.start(), arguments))
                    break
            elif char == "," and depth == 1:
                arguments.append(text[argument_start:index].strip())
                argument_start = index + 1
            index += 1
    return result


def _function_parameters(block: FunctionBlock) -> tuple[str, ...]:
    """Return normalized parameter names, accepting decompiler type prefixes."""

    header = FUNCTION_HEADER_RE.match(_mask_non_code(block.lines[0])) if block.lines else None
    if not header or not header.group(2).strip():
        return ()
    parameters: list[str] = []
    for declaration in header.group(2).split(","):
        names = IDENTIFIER_RE.findall(declaration)
        if not names:
            return ()
        parameters.append(names[-1].casefold())
    return tuple(parameters)


def _simple_identifier(expression: str) -> str | None:
    value = expression.strip().casefold()
    return value if IDENTIFIER_RE.fullmatch(value) else None


def _shared_tvars(project: RsonProject) -> set[str]:
    return {
        str(item.get("Name", "")).casefold()
        for item in project.iter_objects()
        if str(item.get("Type", "")).casefold() == "tvar" and str(item.get("Name", "")).strip()
    }


def _persistent_item_parameter_sinks(
    functions: dict[str, FunctionBlock],
    shared: set[str],
) -> dict[str, dict[int, set[str]]]:
    """Find helper parameters copied into shared TVars/arrays."""

    result: dict[str, dict[int, set[str]]] = {}
    assignment = re.compile(
        r"(?:\b(?:int|dword|str|float|double|bool)\s+)?"
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )
    indexed_assignment = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[[^]]+\]\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )
    for name, block in functions.items():
        parameters = _function_parameters(block)
        if not parameters:
            continue
        sinks: dict[int, set[str]] = {}
        for line in block.lines[1:]:
            masked = _mask_non_code(line)
            for match in assignment.finditer(masked):
                target = match.group(1).casefold()
                source = _simple_identifier(match.group(2))
                if target not in shared or source not in parameters:
                    continue
                sinks.setdefault(parameters.index(source), set()).add(target)
            for match in indexed_assignment.finditer(masked):
                target = match.group(1).casefold()
                source = _simple_identifier(match.group(2))
                if target in shared and source in parameters:
                    sinks.setdefault(parameters.index(source), set()).add(target)
            for _position, arguments in _call_arguments(masked, "ArrayAdd"):
                if len(arguments) < 2:
                    continue
                target = _simple_identifier(arguments[0])
                source = _simple_identifier(arguments[1])
                if target in shared and source in parameters:
                    sinks.setdefault(parameters.index(source), set()).add(target)
        if sinks:
            result[name] = sinks
    changed = True
    while changed:
        changed = False
        for name, block in functions.items():
            parameters = _function_parameters(block)
            if not parameters:
                continue
            for callee_name, callee_sinks in tuple(result.items()):
                for line in block.lines[1:]:
                    for _position, arguments in _call_arguments(
                        _mask_non_code(line), functions[callee_name].name
                    ):
                        for callee_index, targets in callee_sinks.items():
                            if callee_index >= len(arguments):
                                continue
                            actual = _simple_identifier(arguments[callee_index])
                            if actual not in parameters:
                                continue
                            caller_index = parameters.index(actual)
                            current = result.setdefault(name, {}).setdefault(caller_index, set())
                            before = len(current)
                            current.update(targets)
                            changed |= len(current) != before
    return result


def _raw_item_expression(expression: str, tainted: set[str]) -> bool:
    folded = _mask_non_code(expression).casefold()
    if re.search(r"\bid\s*\(", folded):
        return False
    if re.search(r"\b(?:createquestitem|idtoitem)\s*\(", folded):
        return True
    return any(re.search(rf"\b{re.escape(value)}\b", folded) for value in tainted)


def _lint_persistent_item_handles(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject transient Item references persisted beyond their current turn.

    RScript exposes Item values as engine objects.  Persisting the raw dword in
    a TVar/array keeps an address that can become invalid on a later turn.  A
    stable project must persist ``Id(item)`` and resolve it with ``IdToItem``.
    """

    shared = _shared_tvars(project)
    if not shared:
        return []
    helper_sinks = _persistent_item_parameter_sinks(functions, shared)
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    reported: set[tuple[str, int, str]] = set()
    assignment = re.compile(
        r"(?:\b(?:int|dword|str|float|double|bool)\s+)?"
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )
    indexed_assignment = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[[^]]+\]\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )

    def report(block: FunctionBlock, line_offset: int, target: str, evidence: str) -> None:
        key = (block.name.casefold(), line_offset, target)
        if key in reported:
            return
        reported.add(key)
        issues.append(
            RuntimeIssue(
                "error",
                "runtime-persistent-raw-item-handle",
                f"Сырая ссылка Item сохраняется в {target} между ходами; сохраните Id(item), затем восстанавливайте Item через IdToItem",
                path,
                f"{block.location} line {block.start_line + line_offset}",
                evidence.strip(),
            )
        )

    for block in functions.values():
        tainted: set[str] = set()
        for line_offset, line in enumerate(block.lines[1:], start=1):
            masked = _mask_non_code(line)
            matches = list(assignment.finditer(masked))
            for match in matches:
                target = match.group(1).casefold()
                expression = match.group(2).strip()
                raw = _raw_item_expression(expression, tainted)
                if target in shared and raw:
                    report(block, line_offset, target, line)
                if raw:
                    tainted.add(target)
                else:
                    tainted.discard(target)

            for match in indexed_assignment.finditer(masked):
                target = match.group(1).casefold()
                if target in shared and _raw_item_expression(match.group(2), tainted):
                    report(block, line_offset, target, line)

            for _position, arguments in _call_arguments(masked, "LinkItemToScript"):
                if arguments and (linked := _simple_identifier(arguments[0])):
                    tainted.discard(linked)

            for _position, arguments in _call_arguments(masked, "ArrayAdd"):
                if len(arguments) < 2:
                    continue
                target = _simple_identifier(arguments[0])
                if target in shared and _raw_item_expression(arguments[1], tainted):
                    report(block, line_offset, target, line)

            for helper_name, sinks in helper_sinks.items():
                for _position, arguments in _call_arguments(masked, functions[helper_name].name):
                    for parameter_index, targets in sinks.items():
                        if parameter_index >= len(arguments) or not _raw_item_expression(
                            arguments[parameter_index], tainted
                        ):
                            continue
                        for target in targets:
                            report(block, line_offset, target, line)
    return issues


_WORLD_OBJECT_ARGUMENT_TYPES: dict[str, dict[int, str]] = {
    "planettostar": {0: "planet"},
    "planetrace": {0: "planet"},
    "planeteco": {0: "planet"},
    "planetowner": {0: "planet"},
    "buywarrior": {0: "planet"},
    "starname": {0: "star"},
    "starowner": {0: "star"},
    "starbattle": {0: "star"},
    "starenemythreatlevel": {0: "star"},
    "starplanets": {0: "star"},
    "starships": {0: "star"},
    "starruins": {0: "star"},
    "starnearbystars": {0: "star"},
    "shipstar": {0: "ship"},
    "getshipplanet": {0: "ship"},
    "getshipruins": {0: "ship"},
    "shipout": {0: "ship"},
    "shipinhyperspace": {0: "ship"},
    "shipinnormalspace": {0: "ship"},
    "shipgetbad": {0: "ship"},
}

_WORLD_OBJECT_DIRECT_RESOLVERS = {
    "idtoplanet": "planet",
    "idtoship": "ship",
}

_WORLD_OBJECT_RESOLVER_GUIDANCE = {
    "planet": "IdToPlanet",
    "star": "локальную ограниченную функцию через GalaxyStars()/GalaxyStar(i)",
    "ship": "IdToShip",
}

_WORLD_OBJECT_RETURN_TYPES: dict[str, tuple[str, int]] = {
    "getshipplanet": ("planet", 1),
    "starplanets": ("planet", 2),
    "planetpirateclan": ("planet", 0),
    "idtoplanet": ("planet", 1),
    "shipstar": ("star", 1),
    "planettostar": ("star", 1),
    "constar": ("star", 2),
    "galaxystar": ("star", 1),
    "starnearbystars": ("star", 2),
    "starships": ("ship", 2),
    "groupship": ("ship", 2),
    "idtoship": ("ship", 1),
    "player": ("ship", 0),
}


def _line_call_sites(masked: str) -> list[tuple[int, str, list[str]]]:
    calls: list[tuple[int, str, list[str]]] = []
    for match in CALL_RE.finditer(masked):
        name = match.group(1)
        if name.casefold() in CONTROL_CALLS:
            continue
        parsed = _call_arguments(masked[match.start():], name)
        if parsed:
            calls.append((match.start(), name.casefold(), parsed[0][1]))
    return sorted(calls)


def _proven_star_id_resolver(block: FunctionBlock) -> bool:
    """Prove a bounded local replacement for the unavailable IdToStar call."""

    parameters = _function_parameters(block)
    if len(parameters) != 1:
        return False
    identifier = re.escape(parameters[0])
    body = _mask_non_code(block.body_text)
    loop_pattern = re.compile(
        r"\bfor\s*\(\s*(?:int\s+)?(?P<cursor>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*0\s*;"
        r"\s*(?P=cursor)\s*<\s*GalaxyStars\s*\(\s*\)\s*;\s*"
        r"(?:(?P=cursor)\s*=\s*(?P=cursor)\s*\+\s*1|(?P=cursor)\s*\+\+|\+\+\s*(?P=cursor))\s*\)",
        re.IGNORECASE,
    )
    loop = loop_pattern.search(body)
    if not loop:
        return False
    open_brace = body.find("{", loop.end())
    if open_brace < 0:
        return False
    depth = 0
    close_brace = -1
    for position in range(open_brace, len(body)):
        if body[position] == "{":
            depth += 1
        elif body[position] == "}":
            depth -= 1
            if depth == 0:
                close_brace = position
                break
    if close_brace < 0:
        return False
    loop_body = body[open_brace + 1:close_brace]
    cursor = re.escape(loop.group("cursor"))
    candidate_pattern = re.compile(
        rf"\b(?:dword\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*GalaxyStar\s*\(\s*{cursor}\s*\)",
        re.IGNORECASE,
    )
    candidate_match = candidate_pattern.search(loop_body)
    if not candidate_match:
        return False
    candidate = re.escape(candidate_match.group(1))
    comparison = re.search(
        rf"(?:Id\s*\(\s*{candidate}\s*\)\s*==\s*{identifier}\b|"
        rf"\b{identifier}\s*==\s*Id\s*\(\s*{candidate}\s*\))",
        loop_body[candidate_match.end():],
        re.IGNORECASE,
    )
    result_match = re.search(
        rf"\bresult\s*=(?!=)\s*{candidate}\s*;",
        loop_body[candidate_match.end():],
        re.IGNORECASE,
    )
    zero_match = re.search(r"\bresult\s*=(?!=)\s*0\s*;", body, re.IGNORECASE)
    return bool(
        comparison
        and result_match
        and comparison.start() <= result_match.start()
        and zero_match
        and zero_match.start() < loop.start()
    )


def _world_object_resolver_kinds(
    functions: dict[str, FunctionBlock],
) -> dict[str, str]:
    resolvers = dict(_WORLD_OBJECT_DIRECT_RESOLVERS)
    resolvers.update(
        {
            name: "star"
            for name, block in functions.items()
            if _proven_star_id_resolver(block)
        }
    )
    return resolvers


def _code_line_contexts(lines: list[str]) -> tuple[list[int], list[str | None]]:
    depths: list[int] = []
    contexts: list[str | None] = []
    depth = 0
    function_name: str | None = None
    function_opened = False
    function_depth = 0
    for line in lines:
        masked = _mask_non_code(line)
        match = FUNCTION_RE.match(masked) if function_name is None else None
        if match:
            function_name = match.group(1).casefold()
            function_opened = False
            function_depth = 0
        depths.append(depth)
        contexts.append(function_name)
        delta = masked.count("{") - masked.count("}")
        depth += delta
        if function_name is not None:
            function_opened |= "{" in masked
            function_depth += delta
            if function_opened and function_depth <= 0:
                function_name = None
    return depths, contexts


def _has_fresh_world_assignment(
    lines: list[str],
    line_index: int,
    variable: str,
    kind: str,
    depths: list[int],
    contexts: list[str | None],
) -> bool:
    """Prove a same-invocation scratch assignment before a typed object use."""

    assignment = re.compile(rf"\b{re.escape(variable)}\s*=(?!=)", re.IGNORECASE)
    for previous_index in range(line_index - 1, -1, -1):
        if contexts[previous_index] != contexts[line_index]:
            continue
        masked = _mask_non_code(lines[previous_index])
        match = assignment.search(masked)
        if not match:
            continue
        if depths[previous_index] > depths[line_index]:
            return False
        prefix = masked[:match.start()].strip().casefold()
        if prefix.startswith(("if", "while", "for", "switch")):
            return False
        rhs = masked[match.end():].lstrip()
        call_match = CALL_RE.match(rhs)
        if not call_match:
            return False
        call = call_match.group(1)
        return_type = _WORLD_OBJECT_RETURN_TYPES.get(call.casefold())
        if not return_type or return_type[0] != kind:
            return False
        parsed = _call_arguments(rhs, call)
        return bool(parsed and len(parsed[0][1]) >= return_type[1])
    return False


def _world_object_parameter_requirements(
    functions: dict[str, FunctionBlock],
) -> dict[str, dict[int, set[str]]]:
    """Infer Planet/Star/Ship parameter roles through local helper calls."""

    requirements: dict[str, dict[int, set[str]]] = {name: {} for name in functions}
    sites = {name: _function_call_sites(block) for name, block in functions.items()}
    changed = True
    while changed:
        changed = False
        for name, block in functions.items():
            parameters = _function_parameters(block)
            if not parameters:
                continue
            for _line, _depth, call, arguments in sites[name]:
                constraints: dict[int, set[str]] = {}
                for index, value in _WORLD_OBJECT_ARGUMENT_TYPES.get(call, {}).items():
                    constraints.setdefault(index, set()).add(value)
                for index, values in requirements.get(call, {}).items():
                    constraints.setdefault(index, set()).update(values)
                for argument_index, kinds in constraints.items():
                    if argument_index >= len(arguments):
                        continue
                    actual = _simple_identifier(arguments[argument_index])
                    if actual not in parameters:
                        continue
                    parameter_index = parameters.index(actual)
                    current = requirements[name].setdefault(parameter_index, set())
                    before = len(current)
                    current.update(kinds)
                    changed |= len(current) != before
    return requirements


def _top_level_code_lines(
    project: RsonProject,
) -> list[tuple[int | None, str, int, str]]:
    result: list[tuple[int | None, str, int, str]] = []
    for item in project.iter_objects():
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        for field in ("Code", "ActCode", "LinkCode"):
            value = item.get(field)
            if not isinstance(value, list):
                continue
            in_function = False
            opened = False
            depth = 0
            for line_number, raw_line in enumerate(value, start=1):
                line = str(raw_line)
                masked = _mask_non_code(line)
                if not in_function and FUNCTION_RE.match(masked):
                    in_function = True
                    opened = False
                    depth = 0
                if in_function:
                    if "{" in masked:
                        opened = True
                    depth += masked.count("{") - masked.count("}")
                    if opened and depth <= 0:
                        in_function = False
                    continue
                result.append((object_id, field, line_number, line))
    return result


def _world_object_uses(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
    requirements: dict[str, dict[int, set[str]]],
) -> dict[tuple[str, str], tuple[str, str]]:
    shared = _shared_tvars(project)
    uses: dict[tuple[str, str], tuple[str, str]] = {}
    for item in project.iter_objects():
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        containers: list[tuple[str, list[str]]] = []
        for field, value in item.items():
            if field in {"Code", "ActCode", "LinkCode"} and isinstance(value, list):
                containers.append((field, [str(line) for line in value]))
            elif field.casefold().endswith("code") and isinstance(value, str):
                containers.append((field, value.splitlines()))
        for field, lines in containers:
            depths, contexts = _code_line_contexts(lines)
            for line_number, line in enumerate(lines, start=1):
                masked = _mask_non_code(line)
                for _position, call, arguments in _line_call_sites(masked):
                    constraints: dict[int, set[str]] = {}
                    for index, value in _WORLD_OBJECT_ARGUMENT_TYPES.get(call, {}).items():
                        constraints.setdefault(index, set()).add(value)
                    for index, values in requirements.get(call, {}).items():
                        constraints.setdefault(index, set()).update(values)
                    for argument_index, kinds in constraints.items():
                        if argument_index >= len(arguments):
                            continue
                        actual = _simple_identifier(arguments[argument_index])
                        if actual not in shared:
                            continue
                        for kind in kinds:
                            if _has_fresh_world_assignment(
                                lines,
                                line_number - 1,
                                actual,
                                kind,
                                depths,
                                contexts,
                            ):
                                continue
                            uses.setdefault(
                                (actual, kind),
                                (f"object #{object_id} {field}:{line_number}", line.strip()),
                            )
    return uses


def _lint_persistent_world_object_handles(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Require persistent world objects to be refreshed from stable IDs.

    Planet/Star/Ship references stored in TVars can survive in a save while the
    underlying engine object does not.  The safe migration pattern clears the
    old reference first, resolves a persistent ID through a proven resolver,
    and stores the ID whenever a new reference is selected.
    """

    shared = _shared_tvars(project)
    if not shared:
        return []
    requirements = _world_object_parameter_requirements(functions)
    uses = _world_object_uses(project, functions, requirements)
    if not uses:
        return []

    all_code = "\n".join(
        _mask_non_code(str(line))
        for item in project.iter_objects()
        for field, value in item.items()
        for line in (
            value
            if field in {"Code", "ActCode", "LinkCode"} and isinstance(value, list)
            else value.splitlines()
            if field.casefold().endswith("code") and isinstance(value, str)
            else []
        )
    )
    graph = _call_graph(functions)
    top_level_calls = {
        call.casefold()
        for _object_id, _field, _line_number, line in _top_level_code_lines(project)
        for call in _calls(line)
        if call.casefold() in functions
    }
    reachable_restorers = _reachable(top_level_calls, graph)
    restorations: dict[tuple[str, str], list[tuple[str, bool, bool, bool]]] = {}
    resolver_kinds = _world_object_resolver_kinds(functions)
    resolver_names = "|".join(
        re.escape(functions[name].name if name in functions else name)
        for name in sorted(resolver_kinds, key=len, reverse=True)
    )
    resolver_pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*"
        rf"({resolver_names})\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)"
        r"\s*(?:,[^()]*)?\)",
        re.IGNORECASE,
    )
    for function_name, block in functions.items():
        depth = 0
        depths: list[int] = []
        for line in block.lines:
            depths.append(depth)
            depth += _brace_delta(line)
        for line_offset, line in enumerate(block.lines[1:], start=1):
            masked = _mask_non_code(line)
            for match in resolver_pattern.finditer(masked):
                target = match.group(1).casefold()
                resolver = match.group(2).casefold()
                identifier = match.group(3).casefold()
                if target not in shared:
                    continue
                kind = resolver_kinds.get(resolver, "")
                if not kind:
                    continue
                cleared = False
                for previous_offset, previous in enumerate(block.lines[1:line_offset], start=1):
                    if depths[previous_offset] != 1:
                        continue
                    if target in {
                        value.casefold()
                        for value in ASSIGN_ZERO_RE.findall(_mask_non_code(previous))
                    }:
                        cleared = True
                prefix = masked[:match.start()]
                if depths[line_offset] == 1 and target in {
                    value.casefold() for value in ASSIGN_ZERO_RE.findall(prefix)
                }:
                    cleared = True
                id_is_shared = identifier in shared
                id_is_stored = bool(
                    re.search(
                        rf"\b{re.escape(identifier)}\s*=(?!=)\s*Id\s*\(\s*{re.escape(target)}\s*\)",
                        all_code,
                        re.IGNORECASE,
                    )
                )
                restorations.setdefault((target, kind), []).append(
                    (identifier, cleared, id_is_shared and id_is_stored, function_name in reachable_restorers)
                )

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    labels = {"planet": "Planet", "star": "Star", "ship": "Ship"}
    for key, (location, evidence) in sorted(uses.items()):
        variable, kind = key
        candidates = restorations.get(key, [])
        valid = any(cleared and stored and reachable for _id, cleared, stored, reachable in candidates)
        if valid:
            continue
        if not candidates:
            reason = f"нет восстановления через {_WORLD_OBJECT_RESOLVER_GUIDANCE[kind]}"
        elif not any(cleared for _id, cleared, _stored, _reachable in candidates):
            reason = "старая ссылка не обнуляется до условного восстановления"
        elif not any(stored for _id, _cleared, stored, _reachable in candidates):
            reason = "ID не хранится в общем TVar через Id(object)"
        else:
            reason = "функция восстановления не вызывается из исполняемого кода объекта"
        issues.append(
            RuntimeIssue(
                "error",
                "runtime-persistent-world-object-handle",
                f"Общий TVar {variable} используется как {labels[kind]}, но небезопасен для сохранений: {reason}; храните числовой ID, сначала обнуляйте старую ссылку и используйте доказанный восстановитель ({_WORLD_OBJECT_RESOLVER_GUIDANCE[kind]})",
                path,
                location,
                evidence,
            )
        )
    return issues


_SHIP_EFFECT_CALLS = {
    "getitemfromship": "mutates",
    "ordertakeoff": "takes_off",
    "shipout": "ships_out",
    "shipdestroy": "ships_out",
}


def _function_call_sites(block: FunctionBlock) -> list[tuple[int, int, str, list[str]]]:
    """Return line/depth/call/args records in source order."""

    result: list[tuple[int, int, str, list[str]]] = []
    depth = 0
    for line_offset, line in enumerate(block.lines):
        masked = _mask_non_code(line)
        depth_before = depth
        for _position, name, arguments in _line_call_sites(masked):
            result.append((line_offset, depth_before, name, arguments))
        depth += masked.count("{") - masked.count("}")
    return result


def _ship_effect_summaries(
    functions: dict[str, FunctionBlock],
) -> dict[str, dict[str, set[int]]]:
    summaries = {
        name: {"mutates": set(), "takes_off": set(), "ships_out": set()}
        for name in functions
    }
    sites = {name: _function_call_sites(block) for name, block in functions.items()}
    changed = True
    while changed:
        changed = False
        for name, block in functions.items():
            parameters = _function_parameters(block)
            if not parameters:
                continue
            for _line, _depth, call, arguments in sites[name]:
                direct_effect = _SHIP_EFFECT_CALLS.get(call)
                if direct_effect and arguments:
                    actual = _simple_identifier(arguments[0])
                    if actual in parameters:
                        index = parameters.index(actual)
                        if index not in summaries[name][direct_effect]:
                            summaries[name][direct_effect].add(index)
                            changed = True
                callee = summaries.get(call)
                if callee is None:
                    continue
                for effect, parameter_indexes in callee.items():
                    for parameter_index in parameter_indexes:
                        if parameter_index >= len(arguments):
                            continue
                        actual = _simple_identifier(arguments[parameter_index])
                        if actual not in parameters:
                            continue
                        index = parameters.index(actual)
                        if index not in summaries[name][effect]:
                            summaries[name][effect].add(index)
                            changed = True
    return summaries


def _direct_detached_item_free_sites(
    block: FunctionBlock,
) -> list[tuple[int, int, str]]:
    """Return FreeItem sites for items detached and unlinked in this call."""

    parameters = _function_parameters(block)
    origins: dict[str, str] = {}
    released: set[str] = set()
    result: list[tuple[int, int, str]] = []
    assignment = re.compile(
        r"(?:\b(?:int|dword|unknown)\s+)?\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )
    sites = _function_call_sites(block)
    sites_by_line: dict[int, list[tuple[int, str, list[str]]]] = {}
    for line_offset, depth, call, arguments in sites:
        sites_by_line.setdefault(line_offset, []).append((depth, call, arguments))
    for line_offset, line in enumerate(block.lines[1:], start=1):
        masked = _mask_non_code(line)
        for match in assignment.finditer(masked):
            target = match.group(1).casefold()
            get_calls = _call_arguments(match.group(2), "GetItemFromShip")
            if get_calls and get_calls[0][1]:
                ship = _simple_identifier(get_calls[0][1][0])
                if ship:
                    origins[target] = ship
            else:
                alias = _simple_identifier(match.group(2))
                if alias in origins:
                    origins[target] = origins[alias]
                else:
                    origins.pop(target, None)
                    released.discard(target)
        for depth, call, arguments in sites_by_line.get(line_offset, []):
            if not arguments:
                continue
            item = _simple_identifier(arguments[0])
            if call == "releaseitemfromscript" and item in origins:
                released.add(item)
            elif call == "freeitem" and item in origins and item in released:
                result.append((line_offset, depth, origins[item]))
    return result


def _detached_item_free_summaries(
    functions: dict[str, FunctionBlock],
) -> dict[str, set[int]]:
    summaries: dict[str, set[int]] = {name: set() for name in functions}
    sites = {name: _function_call_sites(block) for name, block in functions.items()}
    for name, block in functions.items():
        parameters = _function_parameters(block)
        for _line, _depth, ship in _direct_detached_item_free_sites(block):
            if ship in parameters:
                summaries[name].add(parameters.index(ship))
    changed = True
    while changed:
        changed = False
        for name, block in functions.items():
            parameters = _function_parameters(block)
            if not parameters:
                continue
            for _line, _depth, call, arguments in sites[name]:
                for parameter_index in summaries.get(call, set()):
                    if parameter_index >= len(arguments):
                        continue
                    actual = _simple_identifier(arguments[parameter_index])
                    if actual not in parameters:
                        continue
                    caller_index = parameters.index(actual)
                    if caller_index not in summaries[name]:
                        summaries[name].add(caller_index)
                        changed = True
    return summaries


def _lint_repeated_detached_item_free(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject repeated detach/unlink/free mutations in one runtime invocation."""

    summaries = _detached_item_free_summaries(functions)
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    reported: set[tuple[str, str]] = set()
    for name, block in functions.items():
        affected: dict[str, list[tuple[int, int, str]]] = {}
        for line_offset, depth, ship in _direct_detached_item_free_sites(block):
            affected.setdefault(ship, []).append((line_offset, depth, block.lines[line_offset]))
        for line_offset, depth, call, arguments in _function_call_sites(block):
            if line_offset == 0:
                continue
            for parameter_index in summaries.get(call, set()):
                if parameter_index >= len(arguments):
                    continue
                actual = _simple_identifier(arguments[parameter_index])
                if actual:
                    affected.setdefault(actual, []).append(
                        (line_offset, depth, block.lines[line_offset])
                    )
        for ship, sites in affected.items():
            repeated = len(sites) > 1 or any(depth > 1 for _line, depth, _evidence in sites)
            if not repeated or (name, ship) in reported:
                continue
            reported.add((name, ship))
            line_offset, _depth, evidence = sites[1] if len(sites) > 1 else sites[0]
            issues.append(
                RuntimeIssue(
                    "error",
                    "runtime-item-list-mutated-during-star-act",
                    f"{block.name} многократно отделяет, освобождает из скрипта и уничтожает предметы корабля {ship} в одном вызове; это может повредить текущий TStar.ScriptShipsAndItemsAct. Отложите массовое удаление за границу хода",
                    path,
                    f"{block.location} line {block.start_line + line_offset}",
                    evidence.strip(),
                )
            )
    return issues


_ORDER_MUTATION_CALLS = {
    "ordernone": 0,
    "orderfollowship": 0,
    "orderjump": 0,
    "orderlanding": 0,
    "shipsetbad": 0,
    "orderlock": 0,
}


def _order_call_mutates(call: str, arguments: list[str]) -> bool:
    if call == "orderlock" and len(arguments) < 2:
        return False
    if call == "shipsetbad" and len(arguments) < 2:
        return False
    return call in _ORDER_MUTATION_CALLS and bool(arguments)


def _line_depths(lines: tuple[str, ...]) -> list[int]:
    result: list[int] = []
    depth = 0
    for line in lines:
        result.append(depth)
        depth += _brace_delta(line)
    return result


def _variable_reassigned(
    lines: tuple[str, ...],
    variable: str,
    start: int,
    end: int,
) -> bool:
    assignment = re.compile(rf"\b{re.escape(variable)}\s*=(?!=)", re.IGNORECASE)
    return any(assignment.search(_mask_non_code(lines[index])) for index in range(start, end))


def _has_exit_transit_condition(text: str, variable: str) -> bool:
    """Recognize one balanced if(condition) whose taken path exits."""

    masked = _mask_non_code(text)
    match = re.search(r"\bif\s*\(", masked, re.IGNORECASE)
    if not match:
        return False
    start = match.end()
    depth = 1
    close = -1
    for index in range(start, len(masked)):
        if masked[index] == "(":
            depth += 1
        elif masked[index] == ")":
            depth -= 1
            if depth == 0:
                close = index
                break
    if close < 0:
        return False
    condition = masked[start:close]
    wanted = re.escape(variable)
    positive_hyper = False
    for hyper in re.finditer(
        rf"\bShipInHyperSpace\s*\(\s*{wanted}\s*\)", condition, re.IGNORECASE
    ):
        prefix = condition[: hyper.start()].rstrip()
        if not prefix.endswith("!"):
            positive_hyper = True
            break
    non_normal = bool(
        re.search(
            rf"!\s*ShipInNormalSpace\s*\(\s*{wanted}\s*\)",
            condition,
            re.IGNORECASE,
        )
    )
    if not (positive_hyper or non_normal):
        return False
    rest = masked[close + 1 :].lstrip()
    if re.match(r"(?:exit|return|continue)\b", rest, re.IGNORECASE):
        return True
    if not rest.startswith("{"):
        return False
    depth = 0
    block_close = -1
    for index, char in enumerate(rest):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block_close = index
                break
    if block_close < 0:
        return False
    body = rest[1:block_close].strip()
    return bool(re.search(r"(?:exit|return|continue)\s*;?\s*$", body, re.IGNORECASE))


def _has_transit_guard_before(
    block: FunctionBlock,
    line_offset: int,
    variable: str,
) -> bool:
    """Prove a dominating early exit or enclosing normal-space branch."""

    lines = block.lines
    depths = _line_depths(lines)
    wanted = re.escape(variable)
    for index in range(1, line_offset):
        window = "\n".join(lines[index:line_offset])
        if (
            _has_exit_transit_condition(window, variable)
            and depths[index] <= depths[line_offset]
            and not _variable_reassigned(lines, variable, index + 1, line_offset + 1)
        ):
            return True

    for index in range(1, line_offset + 1):
        masked = _mask_non_code(lines[index])
        if "if" not in masked.casefold() or "{" not in masked:
            continue
        positive = re.search(
            rf"!\s*ShipInHyperSpace\s*\(\s*{wanted}\s*\)", masked, re.IGNORECASE
        ) or re.search(
            rf"\bShipInNormalSpace\s*\(\s*{wanted}\s*\)", masked, re.IGNORECASE
        )
        if "||" in masked or not positive or _brace_block_end(lines, index) < line_offset:
            continue
        if not _variable_reassigned(lines, variable, index + 1, line_offset + 1):
            return True

    same_line_prefix = _mask_non_code(lines[line_offset])
    direct_call_positions = [
        position
        for position, call, arguments in _line_call_sites(same_line_prefix)
        if _order_call_mutates(call, arguments)
        and arguments
        and _simple_identifier(arguments[0]) == variable
    ]
    if direct_call_positions:
        prefix = same_line_prefix[: min(direct_call_positions)]
        if re.search(
            rf"!\s*ShipInHyperSpace\s*\(\s*{wanted}\s*\)", prefix, re.IGNORECASE
        ) or re.search(
            rf"\bShipInNormalSpace\s*\(\s*{wanted}\s*\)", prefix, re.IGNORECASE
        ):
            return True
    return False


def _has_late_transit_guard(
    block: FunctionBlock,
    line_offset: int,
    variable: str,
) -> bool:
    depths = _line_depths(block.lines)
    origin_depth = depths[line_offset]
    for index in range(line_offset + 1, len(block.lines)):
        masked_line = _mask_non_code(block.lines[index])
        if depths[index] <= origin_depth and re.fullmatch(
            r"\s*(?:exit|return|continue)\s*;?\s*", masked_line, re.IGNORECASE
        ):
            return False
        window = "\n".join(block.lines[index : min(len(block.lines), index + 8)])
        if _has_exit_transit_condition(window, variable):
            return True
    return False


def _unguarded_order_summaries(
    functions: dict[str, FunctionBlock],
) -> dict[str, set[int]]:
    summaries: dict[str, set[int]] = {name: set() for name in functions}
    sites = {name: _function_call_sites(block) for name, block in functions.items()}
    changed = True
    while changed:
        changed = False
        for name, block in functions.items():
            parameters = _function_parameters(block)
            if not parameters:
                continue
            for line_offset, _depth, call, arguments in sites[name]:
                if line_offset == 0:
                    continue
                affected: list[str] = []
                if _order_call_mutates(call, arguments):
                    actual = _simple_identifier(arguments[_ORDER_MUTATION_CALLS[call]])
                    if actual:
                        affected.append(actual)
                for parameter_index in summaries.get(call, set()):
                    if parameter_index < len(arguments):
                        actual = _simple_identifier(arguments[parameter_index])
                        if actual:
                            affected.append(actual)
                for actual in affected:
                    if actual not in parameters or _has_transit_guard_before(
                        block, line_offset, actual
                    ):
                        continue
                    caller_index = parameters.index(actual)
                    if caller_index not in summaries[name]:
                        summaries[name].add(caller_index)
                        changed = True
    return summaries


def _lint_order_rewrite_before_hyperspace_guard(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Reject an order mutation placed before its own hyperspace barrier."""

    summaries = _unguarded_order_summaries(functions)
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    reported: set[tuple[str, str]] = set()
    for name, block in functions.items():
        for line_offset, _depth, call, arguments in _function_call_sites(block):
            if line_offset == 0:
                continue
            affected: list[str] = []
            if _order_call_mutates(call, arguments):
                actual = _simple_identifier(arguments[_ORDER_MUTATION_CALLS[call]])
                if actual:
                    affected.append(actual)
            for parameter_index in summaries.get(call, set()):
                if parameter_index < len(arguments):
                    actual = _simple_identifier(arguments[parameter_index])
                    if actual:
                        affected.append(actual)
            for actual in affected:
                if (name, actual) in reported or _has_transit_guard_before(
                    block, line_offset, actual
                ):
                    continue
                if not _has_late_transit_guard(block, line_offset, actual):
                    continue
                reported.add((name, actual))
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-order-rewrite-in-hyperspace",
                        f"{block.name} меняет приказ/боевую цель корабля {actual} до проверки ShipInHyperSpace; поздний guard не предотвращает отмену незавершённого прыжка. Перенесите transit-barrier перед первым Order*/ShipSetBad",
                        path,
                        f"{block.location} line {block.start_line + line_offset}",
                        block.lines[line_offset].strip(),
                    )
                )
    return issues


def _group_key(expression: str) -> str:
    return _simple_identifier(expression) or re.sub(r"\s+", "", expression).casefold()


def _lint_post_group_mutation_dereference(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
    summaries: dict[str, dict[str, set[int]]],
) -> list[RuntimeIssue]:
    """Reject group/ship reads after a helper mutates the same live member."""

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    assignment = re.compile(
        r"(?:\b(?:int|dword)\s+)?\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )
    group_read_calls = {"groupcount", "groupship", "grouptoship"}
    reported: set[tuple[str, str]] = set()
    for name, block in functions.items():
        aliases: dict[str, str] = {}
        tainted_groups: dict[str, int] = {}
        tainted_ships: dict[str, int] = {}
        depth = 0
        for line_offset, line in enumerate(block.lines):
            masked = _mask_non_code(line)
            depth_before = depth
            depth += masked.count("{") - masked.count("}")
            if line_offset and re.fullmatch(
                r"\s*(?:exit|return|continue)\s*;?\s*", masked, re.IGNORECASE
            ):
                tainted_groups = {
                    key: origin for key, origin in tainted_groups.items() if origin < depth_before
                }
                tainted_ships = {
                    key: origin for key, origin in tainted_ships.items() if origin < depth_before
                }
                continue

            for match in assignment.finditer(masked):
                target = match.group(1).casefold()
                expression = match.group(2)
                group_calls = _call_arguments(expression, "GroupShip") or _call_arguments(
                    expression, "GroupToShip"
                )
                if group_calls and group_calls[0][1]:
                    aliases[target] = _group_key(group_calls[0][1][0])
                else:
                    alias = _simple_identifier(expression)
                    if alias in aliases:
                        aliases[target] = aliases[alias]

            for _position, call, arguments in _line_call_sites(masked):
                if call in group_read_calls and arguments:
                    group = _group_key(arguments[0])
                    if group in tainted_groups and (name, group) not in reported:
                        reported.add((name, group))
                        issues.append(
                            RuntimeIssue(
                                "error",
                                "runtime-post-group-mutation-dereference",
                                f"{block.name} повторно читает группу {group} после изменения её живого корабля в том же вызове; завершите внешний обработчик и продолжите на следующем Turn",
                                path,
                                f"{block.location} line {block.start_line + line_offset}",
                                line.strip(),
                            )
                        )

                if call in _WORLD_OBJECT_ARGUMENT_TYPES and arguments:
                    ship = _simple_identifier(arguments[0])
                    if (
                        ship in tainted_ships
                        and "ship" in _WORLD_OBJECT_ARGUMENT_TYPES[call].values()
                        and (name, ship) not in reported
                    ):
                        reported.add((name, ship))
                        issues.append(
                            RuntimeIssue(
                                "error",
                                "runtime-post-group-mutation-dereference",
                                f"{block.name} разыменовывает старый Ship handle {ship} после изменения/выхода корабля в том же вызове",
                                path,
                                f"{block.location} line {block.start_line + line_offset}",
                                line.strip(),
                            )
                        )

                effects_by_actual: dict[str, set[str]] = {}
                direct_effect = _SHIP_EFFECT_CALLS.get(call)
                if direct_effect and arguments:
                    actual = _simple_identifier(arguments[0])
                    if actual:
                        effects_by_actual.setdefault(actual, set()).add(direct_effect)
                for effect, parameter_indexes in summaries.get(call, {}).items():
                    for parameter_index in parameter_indexes:
                        if parameter_index >= len(arguments):
                            continue
                        actual = _simple_identifier(arguments[parameter_index])
                        if actual:
                            effects_by_actual.setdefault(actual, set()).add(effect)
                for actual, effects in effects_by_actual.items():
                    if effects & {"takes_off", "ships_out"}:
                        tainted_ships[actual] = depth_before
                    if effects & {"mutates", "takes_off", "ships_out"} and actual in aliases:
                        tainted_groups[aliases[actual]] = depth_before
    return issues


def _lint_cleanup_without_turn_gate(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
    summaries: dict[str, dict[str, set[int]]],
) -> list[RuntimeIssue]:
    """Warn when a Turn cleanup relies on exit but has no date throttle."""

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for container in _iter_code_containers(project):
        if container.field != "Code" or container.code_type != "turn":
            continue
        depths, contexts = _code_line_contexts(list(container.lines))
        top_lines = [
            _mask_non_code(line) if contexts[index] is None else ""
            for index, line in enumerate(container.lines)
        ]
        text = "\n".join(top_lines)
        timer_guard = re.compile(
            r"\bif\s*\(\s*(?:"
            r"CurTurn\s*\(\s*\)\s*<\s*(?P<right>[A-Za-z_][A-Za-z0-9_]*)|"
            r"(?P<left>[A-Za-z_][A-Za-z0-9_]*)\s*>\s*CurTurn\s*\(\s*\)"
            r")\s*\)\s*(?:\{\s*)?exit\b",
            re.IGNORECASE,
        )
        timers = {
            (match.group("right") or match.group("left")).casefold()
            for match in timer_guard.finditer(text)
        }

        for index, line in enumerate(container.lines):
            if contexts[index] is not None:
                continue
            masked = top_lines[index]
            mutations: list[str] = []
            for _position, call, arguments in _line_call_sites(masked):
                if call in {"shipout", "shipdestroy"} and arguments:
                    mutations.append(call)
                elif summaries.get(call, {}).get("ships_out"):
                    mutations.append(call)
            if not mutations:
                continue
            following = "\n".join(top_lines[index : min(len(top_lines), index + 8)])
            if not re.search(r"\b(?:exit|return)\b", following, re.IGNORECASE):
                continue
            gate_window = "\n".join(
                top_lines[max(0, index - 24) : min(len(top_lines), index + 8)]
            )
            throttled = any(
                re.search(
                    rf"\b{re.escape(timer)}\s*=(?!=)\s*CurTurn\s*\(\s*\)\s*\+\s*(?:1\b|max\s*\(\s*1\b)",
                    gate_window,
                    re.IGNORECASE,
                )
                for timer in timers
            )
            if throttled:
                continue
            issues.append(
                RuntimeIssue(
                    "warning",
                    "runtime-cleanup-without-turn-gate",
                    "Turn-cleanup удаляет/выводит корабль и делает exit, но не имеет парного барьера CurTurn()<next_turn и next_turn=CurTurn()+1; глобальный обработчик может войти повторно в ту же игровую дату",
                    path,
                    f"{container.location}:{index + 1}",
                    line.strip(),
                )
            )
    return issues


def _has_safe_follow_context(
    lines: tuple[str, ...],
    line_offset: int,
    actor: str,
    target: str,
) -> bool:
    prefix = _mask_non_code("\n".join(lines[: line_offset + 1]))
    actor_re = re.escape(actor)
    target_re = re.escape(target)
    target_normal = re.search(
        rf"\bShipInNormalSpace\s*\(\s*{target_re}\s*\)", prefix, re.IGNORECASE
    )
    actor_normal = re.search(
        rf"\bShipInNormalSpace\s*\(\s*{actor_re}\s*\)", prefix, re.IGNORECASE
    )
    same_star = re.search(
        rf"ShipStar\s*\(\s*{actor_re}\s*\)\s*==\s*ShipStar\s*\(\s*{target_re}\s*\)|"
        rf"ShipStar\s*\(\s*{target_re}\s*\)\s*==\s*ShipStar\s*\(\s*{actor_re}\s*\)",
        prefix,
        re.IGNORECASE,
    )
    mismatch_exit = re.search(
        rf"ShipStar\s*\(\s*{actor_re}\s*\)\s*!=\s*ShipStar\s*\(\s*{target_re}\s*\)[^;{{}}]*(?:\)|&&|\|\|)[^{{}}]*(?:exit|continue|return)|"
        rf"ShipStar\s*\(\s*{target_re}\s*\)\s*!=\s*ShipStar\s*\(\s*{actor_re}\s*\)[^;{{}}]*(?:\)|&&|\|\|)[^{{}}]*(?:exit|continue|return)",
        prefix,
        re.IGNORECASE | re.DOTALL,
    )
    return bool(target_normal and actor_normal and (same_star or mismatch_exit))


def _lint_stale_shipgetbad_follow(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
) -> list[RuntimeIssue]:
    """Warn when a transient ShipGetBad target is propagated without validation."""

    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    assignment = re.compile(
        r"(?:\b(?:int|dword)\s+)?\b([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([^;]+)",
        re.IGNORECASE,
    )
    for container in _iter_code_containers(project):
        _depths, contexts = _code_line_contexts(list(container.lines))
        targets_by_scope: dict[str, dict[str, str]] = {}
        reported: set[tuple[str, str]] = set()
        for line_offset, line in enumerate(container.lines):
            scope = contexts[line_offset] or "<top>"
            targets = targets_by_scope.setdefault(scope, {})
            masked = _mask_non_code(line)
            for match in assignment.finditer(masked):
                target = match.group(1).casefold()
                calls = _call_arguments(match.group(2), "ShipGetBad")
                if calls and calls[0][1] and (
                    protected := _simple_identifier(calls[0][1][0])
                ):
                    targets[target] = protected
                elif match.group(2).strip() == "0":
                    targets.pop(target, None)
            for _position, call, arguments in _line_call_sites(masked):
                if call == "groupsetbad" and len(arguments) >= 2:
                    target = _simple_identifier(arguments[1])
                    if target in targets and (scope, target) not in reported:
                        reported.add((scope, target))
                        issues.append(
                            RuntimeIssue(
                                "warning",
                                "runtime-stale-shipgetbad-follow",
                                f"Цель {target} из ShipGetBad без проверки распространяется на всю группу через GroupSetBad; очистите stale target при разрыве систем и валидируйте normal-space/ShipStar перед follow",
                                path,
                                f"{container.location}:{line_offset + 1}",
                                line.strip(),
                            )
                        )
                if call != "orderfollowship" or len(arguments) < 2:
                    continue
                actor = _simple_identifier(arguments[0])
                target = _simple_identifier(arguments[1])
                if not actor or target not in targets or (scope, target) in reported:
                    continue
                if _has_safe_follow_context(container.lines, line_offset, actor, target):
                    continue
                reported.add((scope, target))
                issues.append(
                    RuntimeIssue(
                        "warning",
                        "runtime-stale-shipgetbad-follow",
                        f"OrderFollowShip использует {target} из ShipGetBad без доказанных normal-space и общей ShipStar; цель может остаться stale после межсистемного разрыва",
                        path,
                        f"{container.location}:{line_offset + 1}",
                        line.strip(),
                    )
                )
    return issues


def _lint_landed_shipout_after_mutation(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
    summaries: dict[str, dict[str, set[int]]],
) -> list[RuntimeIssue]:
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for name, block in functions.items():
        states: dict[str, set[str]] = {}
        reported: set[str] = set()
        # Only straight-line statements in the outer function body are joined.
        # Branch-local ShipOut calls are handled conservatively to avoid claiming
        # that mutually exclusive landed/space branches execute together.
        for line_offset, depth, call, arguments in _function_call_sites(block):
            if line_offset == 0 or depth != 1 or not arguments:
                continue
            effects_by_actual: dict[str, set[str]] = {}
            effect = _SHIP_EFFECT_CALLS.get(call)
            direct_actual = _simple_identifier(arguments[0])
            if effect and direct_actual:
                effects_by_actual.setdefault(direct_actual, set()).add(effect)
            callee = summaries.get(call)
            if callee is not None:
                for callee_effect, parameter_indexes in callee.items():
                    for parameter_index in parameter_indexes:
                        if parameter_index >= len(arguments):
                            continue
                        actual = _simple_identifier(arguments[parameter_index])
                        if actual:
                            effects_by_actual.setdefault(actual, set()).add(callee_effect)
            for actual, call_effects in effects_by_actual.items():
                before = set(states.get(actual, set()))
                if "ships_out" in call_effects and before & {"mutated", "takeoff"} and actual not in reported:
                    reported.add(actual)
                    reason = "разгрузки/изменения груза" if "mutated" in before else "OrderTakeOff"
                    issues.append(
                        RuntimeIssue(
                            "error",
                            "runtime-landed-shipout-after-mutation",
                            f"{block.name} передаёт {actual} в ShipOut в том же прямом пути после {reason}; завершите обработчик и проверяйте выход в космос на следующем ходу",
                            path,
                            f"{block.location} line {block.start_line + line_offset}",
                            block.lines[line_offset].strip(),
                        )
                    )
                state = states.setdefault(actual, set())
                if "mutates" in call_effects:
                    state.add("mutated")
                if "takes_off" in call_effects:
                    state.add("takeoff")
    return issues


def _lint_group_shipout_iteration(
    project: RsonProject,
    functions: dict[str, FunctionBlock],
    summaries: dict[str, dict[str, set[int]]],
) -> list[RuntimeIssue]:
    path = str(project.path) if project.path else None
    issues: list[RuntimeIssue] = []
    for item in project.iter_objects():
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        for field in ("Code", "ActCode", "LinkCode"):
            value = item.get(field)
            if not isinstance(value, list):
                continue
            lines = [str(line) for line in value]
            _depths, contexts = _code_line_contexts(lines)
            index = 0
            while index < len(lines):
                header = _mask_non_code(lines[index])
                loop = re.search(r"\bfor\s*\((.*)\)", header, re.IGNORECASE)
                if not loop or "groupcount" not in loop.group(1).casefold():
                    index += 1
                    continue
                clauses = loop.group(1).split(";")
                if len(clauses) != 3:
                    index += 1
                    continue
                group_arguments = [
                    arguments[0]
                    for _position, arguments in _call_arguments(loop.group(1), "GroupCount")
                    if arguments
                ]
                group_keys = {
                    _simple_identifier(argument)
                    or re.sub(r"\s+", "", argument).casefold()
                    for argument in group_arguments
                }
                iterator_match = re.search(
                    r"(?:\bint\s+)?\b([A-Za-z_][A-Za-z0-9_]*)\s*=",
                    clauses[0],
                    re.IGNORECASE,
                )
                if not iterator_match:
                    index += 1
                    continue
                iterator = iterator_match.group(1).casefold()
                reverse = "groupcount" in clauses[0].casefold() and bool(
                    re.search(
                        rf"(?:--\s*{re.escape(iterator)}|{re.escape(iterator)}\s*--|{re.escape(iterator)}\s*=\s*{re.escape(iterator)}\s*-)",
                        clauses[2],
                        re.IGNORECASE,
                    )
                )

                cursor = index
                depth = 0
                opened = False
                while cursor < len(lines):
                    masked_line = _mask_non_code(lines[cursor])
                    if "{" in masked_line:
                        opened = True
                    depth += masked_line.count("{") - masked_line.count("}")
                    cursor += 1
                    if opened and depth <= 0:
                        break
                if not opened:
                    index += 1
                    continue
                body = lines[index:cursor]
                folded_body = _mask_non_code("\n".join(body))
                aliases = {
                    match.group(1).casefold()
                    for match in re.finditer(
                        rf"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*GroupShip\s*\([^,]+,\s*{re.escape(iterator)}\s*\)",
                        folded_body,
                        re.IGNORECASE,
                    )
                }
                removal_positions: list[int] = []
                for alias in aliases:
                    removal_positions.extend(
                        position
                        for position, arguments in _call_arguments(folded_body, "ShipOut")
                        if arguments and _simple_identifier(arguments[0]) == alias
                    )
                    for function_name, summary in summaries.items():
                        for position, arguments in _call_arguments(
                            folded_body, functions[function_name].name
                        ):
                            if any(
                                parameter_index < len(arguments)
                                and _simple_identifier(arguments[parameter_index]) == alias
                                for parameter_index in summary["ships_out"]
                            ):
                                removal_positions.append(position)
                if removal_positions and not reverse:
                    issues.append(
                        RuntimeIssue(
                            "error",
                            "runtime-group-mutated-during-iteration",
                            f"Прямой обход GroupShip удаляет текущий корабль через ShipOut, после чего условие цикла повторно вызывает GroupCount; коррекция {iterator} не защищает итератор — вынесите удаление в обратный обход и завершите обработчик до следующего GroupCount",
                            path,
                            f"object #{object_id} {field}:{index + 1}",
                            lines[index].strip(),
                        )
                    )
                elif removal_positions:
                    recount_index = next(
                        (
                            candidate
                            for candidate in range(cursor, len(lines))
                            if contexts[candidate] == contexts[index]
                            and any(
                                (
                                    _simple_identifier(arguments[0])
                                    or re.sub(r"\s+", "", arguments[0]).casefold()
                                )
                                in group_keys
                                for _position, arguments in _call_arguments(
                                    _mask_non_code(lines[candidate]), "GroupCount"
                                )
                                if arguments
                            )
                        ),
                        None,
                    )
                    if recount_index is not None:
                        barrier = any(
                            re.search(r"\b(?:exit|return)\b", _mask_non_code(lines[candidate]), re.IGNORECASE)
                            for candidate in range(cursor, recount_index)
                            if contexts[candidate] == contexts[index]
                        )
                        if not barrier:
                            issues.append(
                                RuntimeIssue(
                                    "error",
                                    "runtime-group-recount-after-mutation",
                                    "После ShipOut код снова вызывает GroupCount в том же обработчике без exit/return; завершите обработчик сразу после обратного прохода и продолжите на следующем ходу",
                                    path,
                                    f"object #{object_id} {field}:{recount_index + 1}",
                                    lines[recount_index].strip(),
                                )
                            )
                index = max(cursor, index + 1)
    return issues


def _proven_bounded_self_recursion(block: FunctionBlock) -> bool:
    """Prove a narrow one-step base-case normalization used by old mods."""

    header = FUNCTION_HEADER_RE.match(_mask_non_code(block.lines[0])) if block.lines else None
    if not header:
        return False
    parameters = [value.strip().casefold() for value in header.group(2).split(",")]
    if not parameters or any(not IDENTIFIER_RE.fullmatch(value) for value in parameters):
        return False
    body = _mask_non_code(block.body_text)
    calls = _call_arguments(body, block.name)
    if not calls:
        return False

    guard_re = re.compile(
        r"\bif\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\+\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*==\s*0(?:\.0+)?\s*\)\s*\{",
        re.IGNORECASE,
    )
    guard = guard_re.search(body)
    if not guard:
        return False
    guarded_parameters = (guard.group(1).casefold(), guard.group(2).casefold())
    if any(value not in parameters for value in guarded_parameters):
        return False
    open_brace = body.find("{", guard.start())
    depth = 0
    close_brace = -1
    for index in range(open_brace, len(body)):
        if body[index] == "{":
            depth += 1
        elif body[index] == "}":
            depth -= 1
            if depth == 0:
                close_brace = index
                break
    if close_brace < 0:
        return False

    numeric = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
    parameter_indexes = [parameters.index(value) for value in guarded_parameters]
    for position, arguments in calls:
        if not (open_brace < position < close_brace) or len(arguments) != len(parameters):
            return False
        selected = [arguments[index] for index in parameter_indexes]
        if not all(numeric.fullmatch(value) for value in selected):
            return False
        if sum(float(value) for value in selected) == 0:
            return False
    return True


def _first_top_level_risky_line(lines: list[str], risky: set[str]) -> tuple[int, set[str]] | None:
    """Return the first executable Turn line that reaches world work."""
    in_function = False
    function_opened = False
    depth = 0
    for index, line in enumerate(lines):
        masked = _mask_non_code(line)
        if not in_function and FUNCTION_RE.match(masked):
            in_function = True
            function_opened = False
            depth = 0
        if in_function:
            if "{" in masked:
                function_opened = True
            depth += masked.count("{") - masked.count("}")
            if function_opened and depth <= 0:
                in_function = False
            continue
        calls = {value.casefold() for value in _calls(line)}
        reached = calls & (WORLD_CALLS | risky)
        if reached:
            return index, reached
    return None


def _inline_readiness_barrier(
    lines: list[str],
    ready_vars: set[str],
    ready_turn_vars: set[str],
    before: int | None = None,
) -> bool:
    wrapped = ("<turn>", *lines)
    limit = len(wrapped) if before is None else before + 1
    guarded = any(_has_exit_guard(wrapped, variable, limit) for variable in ready_vars)
    if not guarded:
        return False
    return not ready_turn_vars or any(
        _has_turn_grace(wrapped, variable, limit) for variable in ready_turn_vars
    )


def _positive_turn_gate(
    item: dict[str, Any],
    ready_vars: set[str],
    ready_turn_vars: set[str],
) -> bool:
    """Prove that the true (Nom=0) branch is outside generation turn zero."""
    if str(item.get("Type", "")).casefold() != "tif":
        return False
    lines = item.get("Code")
    if not isinstance(lines, list):
        return False
    folded = _mask_non_code("\n".join(str(line) for line in lines)).casefold()
    if "||" in folded:
        return False
    generation_barriers = (
        r"curturn\s*\(\s*\)\s*>\s*0\b",
        r"curturn\s*\(\s*\)\s*>=\s*1\b",
        r"\b0\s*<\s*curturn\s*\(\s*\)",
        r"\b1\s*<=\s*curturn\s*\(\s*\)",
    )
    if any(re.search(pattern, folded) for pattern in generation_barriers):
        return True
    ready_proven = False
    for variable in ready_vars:
        wanted = re.escape(variable)
        positive_patterns = (
            rf"\b{wanted}\b\s*(?:&&|\)|$)",
            rf"\b{wanted}\b\s*(?:!=|>)\s*0\b",
            rf"\b{wanted}\b\s*==\s*1\b",
            rf"\b0\s*(?:!=|<)\s*{wanted}\b",
            rf"\b1\s*==\s*{wanted}\b",
        )
        if not re.search(rf"!\s*{wanted}\b", folded) and any(
            re.search(pattern, folded) for pattern in positive_patterns
        ):
            ready_proven = True
            break
    if not ready_proven:
        return False
    if not ready_turn_vars:
        return True
    for variable in ready_turn_vars:
        wanted = re.escape(variable)
        comparisons = (
            rf"curturn\s*\(\s*\)\s*>\s*{wanted}\b",
            rf"\b{wanted}\s*<\s*curturn\s*\(\s*\)",
        )
        if any(re.search(pattern, folded) for pattern in comparisons):
            return True
    return False


def _graph_guarded_turn_entries(
    project: RsonProject,
    ready_vars: set[str],
    ready_turn_vars: set[str],
) -> set[int]:
    """Return Turn objects reached exclusively through a proven readiness gate.

    The analysis tracks both guarded and unguarded reachability.  A merge is
    considered guarded only when no root-to-object path can arrive without the
    barrier, so adding an alternative direct link cannot accidentally suppress
    a runtime warning.
    """
    objects = {
        item["#"]: item
        for item in project.iter_objects()
        if isinstance(item.get("#"), int)
        and str(item.get("Code.Type", "")).casefold() == "turn"
    }
    if not objects:
        return set()

    outgoing: dict[int, list[tuple[int, int]]] = {object_id: [] for object_id in objects}
    incoming: dict[int, set[int]] = {object_id: set() for object_id in objects}
    links = project.data.get("Visual.Links", [])
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            begin = link.get("Begin")
            end = link.get("End")
            if begin not in objects or end not in objects:
                continue
            nom = link.get("Nom", 0)
            if not isinstance(nom, int) or isinstance(nom, bool):
                continue
            outgoing[begin].append((end, nom))
            incoming[end].add(begin)

    roots = {object_id for object_id in objects if not incoming[object_id]}
    entry_states: dict[int, set[bool]] = {object_id: set() for object_id in objects}
    pending: list[tuple[int, bool]] = [(object_id, False) for object_id in roots]
    seen: set[tuple[int, bool]] = set()
    while pending:
        object_id, guarded_on_entry = pending.pop()
        state = (object_id, guarded_on_entry)
        if state in seen:
            continue
        seen.add(state)
        entry_states[object_id].add(guarded_on_entry)
        item = objects[object_id]
        lines = item.get("Code")
        inline_guard = isinstance(lines, list) and _inline_readiness_barrier(
            [str(line) for line in lines], ready_vars, ready_turn_vars
        )
        guarded_after = guarded_on_entry or inline_guard
        positive_gate = _positive_turn_gate(item, ready_vars, ready_turn_vars)
        for child, nom in outgoing[object_id]:
            edge_guarded = guarded_after or (positive_gate and nom == 0)
            pending.append((child, edge_guarded))

    return {
        object_id
        for object_id, states in entry_states.items()
        if states == {True}
    }


def lint_rson_runtime(project: RsonProject) -> list[RuntimeIssue]:
    path = str(project.path) if project.path else None
    functions, issues = _extract_functions(project)
    issues.extend(_lint_apostrophes_in_line_comments(project))
    issues.extend(_lint_cross_block_calls(project, functions))
    issues.extend(_lint_unavailable_engine_calls(project, functions))
    issues.extend(_lint_unresolved_user_functions(project, functions))
    issues.extend(_lint_rscript_arrays(project))
    issues.extend(_lint_rndobject_anchor_types(project, functions))
    issues.extend(_lint_id_to_ship_guards(project, functions))
    issues.extend(_lint_suppressed_shipjoin_state(project, functions))
    issues.extend(_lint_shipjoin_guarded_by_script_membership(project, functions))
    issues.extend(_lint_runtime_cross_block_variables(project))
    issues.extend(_lint_linked_empty_runtime_code(project))
    issues.extend(_lint_persistent_item_handles(project, functions))
    issues.extend(_lint_persistent_world_object_handles(project, functions))
    ship_effects = _ship_effect_summaries(functions)
    issues.extend(_lint_repeated_detached_item_free(project, functions))
    issues.extend(_lint_order_rewrite_before_hyperspace_guard(project, functions))
    issues.extend(_lint_post_group_mutation_dereference(project, functions, ship_effects))
    issues.extend(_lint_cleanup_without_turn_gate(project, functions, ship_effects))
    issues.extend(_lint_stale_shipgetbad_follow(project, functions))
    issues.extend(_lint_landed_shipout_after_mutation(project, functions, ship_effects))
    issues.extend(_lint_group_shipout_iteration(project, functions, ship_effects))
    graph = _call_graph(functions)
    risky = _risky_functions(functions, graph)

    global_initialization = _global_initialization_lines(project)
    initialization_text = "\n".join(line for _object_id, _line_number, line in global_initialization)
    initialized_zero = {
        match.group(1).casefold()
        for match in ASSIGN_ZERO_RE.finditer(_mask_non_code(initialization_text))
    }

    entering_handlers: set[str] = set()
    entering_inline: list[str] = []
    for item in project.iter_objects():
        if item.get("Type") != "TState" or "t_OnEnteringForm" not in project.state_events(item.get("#")):
            continue
        code = item.get("OnActCode", "")
        if isinstance(code, str):
            handler = re.sub(r"^\s*\[[^\n]*\|\]\s*", "", code)
            entering_inline.append(handler)
            entering_handlers.update(call.casefold() for call in _calls(handler) if call.casefold() in functions)
    entering_reachable = _reachable(entering_handlers, graph)
    entering_text = "\n".join(entering_inline + [functions[name].body_text for name in sorted(entering_reachable)])
    ready_vars = initialized_zero & {
        match.group(1).casefold() for match in ASSIGN_ONE_RE.finditer(_mask_non_code(entering_text))
    }
    ready_turn_vars = initialized_zero & {
        match.group(1).casefold() for match in ASSIGN_TURN_RE.finditer(_mask_non_code(entering_text))
    }

    graph_guarded_turn_entries = _graph_guarded_turn_entries(project, ready_vars, ready_turn_vars)
    dialog_scoped_turns = _dialog_scoped_turn_objects(project)

    for variable in sorted(ready_vars):
        assignment = re.compile(rf"\b{re.escape(variable)}\s*=\s*1\s*;", re.IGNORECASE)
        for name in sorted(entering_reachable):
            block = functions[name]
            setter_line = next(
                (index for index, line in enumerate(block.lines[1:], start=1) if assignment.search(_mask_non_code(line))),
                None,
            )
            if setter_line is None:
                continue
            first_risk = _first_risky_line(block, risky)
            if first_risk is None or first_risk <= setter_line:
                continue
            armed_prefix = _mask_non_code("\n".join(block.lines[setter_line:first_risk])).casefold()
            if not re.search(r"\bexit\b", armed_prefix):
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-first-ui-event-work",
                        f"Обработчик {block.name} открывает флаг {variable}, но не завершает первый UI-вызов до доступа к миру",
                        path,
                        block.location,
                        block.lines[first_risk].strip(),
                    )
                )

    turn_starts: set[str] = set()
    turn_function_guard_starts: set[str] = set()
    inline_direct_world = False
    for item in project.iter_objects():
        if str(item.get("Code.Type", "")).casefold() != "turn":
            continue
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
        if object_id in dialog_scoped_turns:
            continue
        lines = item.get("Code")
        if not isinstance(lines, list):
            continue
        text = "\n".join(lines)
        calls = {call.casefold() for call in _calls(text)}
        custom = calls & set(functions)
        turn_starts.update(custom)
        first_top_risk = _first_top_level_risky_line([str(line) for line in lines], risky)
        if first_top_risk is None:
            continue
        risk_index, reached = first_top_risk
        wrapped_lines = ("<turn>", *(str(line) for line in lines))
        before = risk_index + 1
        guarded_by = [variable for variable in ready_vars if _has_exit_guard(wrapped_lines, variable, before)]
        if object_id in graph_guarded_turn_entries:
            continue
        if guarded_by:
            if ready_turn_vars and not any(
                _has_turn_grace(wrapped_lines, variable, before) for variable in ready_turn_vars
            ):
                issues.append(
                    RuntimeIssue(
                        "warning",
                        "runtime-no-post-ui-turn-grace",
                        "Пошаговый Top защищён флагом UI, но не пропускает ход, на котором флаг был установлен",
                        path,
                        f"object #{item.get('#')} Code",
                    )
                )
            continue

        risky_custom = reached & risky
        turn_function_guard_starts.update(risky_custom)
        if reached & WORLD_CALLS:
            inline_direct_world = True
            issues.append(
                RuntimeIssue(
                    "warning",
                    "runtime-turn-direct-world-access",
                    "Пошаговый объект обращается к миру напрямую до доказанного раннего exit по флагу готовности UI",
                    path,
                    f"object #{item.get('#')} Code",
                    str(lines[risk_index]).strip(),
                )
            )

    for name in sorted(turn_function_guard_starts):
        if name not in risky:
            continue
        block = functions[name]
        first_risk = _first_risky_line(block, risky)
        if first_risk is None:
            continue
        guarded_by = [variable for variable in ready_vars if _has_exit_guard(block.lines, variable, first_risk)]
        if not guarded_by:
            issues.append(
                RuntimeIssue(
                    "warning",
                    "runtime-turn-before-ui",
                    f"Пошаговая функция {block.name} достигает Player/Shop/Galaxy до раннего exit, связанного с t_OnEnteringForm",
                    path,
                    block.location,
                    block.lines[first_risk].strip(),
                )
            )
        elif ready_turn_vars and not any(
            _has_turn_grace(block.lines, variable, first_risk) for variable in ready_turn_vars
        ):
            issues.append(
                RuntimeIssue(
                    "warning",
                    "runtime-no-post-ui-turn-grace",
                    f"{block.name} защищена флагом UI, но не пропускает ход, на котором флаг был установлен",
                    path,
                    block.location,
                )
            )

    runtime_starts = turn_starts | entering_handlers
    for cycle in _find_recursion_cycles(graph, runtime_starts):
        if len(cycle) == 1 and _proven_bounded_self_recursion(functions[cycle[0]]):
            continue
        label = " -> ".join(functions[name].name for name in cycle) + f" -> {functions[cycle[0]].name}"
        issues.append(
            RuntimeIssue(
                "error",
                "runtime-recursion-cycle",
                f"Из runtime-точки достижим цикл вызовов: {label}",
                path,
                functions[cycle[0]].location,
            )
        )

    reachable_runtime = _reachable(runtime_starts, graph)
    loop_depths = _runtime_loop_depths(turn_starts, graph, functions)
    for name, (depth, local_depth, evidence) in sorted(loop_depths.items()):
        if depth < 2 or local_depth < 1 or name not in risky or evidence is None:
            continue
        block = functions[name]
        issues.append(
            RuntimeIssue(
                "error",
                "runtime-nested-world-loop",
                f"Пошаговая цепочка достигает {block.name} с суммарной вложенностью циклов {depth}; обработку мира нужно дробить по явному бюджету на ход",
                path,
                block.location,
                evidence,
            )
        )

    literal_loop = re.compile(r"\b(?:while\s*\(\s*(?:1|true)\s*\)|for\s*\(\s*;\s*;\s*\))", re.IGNORECASE)
    for name in sorted(reachable_runtime):
        block = functions[name]
        match = literal_loop.search(_mask_non_code(block.body_text))
        if match:
            severity = "warning" if re.search(r"\b(?:break|exit)\b", _mask_non_code(block.body_text)) else "error"
            issues.append(
                RuntimeIssue(
                    severity,
                    "runtime-unbounded-loop",
                    f"В достижимой функции {block.name} найден цикл без статической верхней границы",
                    path,
                    block.location,
                    match.group(0),
                )
            )

    for object_id, line_number, line in global_initialization:
        calls = {value.casefold() for value in _calls(line)}
        if calls & WORLD_CALLS or calls & risky:
            issues.append(
                RuntimeIssue(
                    "error",
                    "runtime-startup-world-access",
                    "Глобальная инициализация обращается к игровому миру до подтверждения готовности интерфейса",
                    path,
                    f"object #{object_id} Code:{line_number}",
                    line.strip(),
                )
            )

    # A proven CurTurn()>0 graph gate is a complete generation barrier and does
    # not need a separate t_OnEnteringForm source.  Only entry chains that still
    # require their own function guard participate in the missing-source check.
    has_risky_turn_work = inline_direct_world or bool(turn_function_guard_starts)
    if has_risky_turn_work and not entering_inline:
        issues.append(
            RuntimeIssue(
                "warning",
                "runtime-ui-readiness-source-missing",
                "Есть опасная пошаговая работа, но нет обработчика t_OnEnteringForm, который может открыть защитный флаг",
                path,
            )
        )
    return issues


def _split_call_arguments(text: str, open_paren: int) -> tuple[list[str], int] | None:
    arguments: list[str] = []
    start = open_paren + 1
    depth = 1
    quote = ""
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                arguments.append(text[start:index].strip())
                return arguments, index + 1
        elif char == "," and depth == 1:
            arguments.append(text[start:index].strip())
            start = index + 1
        index += 1
    return None


def _script_run_calls(text: str) -> list[tuple[list[str], str]]:
    masked = _mask_non_code(text)
    result: list[tuple[list[str], str]] = []
    for match in re.finditer(r"\bScriptRun\s*\(", masked, re.IGNORECASE):
        open_paren = masked.find("(", match.start())
        parsed = _split_call_arguments(text, open_paren)
        if parsed:
            arguments, end = parsed
            result.append((arguments, text[match.start():end]))
    return result


def _node_parameters(nodes: Iterable[BlockParNode], prefix: str = ""):
    for node in nodes:
        node_path = f"{prefix}/{node.name}" if prefix else node.name
        for parameter in node.parameters:
            yield node_path, parameter.key, parameter.value
        yield from _node_parameters(node.children, node_path)


def lint_main_runtime(document: BlockParDocument, path: str | Path | None = None) -> list[RuntimeIssue]:
    issues: list[RuntimeIssue] = []
    source = str(Path(path).resolve()) if path else None
    for node_path, key, value in _node_parameters(document.roots):
        for arguments, call in _script_run_calls(value):
            if len(arguments) < 2:
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-scriptrun-arguments",
                        "ScriptRun должен содержать контекст звезды и планеты",
                        source,
                        f"{node_path}/{key}",
                        call,
                    )
                )
                continue
            star = re.sub(r"\s+", "", arguments[0]).casefold()
            planet = re.sub(r"\s+", "", arguments[1]).casefold()
            player_star = star == "shipstar(player())"
            unsafe_first_planet = planet == "starplanets(shipstar(player()),0)"
            if player_star and unsafe_first_planet:
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-unsafe-player-planet-context",
                        "ScriptRun привязан к первой планете звезды, а не к фактической планете игрока; используйте GetShipPlanet(Player())",
                        source,
                        f"{node_path}/{key}",
                        call,
                    )
                )
            elif player_star and planet != "getshipplanet(player())":
                issues.append(
                    RuntimeIssue(
                        "warning",
                        "runtime-ambiguous-player-planet-context",
                        "ScriptRun использует звезду игрока, но контекст планеты не совпадает с GetShipPlanet(Player())",
                        source,
                        f"{node_path}/{key}",
                        call,
                    )
                )
    return issues


def has_onstart_script_run(document: BlockParDocument) -> bool:
    for node_path, _key, value in _node_parameters(document.roots):
        if "onstart" in {part.casefold() for part in node_path.split("/")} and _script_run_calls(value):
            return True
    return False


def lint_module_runtime(module: ModuleInfo) -> list[RuntimeIssue]:
    issues: list[RuntimeIssue] = []
    languages = {value.casefold() for value in module.languages}
    russian_other = {"othermods", "other mods"}
    if "rus" in languages and module.section.strip().casefold() in russian_other:
        line = next((entry.line for entry in module.entries if entry.key.casefold() == "section"), None)
        issues.append(
            RuntimeIssue(
                "warning",
                "runtime-module-section-rus",
                "Для русского языка секция OtherMods должна быть штатной «Прочие моды»",
                str(module.path),
                f"line {line}" if line else None,
                f"Section={module.section}",
            )
        )
    section_eng = module.first("SectionEng")
    if "eng" in languages and section_eng.strip().casefold() == "othermods":
        line = next((entry.line for entry in module.entries if entry.key.casefold() == "sectioneng"), None)
        issues.append(
            RuntimeIssue(
                "warning",
                "runtime-module-section-eng",
                "Английская секция должна использовать штатное отображаемое имя «Other Mods»",
                str(module.path),
                f"line {line}" if line else None,
                f"SectionEng={section_eng}",
            )
        )
    return issues
