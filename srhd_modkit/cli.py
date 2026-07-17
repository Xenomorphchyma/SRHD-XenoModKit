from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .discovery import discover_mods, load_mod
from .files import build_manifest, compare_trees, find_collisions, find_duplicates, pack_mod, sha256_file, stage_tree
from .formats import format_catalog, inspect_file, scan_formats
from .modcfg import parse_modcfg, validate_modcfg
from .module_info import find_module_info, parse_module_info
from .toolchain import Toolchain, is_empty_rscript_lang_dat
from .blockpar import BlockParDocument, BlockParNode, load_blockpar
from .scripts import inspect_scr, load_rson
from .resources import build_gai, build_pkg, extract_resource, inspect_resource, verify_resource
from .quests import (
    build_quest_from_json,
    export_quest_json,
    inspect_quest,
    load_quest,
    quest_media,
    verify_quest,
)
from .game_text import GameTextIssue, lint_game_text
from .script_artifacts import ScriptArtifactIssue, lint_script_cache
from .textio import DecodedText, read_text
from .runtime_lint import (
    RuntimeIssue,
    has_onstart_script_run,
    lint_main_runtime,
    lint_module_runtime,
    lint_rson_runtime,
)
from .validation import validate_collection
from .audit import AuditProfile, AuditReport, audit_collection, audit_mod
from .release import ReleaseBlockedError, build_release
from .compat import analyze_modset
from .hidden_process import inspect_hidden_processes, terminate_hidden_processes


def human_size(value: int) -> str:
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    number = float(value)
    for unit in units:
        if abs(number) < 1024 or unit == units[-1]:
            return f"{number:.2f} {unit}" if unit != "Б" else f"{int(number)} {unit}"
        number /= 1024
    return f"{value} Б"


