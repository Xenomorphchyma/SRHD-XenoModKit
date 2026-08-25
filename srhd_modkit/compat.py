from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .audit import AuditIssue
from .blockpar import BlockParDocument, BlockParNode, load_blockpar
from .discovery import discover_mods
from .files import iter_files, sha256_file
from .modcfg import ModConfig, parse_modcfg, validate_modcfg
from .models import ModRecord, normalize_ref
from .textio import read_text
from .toolchain import Toolchain
from .validation import validate_collection


MODSET_SCHEMA = "srhd-modkit-modset-v1"


@dataclass(frozen=True, slots=True)
class OverlayOwner:
    mod: str
    mod_path: str
    file: str
    priority: int | None
    order: int
    sha256: str
    effective_priority: int = 0
    priority_source: str = "default-zero"
    configured_order: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mod": self.mod,
            "mod_path": self.mod_path,
            "file": self.file,
            "priority": self.priority,
            "effective_priority": self.effective_priority,
            "priority_source": self.priority_source,
            "configured_order": self.configured_order,
            "order": self.order,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class _EnabledMod:
    reference: str
    mod: ModRecord
    configured_order: int
    effective_priority: int
    priority_source: str


@dataclass(frozen=True, slots=True)
class OverlayCollision:
    path: str
    kind: str
    owners: tuple[OverlayOwner, ...]
    identical: bool
    resolution: str = "unknown"
    operators: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "identical": self.identical,
            "resolution": self.resolution,
            "operators": list(self.operators),
            "owners": [owner.as_dict() for owner in self.owners],
        }


@dataclass(frozen=True, slots=True)
class ModSetReport:
    config: str
    mods_root: str
    load_order: tuple[dict[str, Any], ...]
    dependency_edges: tuple[dict[str, Any], ...]
    conflict_edges: tuple[dict[str, Any], ...]
    cycles: tuple[tuple[str, ...], ...]
    collisions: tuple[OverlayCollision, ...]
    issues: tuple[AuditIssue, ...]
    schema: str = MODSET_SCHEMA

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "config": self.config,
            "mods_root": self.mods_root,
            "order_policy": {
                "field": "Priority",
                "direction": "ascending",
                "stable": True,
                "missing_priority": 0,
                "tie_breaker": "CurrentMod",
                "writes_config": False,
            },
            "load_order": list(self.load_order),
            "dependency_edges": list(self.dependency_edges),
            "conflict_edges": list(self.conflict_edges),
            "cycles": [list(cycle) for cycle in self.cycles],
            "collisions": [collision.as_dict() for collision in self.collisions],
            "issues": [issue.as_dict() for issue in self.issues],
            "summary": {
                "enabled_mods": len(self.load_order),
                "dependency_edges": len(self.dependency_edges),
                "conflict_edges": len(self.conflict_edges),
                "cycles": len(self.cycles),
                "collisions": len(self.collisions),
                "errors": sum(issue.severity == "error" for issue in self.issues),
                "warnings": sum(issue.severity == "warning" for issue in self.issues),
            },
        }


def _indexes(mods: Iterable[ModRecord]) -> tuple[dict[str, ModRecord], dict[str, list[ModRecord]]]:
    by_path: dict[str, ModRecord] = {}
    by_ref: dict[str, list[ModRecord]] = {}
    for mod in mods:
        path_ref = mod.relative_path.as_posix().replace("/", "\\")
        by_path[normalize_ref(path_ref)] = mod
        for ref in dict.fromkeys((mod.name, mod.root.name, path_ref)):
            key = normalize_ref(ref)
            if not key:
                continue
            for candidate in dict.fromkeys((key, key.split("\\")[-1])):
                values = by_ref.setdefault(candidate, [])
                if mod not in values:
                    values.append(mod)
    return by_path, by_ref


def _enabled(config: ModConfig, mods: list[ModRecord]) -> list[tuple[int, str, ModRecord]]:
    by_path, _ = _indexes(mods)
    result: list[tuple[int, str, ModRecord]] = []
    seen: set[str] = set()
    for configured_order, ref in enumerate(config.enabled):
        key = normalize_ref(ref)
        if key in seen:
            continue
        seen.add(key)
        mod = by_path.get(key)
        if mod is not None:
            result.append((configured_order, ref, mod))
    return result


