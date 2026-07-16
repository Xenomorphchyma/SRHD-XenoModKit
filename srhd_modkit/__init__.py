"""Programmatic tools for inspecting and packaging Space Rangers HD mods."""

from .discovery import discover_mods, load_mod
from .files import (
    build_manifest,
    compare_trees,
    find_collisions,
    find_duplicates,
    pack_mod,
    sha256_file,
    stage_tree,
)
from .formats import format_catalog, get_format_spec, inspect_file, scan_formats
from .modcfg import parse_modcfg, validate_modcfg
from .module_info import parse_module_info
from .validation import validate_collection, validate_mod
from .toolchain import ConversionItem, Toolchain, is_empty_rscript_lang_dat
from .blockpar import BlockParDocument, BlockParNode, BlockParParameter, load_blockpar, parse_blockpar
from .scripts import RsonProject, ScriptIssue, inspect_scr, load_rson
from .resources import (
    GaiInfo,
    HaiInfo,
    PkgInfo,
    ResourceFormatError,
    extract_resource,
    inspect_gai,
    inspect_hai,
    inspect_pkg,
    inspect_resource,
    verify_resource,
)
from .runtime_lint import (
    RuntimeIssue,
    lint_main_runtime,
    lint_module_runtime,
    lint_rson_runtime,
)
from .script_artifacts import ScriptArtifactIssue, lint_script_cache
from .game_text import GameTextIssue, lint_game_text
from .audit import (
    AuditCheck,
    AuditIssue,
    AuditProfile,
    AuditRegistry,
    AuditReport,
    audit_collection,
    audit_mod,
)
from .release import (
    ReleaseBlockedError,
    ReleaseResult,
    build_release,
    verify_release_archive,
)

__all__ = [
    "build_manifest",
    "compare_trees",
    "discover_mods",
    "find_collisions",
    "find_duplicates",
    "format_catalog",
    "get_format_spec",
    "inspect_file",
    "load_mod",
    "pack_mod",
    "parse_modcfg",
    "parse_module_info",
    "sha256_file",
    "scan_formats",
    "stage_tree",
    "Toolchain",
    "ConversionItem",
    "is_empty_rscript_lang_dat",
    "BlockParDocument",
    "BlockParNode",
    "BlockParParameter",
    "load_blockpar",
    "parse_blockpar",
    "RsonProject",
    "ScriptIssue",
    "load_rson",
    "inspect_scr",
    "GaiInfo",
    "HaiInfo",
    "PkgInfo",
    "ResourceFormatError",
    "extract_resource",
    "inspect_gai",
    "inspect_hai",
    "inspect_pkg",
    "inspect_resource",
    "verify_resource",
    "RuntimeIssue",
    "lint_main_runtime",
    "lint_module_runtime",
    "lint_rson_runtime",
    "ScriptArtifactIssue",
    "lint_script_cache",
    "GameTextIssue",
    "lint_game_text",
    "AuditCheck",
    "AuditIssue",
    "AuditProfile",
    "AuditRegistry",
    "AuditReport",
    "audit_collection",
    "audit_mod",
    "ReleaseBlockedError",
    "ReleaseResult",
    "build_release",
    "verify_release_archive",
    "validate_collection",
    "validate_mod",
    "validate_modcfg",
]

__version__ = "0.5.7"