def parse_size(value: str) -> int:
    raw = value.strip().casefold().replace("ib", "b")
    multipliers = {"k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2, "g": 1024**3, "gb": 1024**3}
    for suffix in sorted(multipliers, key=len, reverse=True):
        if raw.endswith(suffix):
            return int(float(raw[: -len(suffix)].strip()) * multipliers[suffix])
    return int(raw)


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _load_target(path: str | Path):
    path = Path(path).resolve()
    if find_module_info(path):
        return [load_mod(path)]
    return discover_mods(path)


def cmd_scan(args: argparse.Namespace) -> int:
    mods = discover_mods(args.root, max_depth=args.max_depth)
    if args.json:
        print_json({"root": str(Path(args.root).resolve()), "count": len(mods), "mods": [mod.as_dict() for mod in mods]})
        return 0
    print(f"Найдено модов: {len(mods)}")
    print("РАЗМЕР\tФАЙЛЫ\tКОДИРОВКА\tИМЯ\tПУТЬ")
    for mod in mods:
        print(f"{human_size(mod.total_size)}\t{mod.file_count}\t{mod.module.encoding}\t{mod.name}\t{mod.relative_path}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    path = Path(args.mod).resolve()
    info_path = find_module_info(path)
    if info_path is None:
        raise FileNotFoundError(f"ModuleInfo.txt не найден: {path}")
    info = parse_module_info(info_path)
    if args.json:
        print_json(info.as_dict())
        return 0
    print(f"Имя: {info.name}")
    print(f"Автор: {info.author or '—'}")
    print(f"Раздел: {info.section or '—'}")
    print(f"Приоритет: {info.priority if info.priority is not None else '—'}")
    print(f"Языки: {', '.join(info.languages) or '—'}")
    print(f"Зависимости: {', '.join(info.dependencies) or '—'}")
    print(f"Конфликты: {', '.join(info.conflicts) or '—'}")
    print(f"Кодировка: {info.encoding}")
    print(f"Файл: {info.path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    mods = _load_target(args.root)
    issues = validate_collection(mods)
    if args.json:
        print_json({"mods": len(mods), "issues": [issue.as_dict() for issue in issues]})
    else:
        print(f"Проверено модов: {len(mods)}")
        if not issues:
            print("Проблем не найдено.")
        for issue in issues:
            location = f" [{issue.path}]" if issue.path else ""
            print(f"{issue.severity.upper():7} {issue.code}: {issue.message}{location}")
        totals = {level: sum(issue.severity == level for issue in issues) for level in ("error", "warning", "info")}
        print(f"Итог: ошибок {totals['error']}, предупреждений {totals['warning']}, сведений {totals['info']}.")
    return 2 if any(issue.severity == "error" for issue in issues) else 0


def _print_audit_report(report: AuditReport) -> None:
    value = report.as_dict()
    print(f"Цель: {report.target}")
    print(f"Профиль: {report.profile.value}; полное покрытие: {'да' if report.coverage_complete else 'НЕТ'}")
    for check in report.checks:
        print(f"{check.status.upper():11} {check.name}: файлов {len(check.checked_files)}, проблем {len(check.issues)}")
    if report.children:
        print(f"Модов в коллекции: {len(report.children)}")
    for issue in report.issues:
        marker = " [ПОДАВЛЕНО]" if issue.suppressed else ""
        location = f" [{issue.path}" if issue.path else ""
        if issue.location:
            location += f": {issue.location}"
        if location:
            location += "]"
        print(f"{issue.severity.upper():7} {issue.code}: {issue.message}{marker}{location}")
        if issue.evidence:
            print(f"         {issue.evidence}")
    summary = value["summary"]
    print(
        f"Итог: ошибок {summary['error']}, предупреждений {summary['warning']}, "
        f"сведений {summary['info']}, подавлено {summary['suppressed']}."
    )


def cmd_audit(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    if find_module_info(target):
        report = audit_mod(
            target,
            profile=args.profile,
            tools_root=args.tools_root,
            allow=args.allow,
        )
    else:
        report = audit_collection(
            target,
            profile=args.profile,
            tools_root=args.tools_root,
            allow=args.allow,
        )
    if args.json:
        print_json(report.as_dict())
    else:
        _print_audit_report(report)
    return 2 if report.blocking_issues(warnings_as_errors=args.warnings_as_errors) else 0


def cmd_release_check(args: argparse.Namespace) -> int:
    report = audit_mod(
        args.mod,
        profile=AuditProfile.RELEASE,
        tools_root=args.tools_root,
        allow=args.allow,
    )
    if args.json:
        print_json(report.as_dict())
    else:
        _print_audit_report(report)
    return 2 if report.blocking_issues(warnings_as_errors=args.warnings_as_errors) else 0


def cmd_release_build(args: argparse.Namespace) -> int:
    try:
        result = build_release(
            args.mod,
            args.output,
            prefix=args.prefix,
            exclude=args.exclude,
            tools_root=args.tools_root,
            allow=args.allow,
            warnings_as_errors=args.warnings_as_errors,
            overwrite=args.overwrite,
        )
    except ReleaseBlockedError as exc:
        if args.json:
            print_json({"schema": "srhd-modkit-release-v1", "blocked": True, "audit": exc.report.as_dict()})
        else:
            print(str(exc))
            _print_audit_report(exc.report)
        return 2
    if args.json:
        print_json(result.as_dict())
    else:
        print(f"Релиз: {result.output}")
        print(f"Файлов: {result.file_count}; размер: {human_size(result.archive_size)}")
        print(f"SHA-256: {result.sha256}")
        print(f"Манифест: {result.manifest_path}")
        print(f"Аудит: {result.audit_path}")
        print(f"Архив повторно проверен: {'да' if result.verified else 'НЕТ'}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    result = compare_trees(args.left, args.right)
    if args.json:
        print_json(result)
        return 0
    summary = result["summary"]
    print(f"Одинаковых: {summary['identical']}; изменённых: {summary['changed']}; добавлено: {summary['added']}; удалено: {summary['removed']}")
    for label, title in (("changed", "ИЗМЕНЕНО"), ("added", "ДОБАВЛЕНО СПРАВА"), ("removed", "ТОЛЬКО СЛЕВА")):
        for path in result[label]:
            print(f"{title}\t{path}")
    return 1 if summary["changed"] or summary["added"] or summary["removed"] else 0


def cmd_manifest(args: argparse.Namespace) -> int:
    manifest = build_manifest(args.root, exclude=args.exclude)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Манифест записан: {output}")
        print(f"Файлов: {manifest['file_count']}; размер: {human_size(manifest['total_size'])}")
    else:
        print_json(manifest)
    return 0


def cmd_duplicates(args: argparse.Namespace) -> int:
    groups = find_duplicates(args.root, min_size=args.min_size)
    total = sum(group["recoverable"] for group in groups)
    if args.json:
        print_json({"groups": groups, "recoverable": total})
        return 0
    print(f"Групп дублей: {len(groups)}; потенциально освобождается: {human_size(total)}")
    for group in groups:
        print(f"\n{group['copies']} копии × {human_size(group['size'])}; лишнее {human_size(group['recoverable'])}")
        for path in group["paths"]:
            print(f"  {path}")
    return 0


def cmd_collisions(args: argparse.Namespace) -> int:
    mods = discover_mods(args.root)
    collisions = find_collisions(mods, data_only=args.data_only, hash_files=args.hash)
    if args.json:
        print_json({"mods": len(mods), "collisions": collisions})
        return 0
    print(f"Модов: {len(mods)}; пересекающихся путей: {len(collisions)}")
    for item in collisions:
        suffix = " — одинаковые" if item["identical"] is True else " — разные" if item["identical"] is False else ""
        print(f"{item['path']}: {', '.join(item['mods'])}{suffix}")
    return 0


def cmd_compat(args: argparse.Namespace) -> int:
    report = analyze_modset(args.config, args.mods_root, tools_root=args.tools_root)
    if args.json:
        print_json(report.as_dict())
    else:
        print(f"Включено модов: {len(report.load_order)}")
        for item in report.load_order:
            priority = item["priority"] if item["priority"] is not None else "—"
            print(f"  [{item['order']}] Priority={priority} {item['name']} ({item['path']})")
        print(f"Зависимостей: {len(report.dependency_edges)}; объявленных конфликтов: {len(report.conflict_edges)}")
        print(f"Циклов зависимостей: {len(report.cycles)}")
        for cycle in report.cycles:
            print("  " + " -> ".join(cycle))
        print(f"Пересечений игровых путей: {len(report.collisions)}")
        for collision in report.collisions:
            mods = ", ".join(owner.mod for owner in collision.owners)
            print(f"  {collision.kind:24} {collision.path}: {mods}; resolution={collision.resolution}")
        for issue in report.issues:
            print(f"{issue.severity.upper():7} {issue.code}: {issue.message}")
    return 2 if any(issue.severity == "error" for issue in report.issues) else 0


def cmd_pack(args: argparse.Namespace) -> int:
    result = pack_mod(args.mod, args.output, prefix=args.prefix, exclude=args.exclude)
    if args.json:
        print_json(result)
    else:
        print(f"Архив: {result['output']}")
        print(f"Файлов: {result['file_count']}; размер: {human_size(result['archive_size'])}")
        print(f"SHA-256: {result['sha256']}")
    return 0


def cmd_modcfg(args: argparse.Namespace) -> int:
    config = parse_modcfg(args.config)
    issues = validate_modcfg(config, args.mods_root) if args.mods_root else []
    if args.json:
        value = config.as_dict()
        value["issues"] = [issue.as_dict() for issue in issues]
        print_json(value)
    else:
        print(f"Кодировка: {config.encoding}")
        print(f"Включено записей: {len(config.enabled)}")
        for item in config.enabled:
            print(f"  {item}")
        for issue in issues:
            print(f"{issue.severity.upper():7} {issue.code}: {issue.message}")
    return 2 if any(issue.severity == "error" for issue in issues) else 0


def cmd_formats(args: argparse.Namespace) -> int:
    path = Path(args.path).resolve()
    if path.is_file():
        result = inspect_file(path, include_hash=args.hash)
        if args.json:
            print_json(result)
        else:
            print(f"Формат: {result['format']} ({result['extension'] or 'без расширения'})")
            print(f"Категория: {result['category']}; обработка: {result['handling']}")
            print(f"Размер: {human_size(result['size'])}")
            if result.get("width"):
                print(f"Размер изображения: {result['width']} × {result['height']}")
            if result["signature_valid"] is not None:
                print(f"Сигнатура: {'верна' if result['signature_valid'] else 'НЕВЕРНА'}")
            if result["editor"]:
                print(f"Редактор: {result['editor']}")
            if result.get("sha256"):
                print(f"SHA-256: {result['sha256']}")
        return 2 if result["signature_valid"] is False else 0

    result = scan_formats(path, include_hash=args.hash)
    if args.json:
        print_json(result)
        return 2 if result["invalid_signatures"] else 0
    print(f"Файлов: {result['file_count']}; размер: {human_size(result['total_size'])}")
    print("ФАЙЛОВ\tРАЗМЕР\tРЕЖИМ\tФОРМАТ")
    for item in result["extensions"]:
        print(f"{item['count']}\t{human_size(item['size'])}\t{item['handling']}\t{item['extension']}: {item['format']}")
    if result["invalid_signatures"]:
        print(f"Неверных сигнатур: {len(result['invalid_signatures'])}")
        for item in result["invalid_signatures"]:
            print(f"  {item['path']}")
    return 2 if result["invalid_signatures"] else 0


def cmd_resource_info(args: argparse.Namespace) -> int:
    result = inspect_resource(args.source)
    if args.json:
        print_json(result)
    else:
        print(f"Формат: {result['format']}; размер: {human_size(result['size'])}")
        if result["format"] == "GI image":
            print(
                f"Размер: {result['width']} × {result['height']}; "
                f"тип: {result['frame_type']}; слоёв: {result['layer_count']}"
            )
        if result["format"].startswith(("GAI", "HAI")):
            print(f"Кадров: {result['frame_count']}; размер: {result['width']} × {result['height']}")
        if result["format"] == "HAI animation/image":
            layout = "стандартная" if result["standard_layout"] else "расширенная/неизученная"
            print(
                f"Разметка кадров: {layout}; физически {result['physical_frame_size']} байт, "
                f"заявлено {result['nominal_frame_size']}"
            )
        if result["format"] == "resource package":
            print(f"Путь внутри: {'/'.join(result['folders'])}")
            print(f"Файлов: {result['file_count']}; после распаковки: {human_size(result['uncompressed_size'])}")
        print("Headless-возможности: " + ", ".join(result["capabilities"]))
    return 0


def cmd_resource_list(args: argparse.Namespace) -> int:
    result = inspect_resource(args.source, listing=True)
    if args.json:
        print_json(result)
    elif "files" in result:
        print(f"PKG-файлов: {len(result['files'])}; путь: {'/'.join(result['folders'])}")
        for item in result["files"]:
            print(
                f"{item['index']:4}  {human_size(item['uncompressed_size']):>10}  "
                f"{human_size(item['compressed_size']):>10}  {item['name']}"
            )
    elif "frames" in result:
        print(f"Кадров: {len(result['frames'])}")
        for item in result["frames"]:
            dimensions = ""
            if "width" in item:
                dimensions = f"  {item['width']}×{item['height']}"
            print(f"{item['index']:4}  {item['offset']:10}  {human_size(item['size']):>10}{dimensions}")
    else:
        print(f"Слоёв GI: {len(result['layers'])}")
        for item in result["layers"]:
            print(
                f"{item['index']:4}  {item['offset']:10}  {human_size(item['size']):>10}  "
                f"{item['width']}×{item['height']}"
            )
    return 0


def cmd_resource_verify(args: argparse.Namespace) -> int:
    result = verify_resource(args.source)
    if args.json:
        print_json(result)
    elif result["format"] == "resource package":
        print(
            f"PKG корректен: распаковано и проверено {result['verified_files']} файлов, "
            f"{human_size(result['verified_uncompressed_size'])}"
        )
    else:
        print(f"{result['format']} корректен: структура и границы данных проверены.")
    return 0


def cmd_resource_extract(args: argparse.Namespace) -> int:
    result = extract_resource(args.source, args.output, overwrite=args.overwrite)
    if args.json:
        print_json(result)
    else:
        print(f"Извлечено файлов: {result['files']}; размер: {human_size(result['written_size'])}")
        print(f"Каталог: {result['output']}")
        if result.get("package_root"):
            print(f"Корень содержимого PKG: {result['package_root']}")
    return 0


def cmd_resource_build_gai(args: argparse.Namespace) -> int:
    result = build_gai(
        args.frames,
        args.output,
        template=args.template,
        width=args.width,
        height=args.height,
        overwrite=args.overwrite,
    )
    if args.json:
        print_json(result)
    else:
        print(f"GAI: {result['output']}")
        print(f"Кадров: {result['frames']}; холст: {result['width']} × {result['height']}")
        print(f"SHA-256: {result['sha256']}; проверен: {'да' if result['verified'] else 'НЕТ'}")
    return 0


def cmd_resource_build_pkg(args: argparse.Namespace) -> int:
    result = build_pkg(
        args.source,
        args.output,
        package_folders=args.folders,
        template=args.template,
        chunk_size=args.chunk_size,
        overwrite=args.overwrite,
    )
    if args.json:
        print_json(result)
    else:
        print(f"PKG: {result['output']}")
        print(f"Файлов: {result['files']}; после распаковки: {human_size(result['uncompressed_size'])}")
        print(f"SHA-256: {result['sha256']}; проверен: {'да' if result['verified'] else 'НЕТ'}")
    return 0


def _print_quest_issues(result: dict[str, Any]) -> None:
    for issue in result.get("issues", []):
        suffix = f" [{issue['location']}]" if issue.get("location") else ""
        print(f"{issue['severity'].upper():7} {issue['code']}: {issue['message']}{suffix}")
        if issue.get("evidence"):
            print(f"         {issue['evidence']}")


def cmd_quest_info(args: argparse.Namespace) -> int:
    result = inspect_quest(args.source)
    document = load_quest(args.source)
    result["media"] = {key: list(value) for key, value in quest_media(document).items()}
    if args.json:
        print_json(result)
    else:
        print(
            f"{result['format']} {result['header_hex']}: параметров {result['parameters']}, "
            f"локаций {result['locations']}, переходов {result['jumps']}"
        )
        print(f"Поведение старого TGE: {'да' if result['old_tge_behaviour'] else 'нет'}")
        for kind, values in result["media"].items():
            print(f"{kind}: {len(values)}")
        _print_quest_issues(result)
    return 2 if not result["valid"] else 0


def cmd_quest_validate(args: argparse.Namespace) -> int:
    result = inspect_quest(args.source)
    if args.json:
        print_json(result)
    else:
        _print_quest_issues(result)
        if result["valid"]:
            print(
                f"Квест корректен: {result['parameters']} параметров, "
                f"{result['locations']} локаций, {result['jumps']} переходов."
            )
    return 2 if not result["valid"] else 0


def cmd_quest_roundtrip(args: argparse.Namespace) -> int:
    result = verify_quest(args.source)
    if args.json:
        print_json(result)
    else:
        _print_quest_issues(result)
        print(
            "Round-trip: "
            + ("пройден, сборка детерминирована" if result["roundtrip"] else "НЕ ПРОЙДЕН")
        )
        print(f"SHA-256 контрольного QMM: {result['rebuilt_sha256']}")
    return 2 if not result["verified"] else 0


def cmd_quest_export_json(args: argparse.Namespace) -> int:
    result = export_quest_json(args.source, args.output, overwrite=args.overwrite)
    if args.json:
        print_json(result)
    else:
        print(f"JSON квеста: {result['output']}")
        print(
            f"Параметров {result['parameters']}, локаций {result['locations']}, "
            f"переходов {result['jumps']}"
        )
    return 0


def cmd_quest_build(args: argparse.Namespace) -> int:
    result = build_quest_from_json(args.source, args.output, overwrite=args.overwrite)
    if args.json:
        print_json(result)
    else:
        print(f"QMM: {result['output']}")
        print(f"SHA-256: {result['sha256']}")
        print("Контрольная загрузка и повторная сборка: пройдены")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    chain = Toolchain(args.tools_root)
    status = chain.status()
    if args.json:
        print_json({"tools_root": str(chain.tools_root), "tools": status, "formats": [item.as_dict() for item in format_catalog()]})
    else:
        print(f"Каталог инструментов: {chain.tools_root}")
        for item in status:
            mark = "OK" if item["available"] else "НЕТ"
            mode = "автоматически" if item["automatic"] else "через редактор"
            print(f"{mark:3} {item['name']:12} {mode:16} {item['path']}")
            print(f"    {item['purpose']}")
    return 2 if any(not item["available"] for item in status) else 0


def cmd_doctor_processes(args: argparse.Namespace) -> int:
    result = terminate_hidden_processes() if args.terminate else inspect_hidden_processes()
    current = result["remaining"] if args.terminate else result
    if args.json:
        print_json(result)
    else:
        if args.terminate:
            for action in result["actions"]:
                suffix = f" ({action.get('executable')})" if action.get("executable") else ""
                print(f"{action['status'].upper():10} PID {action['pid']}{suffix}")
                if action.get("error"):
                    print(f"           {action['error']}")
                if action.get("reason"):
                    print(f"           {action['reason']}")
        if current["desktop_count"] == 0:
            print("Скрытых процессов SRHD ModKit не найдено.")
        else:
            print(
                f"Скрытых desktop: {current['desktop_count']}; "
                f"процессов: {current['process_count']}."
            )
            for desktop in current["desktops"]:
                print(desktop["name"])
                if not desktop["processes"]:
                    print("  PID не обнаружен; desktop всё ещё занят или завершается")
                for process in desktop["processes"]:
                    executable = process.get("executable") or process.get("name") or "неизвестно"
                    print(
                        f"  PID {process['pid']} (родитель {process.get('parent_pid')}): "
                        f"{executable}"
                    )
    return 0 if current["desktop_count"] == 0 else 2


def cmd_convert(args: argparse.Namespace) -> int:
    chain = Toolchain(args.tools_root)
    items = chain.convert(
        args.inputs,
        args.output,
        direction=args.direction,
        gi_mode=args.mode,
        overwrite=args.overwrite,
    )
    value = {
        "direction": args.direction,
        "count": len(items),
        "items": [item.as_dict() for item in items],
    }
    if args.json:
        print_json(value)
    else:
        print(f"Преобразовано файлов: {len(items)}")
        for item in items:
            print(f"{item.source} -> {item.destination} ({human_size(item.destination_size)})")
    return 0


def cmd_open_editor(args: argparse.Namespace) -> int:
    result = Toolchain(args.tools_root).open_editor(args.file, allow_gui=args.allow_gui)
    if args.json:
        print_json(result)
    else:
        print(f"Файл передан редактору: {result['file']}")
        print(f"Редактор: {result['tool']} ({result['executable']})")
        if result.get("note"):
            print(result["note"])
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    result = stage_tree(args.source, args.destination)
    if args.json:
        print_json(result)
    else:
        print(f"Проверенная копия: {result['destination']}")
        print(f"Файлов: {result['file_count']}; размер: {human_size(result['total_size'])}")
    return 0


def _load_dat_source(path: str | Path, chain: Toolchain, temp: Path) -> tuple[BlockParDocument, Path]:
    path = Path(path).resolve()
    if path.suffix.casefold() == ".txt":
        return load_blockpar(path), path
    if path.suffix.casefold() != ".dat":
        raise ValueError("Ожидается DAT или экспортированный BlockPar TXT")
    exported = temp / path.with_suffix(".txt").name
    chain.convert_dat(path, exported)
    return load_blockpar(exported), exported


def _walk_blockpar(nodes: list[BlockParNode], prefix: str = ""):
    counts: dict[str, int] = {}
    for node in nodes:
        folded = node.name.casefold()
        counts[folded] = counts.get(folded, 0) + 1
        same_names = sum(item.name.casefold() == folded for item in nodes)
        label = f"{node.name}[{counts[folded]}]" if same_names > 1 else node.name
        path = f"{prefix}/{label}" if prefix else label
        yield path, node
        yield from _walk_blockpar(node.children, path)


def cmd_dat_convert(args: argparse.Namespace) -> int:
    result = Toolchain(args.tools_root).convert_dat(
        args.source,
        args.output,
        overwrite=args.overwrite,
        verify=not args.no_verify,
    )
    if args.json:
        print_json(result)
    else:
        print(f"Результат: {result['destination']}")
        print(f"Проверка дерева: {'пройдена' if result['verified'] else 'отключена'}")
        print(f"SHA-256: {result['destination_sha256']}")
    return 0


def cmd_dat_tree(args: argparse.Namespace) -> int:
    chain = Toolchain(args.tools_root)
    with tempfile.TemporaryDirectory(prefix="srhd-dat-tree-") as name:
        document, _ = _load_dat_source(args.source, chain, Path(name))
        if args.json:
            print_json(document.as_dict(include_raw=args.include_raw))
            return 0
        for path, node in _walk_blockpar(document.roots):
            print(f"{path}\tпараметров={len(node.parameters)}\tблоков={len(node.children)}")
    return 0


def cmd_dat_get(args: argparse.Namespace) -> int:
    chain = Toolchain(args.tools_root)
    with tempfile.TemporaryDirectory(prefix="srhd-dat-get-") as name:
        document, _ = _load_dat_source(args.source, chain, Path(name))
        node = document.find_node(args.node)
        if args.key is None:
            value: Any = node.as_dict(include_raw=args.include_raw)
        else:
            params = node.parameters_named(args.key, case_sensitive=args.case_sensitive)
            if not params:
                raise KeyError(f"Параметр не найден: {args.key}")
            value = [item.value for item in params]
        if args.json:
            print_json({"source": str(Path(args.source).resolve()), "node": args.node, "key": args.key, "value": value})
        elif isinstance(value, list):
            for item in value:
                print(item)
        else:
            print_json(value)
    return 0


def cmd_dat_set(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if output.exists() and not args.overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")
    if source.suffix.casefold() != output.suffix.casefold():
        raise ValueError("dat set сохраняет тот же тип: DAT -> DAT или TXT -> TXT")
    chain = Toolchain(args.tools_root)
    with tempfile.TemporaryDirectory(prefix="srhd-dat-set-") as name:
        temp = Path(name)
        document, _ = _load_dat_source(source, chain, temp)
        node = document.find_node(args.node)
        changed = node.set_parameter(args.key, args.value, create=args.create, all_matches=args.all)
        if source.suffix.casefold() == ".txt":
            document.save(output)
            load_blockpar(output)
            result = {"destination": str(output), "destination_sha256": sha256_file(output), "verified": True}
        else:
            editable = temp / source.with_suffix(".txt").name
            document.save(editable, encoding="utf-8", bom=False)
            result = chain.convert_dat(editable, output, overwrite=args.overwrite, verify=True)
        result.update({"node": args.node, "key": args.key, "value": args.value, "changed": changed})
        if args.json:
            print_json(result)
        else:
            print(f"Изменено параметров: {changed}")
            print(f"Результат: {result['destination']}")
            print("Обратная проверка дерева: пройдена")
    return 0


def cmd_dat_patch(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    patch_path = Path(args.patch).resolve()
    if output.exists() and not args.overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")
    if source.suffix.casefold() != output.suffix.casefold() or source.suffix.casefold() not in {".dat", ".txt"}:
        raise ValueError("dat patch сохраняет тот же тип: DAT -> DAT или TXT -> TXT")
    payload = json.loads(patch_path.read_text(encoding="utf-8-sig"))
    operations = payload.get("operations") if isinstance(payload, dict) else payload
    if not isinstance(operations, list):
        raise ValueError("JSON-патч должен быть массивом операций или объектом с полем operations")

    chain = Toolchain(args.tools_root)
    with tempfile.TemporaryDirectory(prefix="srhd-dat-patch-") as name:
        temp = Path(name)
        document, _ = _load_dat_source(source, chain, temp)
        changes = document.apply_operations(operations)
        if source.suffix.casefold() == ".txt":
            document.save(output)
            load_blockpar(output)
            result = {"destination": str(output), "destination_sha256": sha256_file(output), "verified": True}
        else:
            # The source basename matters for BlockPar's encryption mode (notably CacheData.dat).
            editable = temp / source.with_suffix(".txt").name
            document.save(editable, encoding="utf-8", bom=False)
            result = chain.convert_dat(editable, output, overwrite=args.overwrite, verify=True)
    result.update({"patch": str(patch_path), "operations": len(operations), "changes": changes})
    if args.json:
        print_json(result)
    else:
        print(f"Применено операций: {len(operations)}")
        print(f"Результат: {result['destination']}")
        print("Обратная проверка дерева: пройдена")
    return 0


def cmd_dat_validate(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    if source.suffix.casefold() == ".dat" and is_empty_rscript_lang_dat(source):
        result = {
            "source": str(source),
            "encoding": "utf-16-le-bom",
            "format": "rscript-empty-lang-dat",
            "roots": 0,
            "nodes": 0,
            "parameters": 0,
            "valid": True,
            "temporary_export": None,
        }
        if args.json:
            print_json(result)
        else:
            print("Пустой RScript DATA/Script/Lang.dat корректен: UTF-16LE BOM FF FE, записей 0")
        return 0

    chain = Toolchain(args.tools_root)
    with tempfile.TemporaryDirectory(prefix="srhd-dat-validate-") as name:
        document, exported = _load_dat_source(args.source, chain, Path(name))
        nodes = list(_walk_blockpar(document.roots))
        result = {
            "source": str(Path(args.source).resolve()),
            "encoding": document.encoding,
            "roots": len(document.roots),
            "nodes": len(nodes),
            "parameters": sum(len(node.parameters) for _, node in nodes),
            "valid": True,
            "temporary_export": str(exported) if Path(args.source).suffix.casefold() == ".txt" else None,
        }
        if args.json:
            print_json(result)
        else:
            print(f"BlockPar корректен: корней {result['roots']}, блоков {result['nodes']}, параметров {result['parameters']}")
            print(f"Кодировка текстового представления: {result['encoding']}")
    return 0


def cmd_script_info(args: argparse.Namespace) -> int:
    project = load_rson(args.source)
    value = project.summary()
    value["issues"] = [issue.as_dict() for issue in project.validate()]
    if args.json:
        print_json(value)
    else:
        print(f"Скрипт: {value['name']}")
        print(f"RSON: FileID={value['file_id']}, версия={value['file_version']}")
        print(f"Объектов: {value['objects']}; связей: {value['links']}; строк кода: {value['code_lines']}")
        print("Типы: " + ", ".join(f"{key}={count}" for key, count in value["types"].items()))
        for subscription in value["state_event_subscriptions"]:
            print(
                f"События TState #{subscription['object_id']} {subscription['name']}: "
                + ", ".join(subscription["events"])
            )
        for issue in project.validate():
            print(f"{issue.severity.upper():7} {issue.code}: {issue.message}")
    return 2 if any(issue.severity == "error" for issue in project.validate()) else 0


def cmd_script_validate(args: argparse.Namespace) -> int:
    project = load_rson(args.source)
    issues = project.validate()
    if args.json:
        print_json({"source": str(Path(args.source).resolve()), "valid": not issues, "issues": [item.as_dict() for item in issues]})
    elif not issues:
        print("RSON корректен: идентификаторы, родители, связи и массивы кода проверены.")
    else:
        for issue in issues:
            suffix = f" [{issue.location}]" if issue.location else ""
            print(f"{issue.severity.upper():7} {issue.code}: {issue.message}{suffix}")
    return 2 if any(issue.severity == "error" for issue in issues) else 0


def cmd_script_search(args: argparse.Namespace) -> int:
    results = load_rson(args.source).search_code(args.query, case_sensitive=args.case_sensitive)
    if args.json:
        print_json({"count": len(results), "results": results})
    else:
        print(f"Совпадений: {len(results)}")
        for item in results:
            print(f"#{item['object_id']} {item['type']} {item['field']}:{item['line']}\t{item['text']}")
    return 0


def cmd_script_get(args: argparse.Namespace) -> int:
    project = load_rson(args.source)
    item = project.object_by_id(args.id)
    if args.json:
        print_json(item)
    else:
        print_json(item)
    return 0


def cmd_script_list_links(args: argparse.Namespace) -> int:
    project = load_rson(args.source)
    links = project.data.get("Visual.Links", [])
    if not isinstance(links, list):
        raise ValueError("Visual.Links должен быть массивом")
    result = {"source": str(Path(args.source).resolve()), "count": len(links), "links": links}
    if args.json:
        print_json(result)
    else:
        print(f"Связей: {len(links)}")
        for index, link in enumerate(links):
            if isinstance(link, dict):
                print(
                    f"[{index}] #{link.get('Begin')} -> #{link.get('End')}; "
                    f"Nom={link.get('Nom')}; Arrow={link.get('Arrow')}"
                )
            else:
                print(f"[{index}] некорректная запись: {link!r}")
    return 0


def cmd_script_set_code(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if output.exists() and not args.overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")
    code_file = Path(args.code_file).resolve()
    decoded = code_file.read_text(encoding=args.encoding)
    lines = decoded.splitlines()
    project = load_rson(source)
    project.set_code(args.id, lines, field=args.field)
    issues = project.validate()
    if any(issue.severity == "error" for issue in issues):
        raise ValueError("После изменения RSON не прошёл проверку")
    project.save(output)
    result = {"output": str(output), "object_id": args.id, "field": args.field, "lines": len(lines), "sha256": sha256_file(output)}
    if args.json:
        print_json(result)
    else:
        print(f"Код записан: объект #{args.id}, поле {args.field}, строк {len(lines)}")
        print(f"RSON: {output}")
    return 0


def cmd_script_set_field(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if output.exists() and not args.overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError as exc:
        raise ValueError("--value должен быть корректным JSON: строку заключайте в двойные кавычки JSON") from exc
    project = load_rson(source)
    project.set_field(args.id, args.field, value)
    issues = project.validate()
    errors = [item for item in issues if item.severity == "error"]
    if errors:
        raise ValueError(f"После изменения RSON не прошёл проверку: {errors[0].message}")
    project.save(output)
    result = {
        "output": str(output),
        "object_id": args.id,
        "field": args.field,
        "value": value,
        "sha256": sha256_file(output),
    }
    if args.json:
        print_json(result)
    else:
        print(f"Поле записано: объект #{args.id}, {args.field}={json.dumps(value, ensure_ascii=False)}")
        print(f"RSON: {output}")
    return 0


def cmd_script_set_events(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if output.exists() and not args.overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")
    project = load_rson(source)
    events = [] if args.clear else args.events
    project.set_state_events(args.id, events)
    issues = project.validate()
    if any(issue.severity == "error" for issue in issues):
        raise ValueError("После изменения событий RSON не прошёл проверку")
    project.save(output)
    result = {
        "output": str(output),
        "object_id": args.id,
        "events": project.state_events(args.id),
        "sha256": sha256_file(output),
    }
    if args.json:
        print_json(result)
    else:
        label = ", ".join(result["events"]) if result["events"] else "очищены"
        print(f"События TState #{args.id}: {label}")
        print(f"RSON: {output}")
    return 0


def _runtime_lint_target(
    target: str | Path,
    *,
    tools_root: str | Path | None = None,
    main_path: str | Path | None = None,
    module_info_path: str | Path | None = None,
) -> dict[str, Any]:
    target = Path(target).resolve()
    chain = Toolchain(tools_root)
    issues: list[RuntimeIssue] = []
    checked_rson: list[str] = []
    checked_main: list[str] = []
    onstart_script_run = False

    if target.is_file():
        if target.suffix.casefold() != ".rson":
            raise ValueError("Файловая цель lint-runtime должна быть RSON")
        rson_files = [target]
        root = target.parent
    elif target.is_dir():
        root = target
        source_root = root / "SOURCE"
        search_root = source_root if source_root.is_dir() else root
        rson_files = sorted(
            path
            for path in search_root.rglob("*.rson")
            if not any(part.casefold().startswith(".srhd-") for part in path.parts)
        )
    else:
        raise FileNotFoundError(target)

    for path in rson_files:
        try:
            project = load_rson(path)
            structural = [item for item in project.validate() if item.severity == "error"]
            if structural:
                issues.append(
                    RuntimeIssue(
                        "error",
                        "runtime-rson-structural",
                        f"Смысловой анализ невозможен: {structural[0].message}",
                        str(path),
                        structural[0].location,
                    )
                )
            else:
                issues.extend(lint_rson_runtime(project))
            checked_rson.append(str(path))
        except Exception as exc:
            issues.append(RuntimeIssue("error", "runtime-rson-load", str(exc), str(path)))

    main_candidates: list[Path] = []
    if main_path:
        main_candidates.append(Path(main_path).resolve())
    elif target.is_dir():
        for candidate in (root / "CFG" / "Main.dat", root / "SOURCE" / "CFG" / "Main.txt"):
            if candidate.is_file():
                main_candidates.append(candidate.resolve())

    with tempfile.TemporaryDirectory(prefix="srhd-runtime-lint-") as name:
        temp = Path(name)
        for index, path in enumerate(main_candidates):
            try:
                workspace = temp / str(index)
                workspace.mkdir(parents=True, exist_ok=True)
                document, _ = _load_dat_source(path, chain, workspace)
                issues.extend(lint_main_runtime(document, path))
                onstart_script_run = onstart_script_run or has_onstart_script_run(document)
                checked_main.append(str(path))
            except Exception as exc:
                issues.append(RuntimeIssue("error", "runtime-main-load", str(exc), str(path)))

    info_path: Path | None
    if module_info_path:
        info_path = Path(module_info_path).resolve()
    elif target.is_dir():
        info_path = find_module_info(root)
    else:
        info_path = None
    if info_path:
        try:
            issues.extend(lint_module_runtime(parse_module_info(info_path)))
        except Exception as exc:
            issues.append(RuntimeIssue("error", "runtime-module-load", str(exc), str(info_path)))

    onstart_risks = {
        "runtime-turn-direct-world-access",
        "runtime-turn-before-ui",
        "runtime-ui-readiness-source-missing",
    }
    if onstart_script_run and any(issue.code in onstart_risks for issue in issues):
        issues.append(
            RuntimeIssue(
                "error",
                "runtime-onstart-unguarded-world",
                "Мод запускается из OnStart, а его пошаговая цепочка достигает Player/мира без доказанного барьера t_OnEnteringForm",
                str(target),
            )
        )

    return {
        "target": str(target),
        "rson": checked_rson,
        "main": checked_main,
        "module_info": str(info_path) if info_path else None,
        "issues": [issue.as_dict() for issue in issues],
    }


def cmd_script_lint_runtime(args: argparse.Namespace) -> int:
    result = _runtime_lint_target(
        args.target,
        tools_root=args.tools_root,
        main_path=args.main,
        module_info_path=args.module_info,
    )
    issues = result["issues"]
    if args.json:
        print_json(result)
    elif not issues:
        print("Опасных runtime-шаблонов не найдено.")
    else:
        for issue in issues:
            suffix = f" [{issue['path']}" if issue.get("path") else ""
            if issue.get("location"):
                suffix += f": {issue['location']}"
            if suffix:
                suffix += "]"
            print(f"{issue['severity'].upper():7} {issue['code']}: {issue['message']}{suffix}")
            if issue.get("evidence"):
                print(f"         {issue['evidence']}")
    has_errors = any(issue["severity"] == "error" for issue in issues)
    has_warnings = any(issue["severity"] == "warning" for issue in issues)
    return 2 if has_errors or (args.strict and has_warnings) else 0


def _rson_mutation(source: str, output: str, overwrite: bool):
    destination = Path(output).resolve()
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Результат уже существует: {destination}")
    return load_rson(source), destination


def _save_valid_rson(project, output: Path) -> str:
    errors = [issue for issue in project.validate() if issue.severity == "error"]
    if errors:
        raise ValueError(f"После изменения RSON не прошёл проверку: {errors[0].message}")
    project.save(output)
    return sha256_file(output)


def cmd_script_clone_object(args: argparse.Namespace) -> int:
    project, output = _rson_mutation(args.source, args.output, args.overwrite)
    clone = project.clone_object(args.id, name=args.name)
    digest = _save_valid_rson(project, output)
    result = {"output": str(output), "source_object_id": args.id, "object": clone, "sha256": digest}
    if args.json:
        print_json(result)
    else:
        print(f"Объект #{args.id} клонирован как #{clone['#']} ({clone.get('Type')}, {clone.get('Name', '—')})")
        print(f"RSON: {output}")
    return 0


def cmd_script_add_link(args: argparse.Namespace) -> int:
    project, output = _rson_mutation(args.source, args.output, args.overwrite)
    link = project.add_link(args.begin, args.end, nom=args.nom, arrow=not args.no_arrow)
    digest = _save_valid_rson(project, output)
    result = {"output": str(output), "link": link, "sha256": digest}
    if args.json:
        print_json(result)
    else:
        print(f"Связь добавлена: #{args.begin} -> #{args.end}, Nom={args.nom}, Arrow={link['Arrow']}")
        print(f"RSON: {output}")
    return 0


def cmd_script_delete_link(args: argparse.Namespace) -> int:
    project, output = _rson_mutation(args.source, args.output, args.overwrite)
    link = project.delete_link(args.index)
    digest = _save_valid_rson(project, output)
    result = {"output": str(output), "index": args.index, "removed_link": link, "sha256": digest}
    if args.json:
        print_json(result)
    else:
        print(f"Связь [{args.index}] удалена: #{link.get('Begin')} -> #{link.get('End')}")
        print(f"RSON: {output}")
    return 0


def cmd_script_delete_object(args: argparse.Namespace) -> int:
    project, output = _rson_mutation(args.source, args.output, args.overwrite)
    removed = project.delete_object(args.id, detach_references=args.detach_references)
    digest = _save_valid_rson(project, output)
    result = {"output": str(output), "object_id": args.id, **removed, "sha256": digest}
    if args.json:
        print_json(result)
    else:
        print(
            f"Объект #{args.id} удалён; связей удалено: {removed['removed_links']}; "
            f"детей отвязано: {len(removed['detached_children'])}"
        )
        print(f"RSON: {output}")
    return 0


def cmd_script_register(args: argparse.Namespace) -> int:
    source = Path(args.main_dat).resolve()
    output = Path(args.output).resolve()
    if source.suffix.casefold() != ".dat" or output.suffix.casefold() != ".dat":
        raise ValueError("Регистрация выполняется из Main.dat в новый Main.dat")
    if output.exists() and not args.overwrite:
        raise FileExistsError(f"Результат уже существует: {output}")
    if bool(args.startup_key) != bool(args.startup_code):
        raise ValueError("--startup-key и --startup-code указываются только вместе")
    chain = Toolchain(args.tools_root)
    with tempfile.TemporaryDirectory(prefix="srhd-script-register-") as name:
        temp = Path(name)
        document, _ = _load_dat_source(source, chain, temp)
        scripts = document.ensure_node("Data/Script")
        registration = f"{args.flag},Script.{args.name}"
        scripts.set_parameter(args.name, registration, create=True)
        if args.startup_key:
            startup = document.ensure_node("BV/OnStart/0DayScripts")
            startup.set_parameter(args.startup_key, args.startup_code, create=True)
        runtime_issues = lint_main_runtime(document, source)
        runtime_errors = [issue for issue in runtime_issues if issue.severity == "error"]
        if runtime_errors:
            first = runtime_errors[0]
            raise ValueError(f"Main.dat не прошёл runtime-lint ({first.code}): {first.message}")
        editable = temp / "Main.txt"
        document.save(editable, encoding="utf-8", bom=False)
        result = chain.convert_dat(editable, output, overwrite=args.overwrite, verify=True)
    result.update(
        {
            "script": args.name,
            "registration": registration,
            "startup_key": args.startup_key,
            "startup_code": args.startup_code,
            "runtime_warnings": [
                issue.as_dict() for issue in runtime_issues if issue.severity == "warning"
            ],
        }
    )
    if args.json:
        print_json(result)
    else:
        print(f"Зарегистрировано: {args.name}={registration}")
        if args.startup_key:
            print(f"Стартовый вызов: {args.startup_key}={args.startup_code}")
        print(f"Main.dat: {output}")
    return 0


def _find_mod_root(path: str | Path) -> Path | None:
    """Return the nearest parent that looks like a complete SRHD mod."""
    current = Path(path).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "ModuleInfo.txt").is_file():
            return candidate
    return None


def _script_registrations(document: BlockParDocument) -> dict[str, list[str]]:
    registrations: dict[str, list[str]] = {}
    try:
        node = document.find_node("Data/Script")
    except KeyError:
        return registrations
    for parameter in node.parameters:
        registrations.setdefault(parameter.key.casefold(), []).append(parameter.value)
    return registrations


def _script_artifact_lint_target(
    root: str | Path,
    *,
    tools_root: str | Path | None = None,
    scripts: Sequence[str | Path] | None = None,
    registrations: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    chain = Toolchain(tools_root)
    issues: list[ScriptArtifactIssue] = []
    checked_cache: list[str] = []
    script_root = root / "DATA" / "Script"
    actual_scripts = sorted(script_root.glob("*.scr")) if script_root.is_dir() else []
    combined_scripts = [*actual_scripts, *(scripts or ())]
    local_scripts = list({Path(path).name.casefold(): Path(path) for path in combined_scripts}.values())

    with tempfile.TemporaryDirectory(prefix="srhd-script-artifacts-") as name:
        temp = Path(name)
        if registrations is None:
            registrations = {}
            main_candidates = (root / "CFG" / "Main.dat", root / "SOURCE" / "CFG" / "Main.txt")
            for index, path in enumerate(main_candidates):
                if not path.is_file():
                    continue
                try:
                    workspace = temp / f"main-{index}"
                    workspace.mkdir()
                    document, _ = _load_dat_source(path, chain, workspace)
                    registrations = _script_registrations(document)
                    break
                except Exception as exc:
                    issues.append(
                        ScriptArtifactIssue("error", "artifact-main-load", str(exc), str(path.resolve()))
                    )

        cache_documents: list[tuple[Path, BlockParDocument]] = []
        cache_candidates = (
            root / "SOURCE" / "CFG" / "CacheData.txt",
            root / "CFG" / "CacheData.txt",
            root / "CFG" / "CacheData.dat",
        )
        for index, path in enumerate(cache_candidates):
            if not path.is_file():
                continue
            try:
                workspace = temp / f"cache-{index}"
                workspace.mkdir()
                document, _ = _load_dat_source(path, chain, workspace)
                cache_documents.append((path.resolve(), document))
                checked_cache.append(str(path.resolve()))
            except Exception as exc:
                issues.append(
                    ScriptArtifactIssue("error", "cachedata-load", str(exc), str(path.resolve()))
                )
        issues.extend(lint_script_cache(root, local_scripts, registrations, cache_documents))

    return {
        "mod": str(root),
        "scripts": [str(Path(path).resolve()) for path in local_scripts],
        "registrations": registrations,
        "cachedata": checked_cache,
        "issues": [issue.as_dict() for issue in issues],
    }


def _game_text_lint_target(
    root: str | Path,
    *,
    tools_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    chain = Toolchain(tools_root)
    issues: list[GameTextIssue] = []
    checked: list[str] = []

    module_info = root / "ModuleInfo.txt"
    if module_info.is_file():
        try:
            issues.extend(
                lint_game_text(
                    read_text(module_info),
                    module_info,
                    allowed_encodings={"cp1251", "utf-16-le", "utf-16-be"},
                )
            )
            checked.append(str(module_info.resolve()))
        except Exception as exc:
            issues.append(GameTextIssue("error", "game-text-load", str(exc), str(module_info.resolve())))

    source_cfg = root / "SOURCE" / "CFG"
    if source_cfg.is_dir():
        for path in sorted(source_cfg.rglob("*.txt")):
            try:
                decoded = read_text(path)
                russian_target = "rus" in {part.casefold() for part in path.relative_to(source_cfg).parts[:-1]}
                russian_target = russian_target or "_rus" in path.stem.casefold()
                issues.extend(
                    lint_game_text(
                        decoded,
                        path,
                        require_cp1251_representable=russian_target,
                    )
                )
                checked.append(str(path.resolve()))
            except Exception as exc:
                issues.append(GameTextIssue("error", "game-text-load", str(exc), str(path.resolve())))

    for path in sorted(root.rglob("*.rson")):
        try:
            issues.extend(lint_game_text(read_text(path), path))
            checked.append(str(path.resolve()))
        except Exception as exc:
            issues.append(GameTextIssue("error", "game-text-load", str(exc), str(path.resolve())))

    rus_cfg = root / "CFG" / "Rus"
    if rus_cfg.is_dir():
        with tempfile.TemporaryDirectory(prefix="srhd-game-text-") as name:
            temp = Path(name)
            for index, path in enumerate(sorted(rus_cfg.rglob("*"))):
                if not path.is_file() or path.suffix.casefold() not in {".txt", ".dat"}:
                    continue
                try:
                    if path.suffix.casefold() == ".dat":
                        workspace = temp / str(index)
                        workspace.mkdir()
                        document, _ = _load_dat_source(path, chain, workspace)
                        decoded = DecodedText(
                            document.to_text(include_raw=False),
                            document.encoding,
                            document.had_bom,
                        )
                    else:
                        decoded = read_text(path)
                    issues.extend(lint_game_text(decoded, path, require_cp1251=True))
                    checked.append(str(path.resolve()))
                except Exception as exc:
                    issues.append(GameTextIssue("error", "game-text-load", str(exc), str(path.resolve())))

    return {
        "mod": str(root),
        "checked": checked,
        "issues": [issue.as_dict() for issue in issues],
    }


def cmd_script_build(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    mod_root = _find_mod_root(source)
    preflight: dict[str, Any] | None = None
    if mod_root is not None:
        preflight = _runtime_lint_target(mod_root, tools_root=args.tools_root)
        runtime_errors = [issue for issue in preflight["issues"] if issue["severity"] == "error"]
        if runtime_errors:
            first = runtime_errors[0]
            raise ValueError(
                "Сборка остановлена runtime-lint всего мода "
                f"({first['code']}): {first['message']}"
            )
        artifact_preflight = _script_artifact_lint_target(
            mod_root,
            tools_root=args.tools_root,
            scripts=[Path(args.scr).resolve()],
        )
        artifact_errors = [issue for issue in artifact_preflight["issues"] if issue["severity"] == "error"]
        if artifact_errors:
            first = artifact_errors[0]
            raise ValueError(
                "Сборка остановлена проверкой Main/CacheData/SCR "
                f"({first['code']}): {first['message']}"
            )
        text_preflight = _game_text_lint_target(mod_root, tools_root=args.tools_root)
        text_errors = [issue for issue in text_preflight["issues"] if issue["severity"] == "error"]
        if text_errors:
            first = text_errors[0]
            raise ValueError(
                "Сборка остановлена проверкой кодировки игрового текста "
                f"({first['code']}): {first['message']}"
            )
    result = Toolchain(args.tools_root).compile_rson(
        source,
        args.scr,
        args.lang,
        overwrite=args.overwrite,
        timeout=getattr(args, "timeout", None),
    )
    if preflight is not None:
        result["runtime_preflight"] = preflight
        result["artifact_preflight"] = artifact_preflight
        result["text_preflight"] = text_preflight
    if args.json:
        print_json(result)
    else:
        print(f"SCR: {result['scr']} ({human_size(result['scr_size'])})")
        print(f"Lang: {result['lang']}")
        print(f"SHA-256 SCR: {result['scr_sha256']}")
    return 0


def cmd_script_convert(args: argparse.Namespace) -> int:
    result = Toolchain(args.tools_root).convert_script_project(
        args.source,
        args.output,
        overwrite=args.overwrite,
    )
    if args.json:
        print_json(result)
    else:
        print(f"Результат: {result['destination']}")
        print(f"SHA-256: {result['sha256']}")
    return 0


def cmd_script_decompile(args: argparse.Namespace) -> int:
    result = Toolchain(args.tools_root).decompile_scr(
        args.source,
        args.output,
        lang_dat=args.lang_dat,
        overwrite=args.overwrite,
        decompile_timeout=args.decompile_timeout,
        roundtrip_timeout=args.roundtrip_timeout,
        keep_unverified=args.keep_unverified,
        deep_roundtrip=args.deep_roundtrip,
    )
    if args.json:
        print_json(result)
    elif result["verified"]:
        print(f"RSON восстановлен: {result['destination']}")
        print(f"Объектов: {result['objects']}")
        print("Проверочный цикл SCR -> RSON -> SCR: пройден")
        print(f"SHA-256 RSON: {result['destination_sha256']}")
    else:
        print(f"RSON не опубликован: {result['error']['message']}")
        for phase in result["phases"]:
            print(f"  {phase['status'].upper():7} {phase['name']} ({phase['seconds']:.3f} с)")
        if result.get("unverified_path"):
            print(f"Непроверенная копия сохранена явно: {result['unverified_path']}")
    if result["verified"]:
        return 0
    return 1 if result.get("operational_failure") else 2


def cmd_script_compare_scr(args: argparse.Namespace) -> int:
    result = Toolchain(args.tools_root).compare_scr(
        args.left,
        args.right,
        left_lang_dat=args.left_lang_dat,
        right_lang_dat=args.right_lang_dat,
        decompile_timeout=args.decompile_timeout,
        roundtrip_timeout=args.roundtrip_timeout,
        deep_roundtrip=args.deep_roundtrip,
        max_diff_lines=args.max_diff_lines,
    )
    if args.json:
        print_json(result)
    elif not result["verified"]:
        print("Сравнение неполное: хотя бы один SCR не прошёл проверочный round-trip.")
    else:
        comparison = result["comparison"]
        print(f"Структурные метаданные совпадают: {'да' if comparison['metadata_match'] else 'нет'}")
        print(f"Изменённых блоков кода: {len(comparison['changed_blocks'])}")
        runtime = comparison["runtime_issues"]
        print(f"Runtime-замечания: +{len(runtime['added'])}, -{len(runtime['resolved'])}, ={len(runtime['unchanged'])}")
    if result["verified"]:
        return 0
    return 1 if result.get("operational_failure") else 2


def cmd_script_inspect_scr(args: argparse.Namespace) -> int:
    result = inspect_scr(args.source)
    if args.json:
        print_json(result)
    else:
        print(f"SCR: {result['name']}; версия {result['version']}; размер {human_size(result['size'])}")
        print(f"Версия поддерживается RScript 4.10f: {'да' if result['supported_version'] else 'НЕТ'}")
        print(f"Найдено UTF-16 строк: {result['utf16_strings']}")
        for signature in result["event_signatures"]:
            print(f"События: {signature}")
        for line in result["code_samples"][:10]:
            print(f"  {line}")
    return 0 if result["supported_version"] else 2


def cmd_script_audit_mod(args: argparse.Namespace) -> int:
    root = Path(args.mod).resolve()
    chain = Toolchain(args.tools_root)
    issues: list[dict[str, Any]] = []
    scripts = sorted((root / "DATA" / "Script").glob("*.scr")) if (root / "DATA" / "Script").is_dir() else []
    scr_info = []
    for path in scripts:
        try:
            info = inspect_scr(path)
            scr_info.append(info)
            if not info["supported_version"]:
                issues.append({"severity": "error", "code": "scr-version", "message": f"{path.name}: версия SCR {info['version']} не поддерживается"})
        except Exception as exc:
            issues.append({"severity": "error", "code": "scr-invalid", "message": f"{path.name}: {exc}"})

    main_dat = root / "CFG" / "Main.dat"
    registrations: dict[str, list[str]] = {}
    if scripts and not main_dat.is_file():
        issues.append({"severity": "error", "code": "main-dat-missing", "message": "Есть SCR, но отсутствует CFG/Main.dat с регистрацией Data/Script"})
    elif main_dat.is_file():
        with tempfile.TemporaryDirectory(prefix="srhd-script-audit-") as name:
            exported = Path(name) / "Main.txt"
            try:
                chain.convert_dat(main_dat, exported)
                document = load_blockpar(exported)
                try:
                    script_node = document.find_node("Data/Script")
                    for param in script_node.parameters:
                        registrations.setdefault(param.key.casefold(), []).append(param.value)
                except KeyError:
                    if scripts:
                        issues.append({"severity": "error", "code": "script-section-missing", "message": "В CFG/Main.dat отсутствует блок Data/Script"})
            except Exception as exc:
                issues.append({"severity": "error", "code": "main-dat-invalid", "message": str(exc)})
    for path in scripts:
        values = registrations.get(path.stem.casefold(), [])
        expected = f"script.{path.stem}".casefold()
        if main_dat.is_file() and not any(expected in value.casefold() for value in values):
            issues.append({"severity": "error", "code": "scr-unregistered", "message": f"{path.name} не зарегистрирован как {path.stem}=...,Script.{path.stem}"})

    rson_files = sorted(root.rglob("*.rson"))
    valid_rsons: list[str] = []
    for path in rson_files:
        try:
            project = load_rson(path)
            project_issues = project.validate()
            if project_issues:
                issues.append({"severity": "error", "code": "rson-invalid", "message": f"{path.relative_to(root)}: {project_issues[0].message}"})
            else:
                valid_rsons.append(str(path))
        except Exception as exc:
            issues.append({"severity": "error", "code": "rson-json", "message": f"{path.relative_to(root)}: {exc}"})
    if scripts and not valid_rsons:
        issues.append({"severity": "warning", "code": "rson-source-missing", "message": "Для SCR не найдено исходного RSON; восстановите его отдельной командой script decompile"})
    misplaced = root / "DATA" / "Main.dat"
    if misplaced.is_file():
        issues.append({"severity": "error", "code": "main-dat-misplaced", "message": "DATA/Main.dat расположен неверно; конфигурационный Main.dat должен находиться в CFG/Main.dat"})

    runtime_lint = _runtime_lint_target(root, tools_root=args.tools_root)
    issues.extend(runtime_lint["issues"])
    artifact_lint = _script_artifact_lint_target(
        root,
        tools_root=args.tools_root,
        scripts=scripts,
        registrations=registrations,
    )
    issues.extend(artifact_lint["issues"])
    text_lint = _game_text_lint_target(root, tools_root=args.tools_root)
    issues.extend(text_lint["issues"])

    result = {
        "mod": str(root),
        "scripts": scr_info,
        "rson_projects": valid_rsons,
        "registrations": registrations,
        "runtime_lint": {
            "rson": runtime_lint["rson"],
            "main": runtime_lint["main"],
            "module_info": runtime_lint["module_info"],
        },
        "artifact_lint": {
            "cachedata": artifact_lint["cachedata"],
        },
        "text_lint": {
            "checked": text_lint["checked"],
        },
        "issues": issues,
    }
    if args.json:
        print_json(result)
    else:
        print(f"SCR: {len(scr_info)}; корректных RSON: {len(valid_rsons)}; регистраций: {len(registrations)}")
        if not issues:
            print("Проблем в скриптовой части не найдено.")
        for issue in issues:
            suffix = f" [{issue['path']}" if issue.get("path") else ""
            if issue.get("location"):
                suffix += f": {issue['location']}"
            if suffix:
                suffix += "]"
            print(f"{issue['severity'].upper():7} {issue['code']}: {issue['message']}{suffix}")
            if issue.get("evidence"):
                print(f"         {issue['evidence']}")
    return 2 if any(item["severity"] == "error" for item in issues) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="srhd", description="Инструменты для модов Space Rangers HD")
    parser.add_argument("--version", action="version", version="SRHD ModKit 0.9.0")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Найти и описать моды")
    scan.add_argument("root")
    scan.add_argument("--max-depth", type=int)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    info = sub.add_parser("info", help="Прочитать ModuleInfo.txt")
    info.add_argument("mod")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_info)

    validate = sub.add_parser("validate", help="Проверить мод или коллекцию")
    validate.add_argument("root")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_validate)

    audit = sub.add_parser("audit", help="Универсально проверить мод или коллекцию")
    audit.add_argument("target")
    audit.add_argument("--profile", choices=("dev", "release"), default="dev")
    audit.add_argument("--allow", action="append", default=[], help="Подавить CODE или CODE:GLOB с записью в отчёт")
    audit.add_argument("--warnings-as-errors", action="store_true")
    audit.add_argument("--tools-root")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_audit)

    release = sub.add_parser("release", help="Проверить и собрать безопасный релиз")
    release_sub = release.add_subparsers(dest="release_command", required=True)

    release_check = release_sub.add_parser("check", help="Запустить полный релизный аудит")
    release_check.add_argument("mod")
    release_check.add_argument("--allow", action="append", default=[])
    release_check.add_argument("--warnings-as-errors", action="store_true")
    release_check.add_argument("--tools-root")
    release_check.add_argument("--json", action="store_true")
    release_check.set_defaults(func=cmd_release_check)

    release_build = release_sub.add_parser("build", help="Собрать и повторно проверить ZIP-релиз")
    release_build.add_argument("mod")
    release_build.add_argument("output")
    release_build.add_argument("--prefix")
    release_build.add_argument("--exclude", action="append", default=[])
    release_build.add_argument("--allow", action="append", default=[])
    release_build.add_argument("--warnings-as-errors", action="store_true")
    release_build.add_argument("--overwrite", action="store_true")
    release_build.add_argument("--tools-root")
    release_build.add_argument("--json", action="store_true")
    release_build.set_defaults(func=cmd_release_build)

    compare = sub.add_parser("compare", help="Точно сравнить две папки")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(func=cmd_compare)

    manifest = sub.add_parser("manifest", help="Построить SHA-256-манифест")
    manifest.add_argument("root")
    manifest.add_argument("--output", "-o")
    manifest.add_argument("--exclude", action="append", default=[])
    manifest.set_defaults(func=cmd_manifest)

    duplicates = sub.add_parser("duplicates", help="Найти точные дубли")
    duplicates.add_argument("root")
    duplicates.add_argument("--min-size", type=parse_size, default=1)
    duplicates.add_argument("--json", action="store_true")
    duplicates.set_defaults(func=cmd_duplicates)

    collisions = sub.add_parser("collisions", help="Найти одинаковые относительные пути в модах")
    collisions.add_argument("root")
    collisions.add_argument("--data-only", action="store_true")
    collisions.add_argument("--hash", action="store_true", help="Проверить, одинаково ли содержимое")
    collisions.add_argument("--json", action="store_true")
    collisions.set_defaults(func=cmd_collisions)

    compat = sub.add_parser("compat", help="Проверить совместимость активного набора модов")
    compat.add_argument("config", help="Путь к Mods/ModCFG.txt")
    compat.add_argument("--mods-root", required=True)
    compat.add_argument("--tools-root")
    compat.add_argument("--json", action="store_true")
    compat.set_defaults(func=cmd_compat)

    pack = sub.add_parser("pack", help="Собрать воспроизводимый ZIP-релиз")
    pack.add_argument("mod")
    pack.add_argument("output")
    pack.add_argument("--prefix")
    pack.add_argument("--exclude", action="append", default=[])
    pack.add_argument("--json", action="store_true")
    pack.set_defaults(func=cmd_pack)

    modcfg = sub.add_parser("modcfg", help="Прочитать и проверить ModCFG.txt без изменений")
    modcfg.add_argument("config")
    modcfg.add_argument("--mods-root")
    modcfg.add_argument("--json", action="store_true")
    modcfg.set_defaults(func=cmd_modcfg)

    formats = sub.add_parser("formats", help="Распознать форматы и проверить сигнатуры")
    formats.add_argument("path")
    formats.add_argument("--hash", action="store_true", help="Добавить SHA-256 для одиночного файла")
    formats.add_argument("--json", action="store_true")
    formats.set_defaults(func=cmd_formats)

    quest = sub.add_parser("quest", help="Нативная headless-работа с текстовыми квестами QM/QMM")
    quest_sub = quest.add_subparsers(dest="quest_command", required=True)

    quest_info = quest_sub.add_parser("info", help="Показать структуру и ресурсы квеста")
    quest_info.add_argument("source")
    quest_info.add_argument("--json", action="store_true")
    quest_info.set_defaults(func=cmd_quest_info)

    quest_validate = quest_sub.add_parser("validate", help="Проверить граф, формулы и параметры квеста")
    quest_validate.add_argument("source")
    quest_validate.add_argument("--json", action="store_true")
    quest_validate.set_defaults(func=cmd_quest_validate)

    quest_roundtrip = quest_sub.add_parser("roundtrip", help="Доказать семантический QM/QMM -> QMM цикл")
    quest_roundtrip.add_argument("source")
    quest_roundtrip.add_argument("--json", action="store_true")
    quest_roundtrip.set_defaults(func=cmd_quest_roundtrip)

    quest_export = quest_sub.add_parser("export-json", help="Экспортировать редактируемую модель квеста в JSON")
    quest_export.add_argument("source")
    quest_export.add_argument("output")
    quest_export.add_argument("--overwrite", action="store_true")
    quest_export.add_argument("--json", action="store_true")
    quest_export.set_defaults(func=cmd_quest_export_json)

    quest_build = quest_sub.add_parser("build", help="Собрать и повторно проверить QMM из JSON")
    quest_build.add_argument("source", help="JSON схемы srhd-modkit-quest-v1")
    quest_build.add_argument("output")
    quest_build.add_argument("--overwrite", action="store_true")
    quest_build.add_argument("--json", action="store_true")
    quest_build.set_defaults(func=cmd_quest_build)

    resource = sub.add_parser("resource", help="Headless-анализ GI, GAI, HAI и PKG")
    resource_sub = resource.add_subparsers(dest="resource_command", required=True)

    resource_info = resource_sub.add_parser("info", help="Проверить заголовок и показать сводку ресурса")
    resource_info.add_argument("source")
    resource_info.add_argument("--json", action="store_true")
    resource_info.set_defaults(func=cmd_resource_info)

    resource_list = resource_sub.add_parser("list", help="Показать слои GI, кадры GAI/HAI или файлы PKG")
    resource_list.add_argument("source")
    resource_list.add_argument("--json", action="store_true")
    resource_list.set_defaults(func=cmd_resource_list)

    resource_verify = resource_sub.add_parser("verify", help="Глубоко проверить поддерживаемый ресурс")
    resource_verify.add_argument("source")
    resource_verify.add_argument("--json", action="store_true")
    resource_verify.set_defaults(func=cmd_resource_verify)

    resource_extract = resource_sub.add_parser("extract", help="Извлечь GI-кадры GAI или содержимое PKG")
    resource_extract.add_argument("source")
    resource_extract.add_argument("output")
    resource_extract.add_argument("--overwrite", action="store_true")
    resource_extract.add_argument("--json", action="store_true")
    resource_extract.set_defaults(func=cmd_resource_extract)

    resource_build_gai = resource_sub.add_parser("build-gai", help="Собрать GAI из GI-кадров и проверить результат")
    resource_build_gai.add_argument("frames", nargs="+", help="GI-файлы или каталоги с GI")
    resource_build_gai.add_argument("--output", "-o", required=True)
    resource_build_gai.add_argument("--template", help="Исходный GAI для сохранения подтверждённых полей")
    resource_build_gai.add_argument("--width", type=int)
    resource_build_gai.add_argument("--height", type=int)
    resource_build_gai.add_argument("--overwrite", action="store_true")
    resource_build_gai.add_argument("--json", action="store_true")
    resource_build_gai.set_defaults(func=cmd_resource_build_gai)

    resource_build_pkg = resource_sub.add_parser("build-pkg", help="Собрать каталог в детерминированный PKG")
    resource_build_pkg.add_argument("source")
    resource_build_pkg.add_argument("output")
    resource_build_pkg.add_argument("--folder", dest="folders", action="append", help="Компонент пути внутри PKG; ключ повторяется")
    resource_build_pkg.add_argument("--template", help="Исходный PKG для сохранения корневого заголовка/пути")
    resource_build_pkg.add_argument("--chunk-size", type=parse_size, default=1024 * 1024)
    resource_build_pkg.add_argument("--overwrite", action="store_true")
    resource_build_pkg.add_argument("--json", action="store_true")
    resource_build_pkg.set_defaults(func=cmd_resource_build_pkg)

    tools = sub.add_parser("tools", help="Проверить доступность внешних кодеков и редакторов")
    tools.add_argument("--tools-root")
    tools.add_argument("--json", action="store_true")
    tools.set_defaults(func=cmd_tools)

    doctor = sub.add_parser("doctor", help="Диагностировать служебные процессы ModKit")
    doctor_sub = doctor.add_subparsers(dest="doctor_command", required=True)
    doctor_processes = doctor_sub.add_parser("processes", help="Найти скрытые редакторы на desktop SRHDModKit_*")
    doctor_processes.add_argument(
        "--terminate",
        action="store_true",
        help="Завершить только известные RScript/BlockParEditor/ResEditor на служебных desktop",
    )
    doctor_processes.add_argument("--json", action="store_true")
    doctor_processes.set_defaults(func=cmd_doctor_processes)

    convert = sub.add_parser("convert", help="Безопасно преобразовать GI <-> PNG")
    convert.add_argument("direction", choices=("gi-png", "png-gi"))
    convert.add_argument("inputs", nargs="+")
    convert.add_argument("--output", "-o", required=True)
    convert.add_argument("--mode", choices=("0_32", "0_16", "2"), default="0_32", help="Режим создаваемого GI")
    convert.add_argument("--overwrite", action="store_true")
    convert.add_argument("--tools-root")
    convert.add_argument("--json", action="store_true")
    convert.set_defaults(func=cmd_convert)

    editor = sub.add_parser("open", help="Открыть бинарный ресурс штатным редактором")
    editor.add_argument("file")
    editor.add_argument("--allow-gui", action="store_true", help="Ручное подтверждение; также требуется SRHD_MODKIT_ALLOW_GUI=1")
    editor.add_argument("--tools-root")
    editor.add_argument("--json", action="store_true")
    editor.set_defaults(func=cmd_open_editor)

    stage = sub.add_parser("stage", help="Создать проверенную побайтовую копию всего дерева мода")
    stage.add_argument("source")
    stage.add_argument("destination")
    stage.add_argument("--json", action="store_true")
    stage.set_defaults(func=cmd_stage)

    dat = sub.add_parser("dat", help="Headless-работа с деревом BlockPar DAT")
    dat_sub = dat.add_subparsers(dest="dat_command", required=True)

    for name, help_text in (("decode", "Расшифровать DAT в TXT"), ("encode", "Собрать DAT из TXT")):
        item = dat_sub.add_parser(name, help=help_text)
        item.add_argument("source")
        item.add_argument("output")
        item.add_argument("--overwrite", action="store_true")
        item.add_argument("--no-verify", action="store_true")
        item.add_argument("--tools-root")
        item.add_argument("--json", action="store_true")
        item.set_defaults(func=cmd_dat_convert)

    dat_tree = dat_sub.add_parser("tree", help="Показать дерево узлов DAT/TXT")
    dat_tree.add_argument("source")
    dat_tree.add_argument("--include-raw", action="store_true")
    dat_tree.add_argument("--tools-root")
    dat_tree.add_argument("--json", action="store_true")
    dat_tree.set_defaults(func=cmd_dat_tree)

    dat_get = dat_sub.add_parser("get", help="Прочитать узел или параметр без GUI")
    dat_get.add_argument("source")
    dat_get.add_argument("node", help="Путь вида Data/SE/Ship; повторы: Name[2]")
    dat_get.add_argument("--key")
    dat_get.add_argument("--case-sensitive", action="store_true")
    dat_get.add_argument("--include-raw", action="store_true")
    dat_get.add_argument("--tools-root")
    dat_get.add_argument("--json", action="store_true")
    dat_get.set_defaults(func=cmd_dat_get)

    dat_set = dat_sub.add_parser("set", help="Изменить параметр и проверить пересобранный DAT")
    dat_set.add_argument("source")
    dat_set.add_argument("output")
    dat_set.add_argument("--node", required=True)
    dat_set.add_argument("--key", required=True)
    dat_set.add_argument("--value", required=True)
    dat_set.add_argument("--create", action="store_true")
    dat_set.add_argument("--all", action="store_true", help="Изменить все одноимённые параметры узла")
    dat_set.add_argument("--overwrite", action="store_true")
    dat_set.add_argument("--tools-root")
    dat_set.add_argument("--json", action="store_true")
    dat_set.set_defaults(func=cmd_dat_set)

    dat_patch = dat_sub.add_parser("patch", help="Применить набор JSON-изменений без GUI")
    dat_patch.add_argument("source")
    dat_patch.add_argument("output")
    dat_patch.add_argument("patch", help="UTF-8 JSON-файл с массивом operations")
    dat_patch.add_argument("--overwrite", action="store_true")
    dat_patch.add_argument("--tools-root")
    dat_patch.add_argument("--json", action="store_true")
    dat_patch.set_defaults(func=cmd_dat_patch)

    dat_validate = dat_sub.add_parser("validate", help="Расшифровать и проверить структуру DAT/TXT")
    dat_validate.add_argument("source")
    dat_validate.add_argument("--tools-root")
    dat_validate.add_argument("--json", action="store_true")
    dat_validate.set_defaults(func=cmd_dat_validate)

    script = sub.add_parser("script", help="Headless-анализ, декомпиляция и сборка RSON/SCR")
    script_sub = script.add_subparsers(dest="script_command", required=True)

    script_info = script_sub.add_parser("info", help="Показать устройство RSON-проекта")
    script_info.add_argument("source")
    script_info.add_argument("--json", action="store_true")
    script_info.set_defaults(func=cmd_script_info)

    script_validate = script_sub.add_parser("validate", help="Проверить объекты, ссылки и код RSON")
    script_validate.add_argument("source")
    script_validate.add_argument("--json", action="store_true")
    script_validate.set_defaults(func=cmd_script_validate)

    script_search = script_sub.add_parser("search", help="Искать текст во всех блоках кода")
    script_search.add_argument("source")
    script_search.add_argument("query")
    script_search.add_argument("--case-sensitive", action="store_true")
    script_search.add_argument("--json", action="store_true")
    script_search.set_defaults(func=cmd_script_search)

    script_get = script_sub.add_parser("get", help="Получить объект скрипта по номеру #")
    script_get.add_argument("source")
    script_get.add_argument("id", type=int)
    script_get.add_argument("--json", action="store_true")
    script_get.set_defaults(func=cmd_script_get)

    script_links = script_sub.add_parser("list-links", help="Показать связи и их индексы")
    script_links.add_argument("source")
    script_links.add_argument("--json", action="store_true")
    script_links.set_defaults(func=cmd_script_list_links)

    script_code = script_sub.add_parser("set-code", help="Заменить Code/ActCode/LinkCode/OnActCode без GUI")
    script_code.add_argument("source")
    script_code.add_argument("output")
    script_code.add_argument("--id", required=True, type=int)
    script_code.add_argument("--field", choices=("Code", "ActCode", "LinkCode", "OnActCode"), default="Code")
    script_code.add_argument("--code-file", required=True)
    script_code.add_argument("--encoding", default="utf-8")
    script_code.add_argument("--overwrite", action="store_true")
    script_code.add_argument("--json", action="store_true")
    script_code.set_defaults(func=cmd_script_set_code)

    script_field = script_sub.add_parser("set-field", help="Изменить JSON-поле объекта RSON без GUI")
    script_field.add_argument("source")
    script_field.add_argument("output")
    script_field.add_argument("--id", required=True, type=int)
    script_field.add_argument("--field", required=True)
    script_field.add_argument("--value", required=True, help="JSON-значение: 10, true, null или '\"текст\"'")
    script_field.add_argument("--overwrite", action="store_true")
    script_field.add_argument("--json", action="store_true")
    script_field.set_defaults(func=cmd_script_set_field)

    script_events = script_sub.add_parser(
        "set-events",
        help="Задать события TState в OnActCode без GUI, сохранив код обработчика",
    )
    script_events.add_argument("source")
    script_events.add_argument("output")
    script_events.add_argument("--id", required=True, type=int)
    event_mode = script_events.add_mutually_exclusive_group(required=True)
    event_mode.add_argument("--event", dest="events", action="append", help="Имя t_On...; ключ можно повторять")
    event_mode.add_argument("--clear", action="store_true", help="Удалить сигнатуру событий, сохранив обработчик")
    script_events.add_argument("--overwrite", action="store_true")
    script_events.add_argument("--json", action="store_true")
    script_events.set_defaults(func=cmd_script_set_events, events=[])

    script_clone = script_sub.add_parser("clone-object", help="Клонировать реальный объект в той же группе RSON")
    script_clone.add_argument("source")
    script_clone.add_argument("output")
    script_clone.add_argument("--id", required=True, type=int)
    script_clone.add_argument("--name", help="Новое имя; иначе к исходному добавляется Copy")
    script_clone.add_argument("--overwrite", action="store_true")
    script_clone.add_argument("--json", action="store_true")
    script_clone.set_defaults(func=cmd_script_clone_object)

    script_add_link = script_sub.add_parser("add-link", help="Добавить проверенную связь TGraphLink")
    script_add_link.add_argument("source")
    script_add_link.add_argument("output")
    script_add_link.add_argument("--begin", required=True, type=int)
    script_add_link.add_argument("--end", required=True, type=int)
    script_add_link.add_argument("--nom", type=int, default=0)
    script_add_link.add_argument("--no-arrow", action="store_true")
    script_add_link.add_argument("--overwrite", action="store_true")
    script_add_link.add_argument("--json", action="store_true")
    script_add_link.set_defaults(func=cmd_script_add_link)

    script_delete_link = script_sub.add_parser("delete-link", help="Удалить связь по индексу из script info/list")
    script_delete_link.add_argument("source")
    script_delete_link.add_argument("output")
    script_delete_link.add_argument("--index", required=True, type=int)
    script_delete_link.add_argument("--overwrite", action="store_true")
    script_delete_link.add_argument("--json", action="store_true")
    script_delete_link.set_defaults(func=cmd_script_delete_link)

    script_delete_object = script_sub.add_parser("delete-object", help="Удалить объект с защитой ссылок графа")
    script_delete_object.add_argument("source")
    script_delete_object.add_argument("output")
    script_delete_object.add_argument("--id", required=True, type=int)
    script_delete_object.add_argument(
        "--detach-references",
        action="store_true",
        help="Удалить его связи и поставить Parent=-1 дочерним объектам",
    )
    script_delete_object.add_argument("--overwrite", action="store_true")
    script_delete_object.add_argument("--json", action="store_true")
    script_delete_object.set_defaults(func=cmd_script_delete_object)

    script_runtime = script_sub.add_parser(
        "lint-runtime",
        help="Найти опасный контекст ScriptRun, ранний доступ к миру, рекурсию и бесконечные циклы",
    )
    script_runtime.add_argument("target", help="Каталог мода или отдельный RSON")
    script_runtime.add_argument("--main", help="Main.dat/Main.txt для отдельного RSON")
    script_runtime.add_argument("--module-info", help="ModuleInfo.txt для отдельного RSON")
    script_runtime.add_argument("--strict", action="store_true", help="Считать предупреждения ошибкой запуска")
    script_runtime.add_argument("--tools-root")
    script_runtime.add_argument("--json", action="store_true")
    script_runtime.set_defaults(func=cmd_script_lint_runtime)

    script_register = script_sub.add_parser("register", help="Зарегистрировать SCR в CFG/Main.dat без GUI")
    script_register.add_argument("main_dat")
    script_register.add_argument("output")
    script_register.add_argument("--name", required=True, help="Имя без .scr, например Mod_MyScript")
    script_register.add_argument("--flag", choices=("0", "1"), default="1")
    script_register.add_argument("--startup-key", help="Ключ в BV/OnStart/0DayScripts")
    script_register.add_argument("--startup-code", help="Полный ScriptRun(...); библиотека его не угадывает")
    script_register.add_argument("--overwrite", action="store_true")
    script_register.add_argument("--tools-root")
    script_register.add_argument("--json", action="store_true")
    script_register.set_defaults(func=cmd_script_register)

    script_build = script_sub.add_parser("build", help="Скомпилировать RSON в SCR через CLI RScript")
    script_build.add_argument("source")
    script_build.add_argument("--scr", required=True)
    script_build.add_argument("--lang", required=True)
    script_build.add_argument("--overwrite", action="store_true")
    script_build.add_argument(
        "--timeout",
        type=float,
        help="Лимит RScript в секундах; по умолчанию адаптивный, 0 отключает общий лимит",
    )
    script_build.add_argument("--tools-root")
    script_build.add_argument("--json", action="store_true")
    script_build.set_defaults(func=cmd_script_build)

    script_convert = script_sub.add_parser("convert", help="Преобразовать проект RSON <-> SVR без GUI")
    script_convert.add_argument("source")
    script_convert.add_argument("output")
    script_convert.add_argument("--overwrite", action="store_true")
    script_convert.add_argument("--tools-root")
    script_convert.add_argument("--json", action="store_true")
    script_convert.set_defaults(func=cmd_script_convert)

    script_decompile = script_sub.add_parser(
        "decompile",
        help="Восстановить проверенный RSON из SCR без видимого GUI",
    )
    script_decompile.add_argument("source")
    script_decompile.add_argument("output")
    script_decompile.add_argument("--lang-dat", help="Необязательный Lang.dat для восстановления диалогов")
    script_decompile.add_argument(
        "--decompile-timeout",
        type=float,
        help="Лимит восстановления в секундах; по умолчанию адаптивный, 0 отключает общий лимит",
    )
    script_decompile.add_argument(
        "--roundtrip-timeout",
        type=float,
        help="Лимит контрольной сборки в секундах; по умолчанию адаптивный, 0 отключает общий лимит",
    )
    script_decompile.add_argument(
        "--keep-unverified",
        help="Явный отдельный .rson для сохранения результата, не прошедшего round-trip",
    )
    script_decompile.add_argument(
        "--deep-roundtrip",
        action="store_true",
        help="Дополнительно проверить SCR -> RSON -> SCR -> RSON (медленнее)",
    )
    script_decompile.add_argument("--overwrite", action="store_true")
    script_decompile.add_argument("--tools-root")
    script_decompile.add_argument("--json", action="store_true")
    script_decompile.set_defaults(func=cmd_script_decompile)

    script_compare = script_sub.add_parser(
        "compare-scr",
        help="Восстановить и сравнить два SCR без сохранения файлов мода",
    )
    script_compare.add_argument("left")
    script_compare.add_argument("right")
    script_compare.add_argument("--left-lang-dat")
    script_compare.add_argument("--right-lang-dat")
    script_compare.add_argument("--decompile-timeout", type=float)
    script_compare.add_argument("--roundtrip-timeout", type=float)
    script_compare.add_argument("--deep-roundtrip", action="store_true")
    script_compare.add_argument("--max-diff-lines", type=int, default=200)
    script_compare.add_argument("--tools-root")
    script_compare.add_argument("--json", action="store_true")
    script_compare.set_defaults(func=cmd_script_compare_scr)

    script_scr = script_sub.add_parser("inspect-scr", help="Проверить заголовок и строки скомпилированного SCR")
    script_scr.add_argument("source")
    script_scr.add_argument("--json", action="store_true")
    script_scr.set_defaults(func=cmd_script_inspect_scr)

    script_audit = script_sub.add_parser("audit-mod", help="Проверить SCR, RSON и регистрацию в Main.dat")
    script_audit.add_argument("mod")
    script_audit.add_argument("--tools-root")
    script_audit.add_argument("--json", action="store_true")
    script_audit.set_defaults(func=cmd_script_audit_mod)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Операция прервана.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