def _effective_enabled(
    configured: list[tuple[int, str, ModRecord]],
) -> list[_EnabledMod]:
    """Model the engine's stable ascending Priority order without editing ModCFG."""

    entries: list[_EnabledMod] = []
    for configured_order, reference, mod in configured:
        raw = mod.module.first("Priority").strip()
        if not raw:
            effective_priority = 0
            source = "default-zero"
        elif mod.module.priority is None:
            # Invalid Priority is reported separately.  Zero is the safest
            # deterministic fallback for analysis and matches an absent field.
            effective_priority = 0
            source = "invalid-assumed-zero"
        else:
            effective_priority = mod.module.priority
            source = "explicit"
        entries.append(
            _EnabledMod(
                reference,
                mod,
                configured_order,
                effective_priority,
                source,
            )
        )
    # Python's sort is stable: CurrentMod remains the tie-breaker when two mods
    # have the same effective Priority, matching the engine's swap-on-greater
    # behavior instead of inventing an alphabetical order.
    return sorted(entries, key=lambda item: item.effective_priority)


def _dependency_edges(
    enabled: list[ModRecord],
    all_mods: list[ModRecord],
) -> tuple[list[dict[str, Any]], dict[str, set[str]], dict[str, str]]:
    _, by_ref = _indexes(all_mods)
    enabled_ids = {normalize_ref(mod.relative_path.as_posix().replace("/", "\\")) for mod in enabled}
    name_counts: dict[str, int] = {}
    for mod in enabled:
        name_counts[mod.name.casefold()] = name_counts.get(mod.name.casefold(), 0) + 1
    labels = {
        normalize_ref(mod.relative_path.as_posix().replace("/", "\\")): (
            mod.name
            if name_counts[mod.name.casefold()] == 1
            else f"{mod.name} [{mod.relative_path.as_posix()}]"
        )
        for mod in enabled
    }
    edges: list[dict[str, Any]] = []
    graph: dict[str, set[str]] = {identifier: set() for identifier in enabled_ids}
    for mod in enabled:
        mod_id = normalize_ref(mod.relative_path.as_posix().replace("/", "\\"))
        for dependency in mod.module.dependencies:
            matches = by_ref.get(normalize_ref(dependency), [])
            active = [
                item
                for item in matches
                if normalize_ref(item.relative_path.as_posix().replace("/", "\\")) in enabled_ids
            ]
            status = "missing" if not matches else "disabled" if not active else "enabled"
            targets = active or matches
            edges.append(
                {
                    "from": mod.name,
                    "from_path": mod.relative_path.as_posix(),
                    "reference": dependency,
                    "to": [item.name for item in targets],
                    "to_paths": [item.relative_path.as_posix() for item in targets],
                    "status": status,
                }
            )
            for target in active:
                target_id = normalize_ref(target.relative_path.as_posix().replace("/", "\\"))
                graph[mod_id].add(target_id)
    return edges, graph, labels


def _conflict_edges(enabled: list[ModRecord], all_mods: list[ModRecord]) -> list[dict[str, Any]]:
    _, by_ref = _indexes(all_mods)
    enabled_ids = {normalize_ref(mod.relative_path.as_posix().replace("/", "\\")) for mod in enabled}
    edges: list[dict[str, Any]] = []
    for mod in enabled:
        for conflict in mod.module.conflicts:
            matches = by_ref.get(normalize_ref(conflict), [])
            active = [
                item
                for item in matches
                if normalize_ref(item.relative_path.as_posix().replace("/", "\\")) in enabled_ids
            ]
            status = "missing" if not matches else "disabled" if not active else "enabled"
            edges.append(
                {
                    "from": mod.name,
                    "from_path": mod.relative_path.as_posix(),
                    "reference": conflict,
                    "to": [item.name for item in (active or matches)],
                    "to_paths": [item.relative_path.as_posix() for item in (active or matches)],
                    "status": status,
                }
            )
    return edges


