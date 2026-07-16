from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .blockpar import BlockParDocument, BlockParNode
from .models import ModuleInfo
from .scripts import RsonProject


IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
FUNCTION_RE = re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.IGNORECASE)
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

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def body_text(self) -> str:
        return "\n".join(self.lines[1:])

    @property
    def location(self) -> str:
        return f"object #{self.object_id} {self.field}:{self.start_line} function {self.name}"


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
        if str(item.get("Code.Type", "")).casefold() == "turn":
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
    issues.extend(_lint_cross_block_calls(project, functions))
    issues.extend(_lint_runtime_cross_block_variables(project))
    issues.extend(_lint_linked_empty_runtime_code(project))
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
        object_id = item.get("#") if isinstance(item.get("#"), int) else None
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