def _cycles(graph: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    found: set[tuple[str, ...]] = set()
    visiting: list[str] = []
    active: set[str] = set()
    visited: set[str] = set()

    def canonical(cycle: list[str]) -> tuple[str, ...]:
        body = cycle[:-1]
        variants = [tuple(body[index:] + body[:index]) for index in range(len(body))]
        best = min(variants, key=lambda item: tuple(value.casefold() for value in item))
        return best + (best[0],)

    def walk(node: str) -> None:
        if node in visited:
            return
        active.add(node)
        visiting.append(node)
        for target in sorted(graph.get(node, ()), key=str.casefold):
            if target in active:
                index = visiting.index(target)
                found.add(canonical(visiting[index:] + [target]))
            elif target not in visited:
                walk(target)
        visiting.pop()
        active.remove(node)
        visited.add(node)

    for node in sorted(graph, key=str.casefold):
        walk(node)
    return tuple(sorted(found, key=lambda item: tuple(value.casefold() for value in item)))


def _walk_nodes(nodes: Iterable[BlockParNode], *, depth: int = 0) -> Iterable[tuple[int, BlockParNode]]:
    for node in nodes:
        yield depth, node
        yield from _walk_nodes(node.children, depth=depth + 1)


def _document_operators(document: BlockParDocument) -> set[str]:
    result: set[str] = set()
    for depth, node in _walk_nodes(document.roots):
        if depth > 0:
            result.add(node.operator)
    if not result:
        result.update(node.operator for node in document.roots)
    return result


def _read_blockpar(path: Path, tools: Toolchain, temp: Path, index: int) -> BlockParDocument:
    if path.suffix.casefold() == ".txt":
        return load_blockpar(path)
    output = temp / f"collision-{index:06d}.txt"
    tools.convert_dat(path, output, verify=False)
    return load_blockpar(output)


def _classify_collision(
    relative: str,
    paths: list[Path],
    tools: Toolchain,
    temp: Path,
) -> tuple[str, tuple[str, ...], str | None]:
    folded = relative.casefold()
    suffix = Path(relative).suffix.casefold()
    if suffix == ".scr":
        return "script-binary", (), None
    if Path(relative).name.casefold().startswith("cachedata."):
        return "script-cache", (), None
    if (
        ("/cfg/" in f"/{folded}" and "/rus/" in f"/{folded}")
        or Path(relative).name.casefold().startswith("lang.")
    ):
        return "language-data", (), None
    if suffix in {".dat", ".txt"}:
        operators: set[str] = set()
        parsed = 0
        failure: str | None = None
        for index, path in enumerate(paths):
            try:
                document = _read_blockpar(path, tools, temp, index)
                operators.update(_document_operators(document))
                parsed += 1
            except Exception as exc:
                failure = str(exc)
                break
        if parsed == len(paths):
            ordered = tuple(sorted(operators))
            if operators and operators <= {"~"}:
                return "blockpar-merge", ordered, None
            return "blockpar-replacement", ordered, None
        if suffix == ".txt":
            try:
                for path in paths:
                    read_text(path)
                return "text-replacement", (), None
            except Exception:
                pass
        return "structured-unverified", tuple(sorted(operators)), failure
    return "binary-replacement", (), None


def _collisions(
    enabled: list[_EnabledMod],
    tools: Toolchain,
) -> tuple[list[OverlayCollision], list[AuditIssue]]:
    owners: dict[str, list[tuple[int, _EnabledMod, Path, str]]] = {}
    for order, entry in enumerate(enabled):
        mod = entry.mod
        for path in iter_files(mod.root):
            relative = path.relative_to(mod.root).as_posix()
            folded = relative.casefold()
            if not (folded.startswith("cfg/") or folded.startswith("data/")):
                continue
            owners.setdefault(folded, []).append((order, entry, path, relative))

    collisions: list[OverlayCollision] = []
    issues: list[AuditIssue] = []
    with tempfile.TemporaryDirectory(prefix="srhd-compat-") as name:
        temp = Path(name)
        for entries in owners.values():
            if len(entries) < 2:
                continue
            hashes = [sha256_file(path) for _, _, path, _ in entries]
            identical = len(set(hashes)) == 1
            relative = entries[0][3]
            kind = "identical" if identical else "binary-replacement"
            operators: tuple[str, ...] = ()
            failure: str | None = None
            if not identical:
                kind, operators, failure = _classify_collision(
                    relative,
                    [path for _, _, path, _ in entries],
                    tools,
                    temp,
                )
            if failure:
                issues.append(
                    AuditIssue(
                        "warning",
                        "collision-inspection-failed",
                        f"Не удалось глубоко классифицировать пересечение {relative}: {failure}",
                        str(entries[0][2]),
                        validator="compat",
                    )
                )
            collision_owners = tuple(
                OverlayOwner(
                    mod=entry.mod.name,
                    mod_path=entry.mod.relative_path.as_posix(),
                    file=str(path),
                    priority=entry.mod.module.priority,
                    order=order,
                    sha256=digest,
                    effective_priority=entry.effective_priority,
                    priority_source=entry.priority_source,
                    configured_order=entry.configured_order,
                )
                for (order, entry, path, _), digest in zip(entries, hashes)
            )
            collisions.append(
                OverlayCollision(relative, kind, collision_owners, identical, "unknown", operators)
            )
    return sorted(collisions, key=lambda item: item.path.casefold()), issues


def analyze_modset(
    config: str | Path | ModConfig,
    mods_root: str | Path,
    *,
    tools_root: str | Path | None = None,
) -> ModSetReport:
    parsed = parse_modcfg(config) if not isinstance(config, ModConfig) else config
    root = Path(mods_root).resolve()
    mods = discover_mods(root)
    configured_entries = _enabled(parsed, mods)
    enabled_entries = _effective_enabled(configured_entries)
    enabled = [entry.mod for entry in enabled_entries]
    issues = [
        AuditIssue.from_value(item, validator="compat", mod=getattr(item, "mod", ""))
        for item in validate_modcfg(parsed, root, mods)
    ]
    issues.extend(
        AuditIssue.from_value(item, validator="compat", mod=getattr(item, "mod", ""))
        for item in validate_collection(mods)
        if item.code == "duplicate-name"
    )
    for entry in enabled_entries:
        if entry.priority_source != "invalid-assumed-zero":
            continue
        issues.append(
            AuditIssue(
                "warning",
                "invalid-priority",
                f"{entry.mod.name}: Priority не является целым числом; compat "
                "использует 0 только как безопасное допущение для порядка анализа",
                str(entry.mod.module.path),
                entry.mod.name,
                "compat",
                evidence=entry.mod.module.first("Priority"),
            )
        )
    effective_configured_orders = [entry.configured_order for entry in enabled_entries]
    configured_orders = [item[0] for item in configured_entries]
    if effective_configured_orders != configured_orders:
        moved = sum(
            left != right
            for left, right in zip(configured_orders, effective_configured_orders)
        )
        issues.append(
            AuditIssue(
                "warning",
                "modcfg-priority-order-mismatch",
                "Порядок CurrentMod не совпадает со стабильной сортировкой по "
                f"возрастанию Priority ({moved} позиций отличаются). Игра может "
                "предложить переставить моды; compat уже использует эффективный "
                "порядок, но ModCFG.txt не изменяет",
                str(parsed.path),
                validator="compat",
                evidence="configured="
                + ",".join(str(value) for value in configured_orders)
                + "; effective="
                + ",".join(str(value) for value in effective_configured_orders),
            )
        )
    edges, graph, graph_labels = _dependency_edges(enabled, mods)
    conflicts = _conflict_edges(enabled, mods)
    cycle_ids = _cycles(graph)
    found_cycles = tuple(
        tuple(graph_labels.get(identifier, identifier) for identifier in cycle)
        for cycle in cycle_ids
    )
    for cycle in found_cycles:
        issues.append(
            AuditIssue(
                "error",
                "dependency-cycle",
                "Цикл зависимостей: " + " -> ".join(cycle),
                str(parsed.path),
                validator="compat",
            )
        )
    collision_items, collision_issues = _collisions(enabled_entries, Toolchain(tools_root))
    issues.extend(collision_issues)
    order = tuple(
        {
            "order": index,
            "configured_order": entry.configured_order,
            "reference": entry.reference,
            "name": entry.mod.name,
            "path": entry.mod.relative_path.as_posix(),
            "priority": entry.mod.module.priority,
            "effective_priority": entry.effective_priority,
            "priority_source": entry.priority_source,
        }
        for index, entry in enumerate(enabled_entries)
    )
    return ModSetReport(
        str(parsed.path),
        str(root),
        order,
        tuple(edges),
        tuple(conflicts),
        found_cycles,
        tuple(collision_items),
        tuple(issues),
    )
