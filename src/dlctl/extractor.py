"""
extractor.py — AST-based analysis engine for dlctl bundle.

Responsibilities:
  - Locate a module's source file via importlib.
  - Parse the module with Python's `ast` module.
  - Build a table of all top-level definitions (functions, classes, assignments, imports).
  - For a target function, walk its body to find every Name/Attribute it references.
  - Recursively resolve those references to collect the transitive closure of everything
    the function needs from its own module.
  - Classify imports as: standard-library, third-party/external, or local-sibling
    (another sub-module of the same top-level package).
  - Recursively expand sibling-module dependencies: a copied sibling module (e.g.
    models.py) may itself import further sibling modules (e.g. constants.py) or
    external packages, and those must be discovered too, not just the target
    function's own direct imports.
  - Return a structured ExtractionResult per selected function.
  - Detect and return cross-bundle warnings (name collisions, cross-package coupling).

No file I/O is performed here — this is pure analysis (aside from reading source
files via importlib.util.find_spec, which is necessary to parse them).
"""

import ast
import importlib
import importlib.util
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """Everything the builder needs for one selected function."""

    # "package_a.module_name:function_a"
    select_spec: str

    # Top-level package name (e.g. "package_a")
    source_package: str

    # Full dotted module name (e.g. "package_a.module_name")
    source_module: str

    # The function name requested (e.g. "function_a")
    function_name: str

    # Ordered list of AST nodes to emit into the extracted file.
    # Order: needed imports → needed helper defs → the target function itself.
    nodes_to_emit: list  # list[ast.AST]

    # Set of dotted module names (e.g. {"package_a.utils"}) that must be
    # copied wholesale into the bundle because the function depends on them,
    # directly or transitively (i.e. this is the FULL closure, including
    # siblings-of-siblings).
    local_sibling_modules: set  # set[str]

    # Top-level distribution names required at runtime (e.g. {"requests", "numpy"}).
    # These become install_requires in the generated pyproject.toml. Includes
    # deps pulled in transitively through sibling modules.
    external_deps: set  # set[str]

    # Path to the source file the module was loaded from.
    source_file: Path


@dataclass
class BundleWarning:
    """A non-fatal issue detected during extraction."""
    kind: str        # "name_collision" | "cross_package_coupling"
    message: str


# ---------------------------------------------------------------------------
# Standard-library detection helpers
# ---------------------------------------------------------------------------

def _stdlib_module_names() -> frozenset:
    """Return the set of top-level standard-library module names.

    Uses sys.stdlib_module_names (Python 3.10+) when available, otherwise
    falls back to sysconfig to locate the stdlib directories and scans them.
    """
    # Python 3.10+ exposes this directly
    if hasattr(sys, "stdlib_module_names"):
        return frozenset(sys.stdlib_module_names)

    # Fallback: collect names from the stdlib paths reported by sysconfig
    stdlib_paths = {
        sysconfig.get_path("stdlib"),
        sysconfig.get_path("platstdlib"),
    }
    names = set()
    for p in stdlib_paths:
        if p is None:
            continue
        root = Path(p)
        if not root.exists():
            continue
        for child in root.iterdir():
            # Accept .py files and packages (dirs with __init__.py)
            if child.suffix == ".py":
                names.add(child.stem)
            elif child.is_dir() and (child / "__init__.py").exists():
                names.add(child.name)
    # Always include builtins
    names.update(sys.builtin_module_names)
    return frozenset(names)


_STDLIB_NAMES: frozenset = _stdlib_module_names()


def _is_stdlib(module_name: str) -> bool:
    """True if the top-level part of module_name belongs to the standard library."""
    top = module_name.split(".")[0]
    return top in _STDLIB_NAMES


def _is_local_sibling(module_name: str, source_package: str) -> bool:
    """True if module_name is a sub-module of source_package (a local sibling)."""
    top = module_name.split(".")[0]
    return top == source_package


# ---------------------------------------------------------------------------
# Module-level definition table
# ---------------------------------------------------------------------------

def _build_definition_table(module_ast: ast.Module) -> dict:
    """
    Scan the top-level statements of a module and return a dict mapping
    every defined name to the AST node(s) that define it.

    Keys are strings (the name as it appears in code).
    Values are lists of AST nodes (a name can be defined multiple times, e.g.
    via several import aliases or a function + a re-assignment).

    Node types captured:
      - ast.FunctionDef / ast.AsyncFunctionDef  → function definitions
      - ast.ClassDef                             → class definitions
      - ast.Assign / ast.AnnAssign               → module-level variable assignments
      - ast.Import / ast.ImportFrom              → import statements
    """
    table: dict[str, list[ast.AST]] = {}

    def _add(name: str, node: ast.AST):
        table.setdefault(name, []).append(node)

    for node in module_ast.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _add(node.name, node)

        elif isinstance(node, ast.Assign):
            # e.g.  X = 42   or   X = Y = 42
            for target in node.targets:
                if isinstance(target, ast.Name):
                    _add(target.id, node)
                elif isinstance(target, ast.Tuple):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            _add(elt.id, node)

        elif isinstance(node, ast.AnnAssign):
            # e.g.  X: int = 42
            if isinstance(node.target, ast.Name):
                _add(node.target.id, node)

        elif isinstance(node, ast.Import):
            # e.g.  import os, sys as system
            for alias in node.names:
                bound_name = alias.asname if alias.asname else alias.name.split(".")[0]
                _add(bound_name, node)

        elif isinstance(node, ast.ImportFrom):
            # e.g.  from os.path import join, exists as file_exists
            for alias in node.names:
                if alias.name == "*":
                    # Star imports: we can't know what they bring in; keep the node
                    # but don't register specific names. Mark with a sentinel.
                    _add("*", node)
                else:
                    bound_name = alias.asname if alias.asname else alias.name
                    _add(bound_name, node)

    return table


# ---------------------------------------------------------------------------
# Reference collection
# ---------------------------------------------------------------------------

def _collect_references(node: ast.AST) -> set:
    """
    Walk an AST node and collect every Name and Attribute reference used.

    Returns a set of strings:
      - Simple names: "foo"
      - Attribute access on a Name: "foo.bar"  (only first two levels captured)

    We only care about names that could resolve to module-level definitions,
    so we skip names that are function parameters or local assignments.
    """
    # Gather names that are locally bound inside this node so we don't
    # chase them up to the module level.
    local_names: set[str] = set()

    # Walk arguments of function definitions nested inside the node
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in child.args.args + child.args.posonlyargs + child.args.kwonlyargs:
                local_names.add(arg.arg)
            if child.args.vararg:
                local_names.add(child.args.vararg.arg)
            if child.args.kwarg:
                local_names.add(child.args.kwarg.arg)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            local_names.add(child.id)

    refs: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if child.id not in local_names:
                refs.add(child.id)
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            # Capture "module.something" patterns
            if isinstance(child.value, ast.Name):
                refs.add(child.value.id)
                refs.add(f"{child.value.id}.{child.attr}")

    return refs


# ---------------------------------------------------------------------------
# Import classification helpers
# ---------------------------------------------------------------------------

def _import_module_names_from_node(node: ast.AST) -> list:
    """
    Return the list of (bound_name, dotted_module_name) tuples that an
    import node introduces.

    For  `import os.path`               → [("os", "os")]
    For  `import os.path as osp`        → [("osp", "os.path")]
    For  `from os.path import join`     → [("join", "os.path")]
    For  `from . import utils`          → [("__relative__", "")]  (bare package-relative
                                             import with no module name; dotted_module_name
                                             is empty here since node.module is None)
    For  `from .models import Bug`      → [("__relative__", "models")]  (relative,
                                             dotted_module is the bare sub-module name
                                             and MUST be re-qualified with the source
                                             package before classification — callers
                                             check bound_name == "__relative__" for this,
                                             they do NOT rely on dotted_module alone.)
    """
    result = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            bound = alias.asname if alias.asname else alias.name.split(".")[0]
            # The actual module that gets loaded is alias.name
            result.append((bound, alias.name))
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level and node.level > 0:
            # Relative import — treat as local sibling (we handle via source_package)
            result.append(("__relative__", module))
        else:
            for alias in node.names:
                if alias.name == "*":
                    result.append(("*", module))
                else:
                    result.append((alias.asname or alias.name, module))
    return result


def _classify_import(dotted_module: str, source_package: str) -> str:
    """
    Classify an already-qualified module reference as "stdlib", "local", or
    "external" (e.g. "bug_lifecycle_engine.models", "sqlalchemy.orm").

    This does NOT handle the "__relative__" sentinel — that sentinel lives in
    the bound_name slot returned by _import_module_names_from_node, not in
    dotted_module, and callers must check bound_name themselves and re-qualify
    the module name with source_package BEFORE calling this function.
    """
    if not dotted_module:
        return "local"
    if _is_stdlib(dotted_module):
        return "stdlib"
    if _is_local_sibling(dotted_module, source_package):
        return "local"
    return "external"


def _resolve_relative_module(dotted_module: str, source_package: str) -> str:
    """
    Re-qualify a bare relative-import module name (as returned alongside the
    "__relative__" sentinel) with the source package, so it can be treated
    like any other fully-dotted local module name.

    e.g. _resolve_relative_module("models", "bug_lifecycle_engine")
         -> "bug_lifecycle_engine.models"

    If dotted_module is empty (a bare `from . import x` with no module name),
    resolves to the package itself.
    """
    return f"{source_package}.{dotted_module}" if dotted_module else source_package


# ---------------------------------------------------------------------------
# Transitive closure resolver (within a single module's own definitions)
# ---------------------------------------------------------------------------

def _resolve_transitive(
    target_names: set,
    definition_table: dict,
    source_package: str,
    already_resolved: set,
) -> tuple:
    """
    Given a set of referenced names, resolve them against the module's
    definition table, recursively pulling in everything they need.

    Returns:
        needed_nodes   — ordered list of ast.AST nodes to emit
        sibling_modules — set of fully-qualified dotted module names to copy
                           wholesale (direct dependencies only; the caller is
                           responsible for expanding this into the full
                           transitive closure via _expand_sibling_closure)
        external_deps   — set of top-level external package names

    `already_resolved` is mutated in place to track what we've visited
    (prevents infinite loops for mutually recursive definitions).
    """
    needed_nodes: list[ast.AST] = []
    sibling_modules: set[str] = set()
    external_deps: set[str] = set()

    # We use a list as a queue to process names breadth-first
    queue = list(target_names - already_resolved)

    while queue:
        name = queue.pop(0)
        if name in already_resolved:
            continue
        already_resolved.add(name)

        if name not in definition_table:
            # Not defined at module level — likely a builtin or parameter; skip
            continue

        for node in definition_table[name]:
            if node in needed_nodes:
                continue  # already scheduled

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                  ast.Assign, ast.AnnAssign)):
                # It's a definition — include it and recurse into its references
                needed_nodes.append(node)
                child_refs = _collect_references(node)
                new_refs = child_refs - already_resolved
                queue.extend(new_refs)

            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                # It's an import — classify and decide what to do
                needed_nodes.append(node)
                for bound_name, dotted_module in _import_module_names_from_node(node):
                    # --- FIX (relative-import classification) ---
                    # The "__relative__" sentinel lives in bound_name, not
                    # dotted_module. Check it here and re-qualify with
                    # source_package before scheduling as a sibling module,
                    # instead of letting a bare sub-module name like "models"
                    # fall through _classify_import and get misclassified as
                    # "external".
                    if bound_name == "__relative__":
                        full_module = _resolve_relative_module(dotted_module, source_package)
                        sibling_modules.add(full_module)
                        continue

                    kind = _classify_import(dotted_module, source_package)
                    if kind == "local" and dotted_module:
                        # Local sibling module — schedule for wholesale copy
                        sibling_modules.add(dotted_module)
                    elif kind == "external":
                        # External dep — record the top-level distribution name
                        top = dotted_module.split(".")[0]
                        external_deps.add(top)
                    # stdlib: nothing extra to do

    return needed_nodes, sibling_modules, external_deps


# ---------------------------------------------------------------------------
# Sibling-of-sibling closure expansion
# ---------------------------------------------------------------------------

def _load_module_ast(dotted_module: str) -> Optional[ast.Module]:
    """
    Locate and parse a module's source file via importlib, returning its AST
    (or None if it can't be found/parsed — best-effort, since a sibling that
    fails to resolve here will also fail at copy time in the builder, which
    reports that failure explicitly).
    """
    try:
        spec = importlib.util.find_spec(dotted_module)
    except Exception:
        return None

    if spec is None or spec.origin is None:
        return None

    source_file = Path(spec.origin)
    if not source_file.exists():
        return None

    try:
        return ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    except Exception:
        return None


def _expand_sibling_closure(initial_siblings: set, source_package: str) -> tuple:
    """
    Given an initial set of directly-referenced sibling modules, recursively
    discover every module THEY import in turn (siblings-of-siblings, and so
    on), since a wholesale-copied sibling module is not pruned and therefore
    needs its own imports satisfied too.

    This walks ALL top-level import statements of each sibling module (not
    just ones referenced by a specific function), because the module is
    copied wholesale/verbatim — every name it defines, and everything those
    definitions need, ships as-is.

    Returns:
        all_siblings   — the full transitive closure of local sibling modules
                         (superset of initial_siblings)
        external_deps  — external (third-party) top-level package names
                         discovered while walking sibling modules
    """
    visited: set[str] = set(initial_siblings)
    queue: list[str] = list(initial_siblings)
    external_deps: set[str] = set()

    while queue:
        dotted_module = queue.pop(0)
        module_ast = _load_module_ast(dotted_module)
        if module_ast is None:
            # Can't resolve/parse — the builder's copy step will surface this
            # as a "FAILED (module not found)" status; nothing more to expand.
            continue

        for node in module_ast.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    dotted = alias.name
                    kind = _classify_import(dotted, source_package)
                    if kind == "local":
                        if dotted not in visited:
                            visited.add(dotted)
                            queue.append(dotted)
                    elif kind == "external":
                        external_deps.add(dotted.split(".")[0])
                    # stdlib: nothing to do

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level and node.level > 0:
                    # Relative import inside this sibling — resolve against
                    # the SAME source_package (siblings live in the same
                    # top-level package as the original target function).
                    resolved = _resolve_relative_module(module, source_package)
                    if resolved not in visited:
                        visited.add(resolved)
                        queue.append(resolved)
                else:
                    kind = _classify_import(module, source_package)
                    if kind == "local":
                        if module not in visited:
                            visited.add(module)
                            queue.append(module)
                    elif kind == "external":
                        external_deps.add(module.split(".")[0])
                    # stdlib: nothing to do

    return visited, external_deps


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract(select_spec: str) -> ExtractionResult:
    """
    Analyse a single "module:function" spec and return an ExtractionResult.

    Parameters
    ----------
    select_spec : str
        A string like "package_a.module_name:function_a".

    Returns
    -------
    ExtractionResult
        Contains all AST nodes to emit, the FULL transitive closure of
        sibling modules to copy (including siblings-of-siblings), and all
        external deps needed (including ones discovered only via sibling
        modules' own imports).

    Raises
    ------
    ValueError
        If the spec is malformed, the module can't be found/imported, or
        the function doesn't exist in that module.
    """
    # --- Parse the spec ---
    if ":" not in select_spec:
        raise ValueError(
            f"Invalid --select spec {select_spec!r}: expected format is "
            f"'module.path:function_name'  (colon separates module from function)."
        )
    module_path, function_name = select_spec.rsplit(":", 1)
    module_path = module_path.strip()
    function_name = function_name.strip()

    if not module_path or not function_name:
        raise ValueError(
            f"Invalid --select spec {select_spec!r}: both the module path and "
            f"function name must be non-empty."
        )

    # --- Locate the source file ---
    try:
        spec = importlib.util.find_spec(module_path)
    except ModuleNotFoundError as exc:
        raise ValueError(
            f"Cannot locate module {module_path!r} — is it installed? "
            f"(importlib error: {exc})"
        ) from exc

    if spec is None:
        raise ValueError(
            f"Module {module_path!r} not found. Make sure the package containing "
            f"it is installed (e.g. `pip install -e ./package_a`)."
        )

    if spec.origin is None or spec.origin == "built-in":
        raise ValueError(
            f"Module {module_path!r} has no accessible source file "
            f"(it may be a built-in or compiled extension)."
        )

    source_file = Path(spec.origin)
    if not source_file.exists():
        raise ValueError(
            f"Source file for module {module_path!r} does not exist at "
            f"{source_file}. The package installation may be broken."
        )

    # --- Parse the source file ---
    source_text = source_file.read_text(encoding="utf-8")
    try:
        module_ast = ast.parse(source_text, filename=str(source_file))
    except SyntaxError as exc:
        raise ValueError(
            f"Failed to parse {source_file}: {exc}"
        ) from exc

    # --- Build the definition table ---
    definition_table = _build_definition_table(module_ast)

    # --- Locate the target function ---
    if function_name not in definition_table:
        available = [k for k in definition_table if not k.startswith("_") or k == function_name]
        raise ValueError(
            f"Function {function_name!r} not found in module {module_path!r}. "
            f"Top-level names in that module: {sorted(available)}"
        )

    target_nodes = definition_table[function_name]
    # Expect exactly one function/class definition matching; take the last one
    # (handles forward re-definitions).
    target_node = None
    for n in reversed(target_nodes):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            target_node = n
            break

    if target_node is None:
        raise ValueError(
            f"{function_name!r} in {module_path!r} exists in the definition table "
            f"but is not a function or class definition (it may be a variable). "
            f"Only functions and classes can be selected."
        )

    # --- Determine the source package (top-level distribution package) ---
    source_package = module_path.split(".")[0]

    # --- Collect references inside the target function and resolve transitively ---
    direct_refs = _collect_references(target_node)
    already_resolved: set[str] = {function_name}  # don't pull the function into its own deps

    needed_nodes, sibling_modules, external_deps = _resolve_transitive(
        direct_refs, definition_table, source_package, already_resolved
    )

    # --- Expand siblings-of-siblings: a copied sibling module is not pruned, ---
    # --- so everything IT imports must also be discovered and copied/declared. ---
    if sibling_modules:
        expanded_siblings, extra_external_deps = _expand_sibling_closure(
            sibling_modules, source_package
        )
        sibling_modules = expanded_siblings
        external_deps = external_deps | extra_external_deps

    # --- Order the nodes: imports first, then defs, then the target function ---
    import_nodes = [n for n in needed_nodes if isinstance(n, (ast.Import, ast.ImportFrom))]
    def_nodes = [n for n in needed_nodes if not isinstance(n, (ast.Import, ast.ImportFrom))]
    ordered_nodes = import_nodes + def_nodes + [target_node]

    return ExtractionResult(
        select_spec=select_spec,
        source_package=source_package,
        source_module=module_path,
        function_name=function_name,
        nodes_to_emit=ordered_nodes,
        local_sibling_modules=sibling_modules,
        external_deps=external_deps,
        source_file=source_file,
    )


# ---------------------------------------------------------------------------
# Warning detection
# ---------------------------------------------------------------------------

def detect_warnings(results: list) -> list:
    """
    Inspect a list of ExtractionResult objects (one per --select spec) and
    return a list of BundleWarning describing potential problems.

    Checks performed:
      1. Module name collision — two results have the same source_module but
         come from different source_packages.
      2. Cross-package coupling — a result's sibling modules import names
         that belong to another selected package.

    Parameters
    ----------
    results : list[ExtractionResult]
        All extraction results for this bundle run.

    Returns
    -------
    list[BundleWarning]
    """
    warnings: list[BundleWarning] = []

    # --- Check 1: module name collisions ---
    # Build a map from source_module → list of source_packages that use it
    module_to_packages: dict[str, set[str]] = {}
    for r in results:
        module_to_packages.setdefault(r.source_module, set()).add(r.source_package)

    for module_name, packages in module_to_packages.items():
        if len(packages) > 1:
            warnings.append(BundleWarning(
                kind="name_collision",
                message=(
                    f"Module name collision: '{module_name}' appears in multiple "
                    f"source packages: {sorted(packages)}. The generated bundle may "
                    f"have ambiguous imports — review the extracted files carefully."
                )
            ))

    # --- Check 2: cross-package coupling in sibling modules ---
    # For each result, check whether any of its sibling modules import from
    # another result's source_package.
    selected_packages = {r.source_package for r in results}

    for result in results:
        other_packages = selected_packages - {result.source_package}
        if not other_packages or not result.local_sibling_modules:
            continue

        for sibling_dotted in result.local_sibling_modules:
            # Try to find the sibling module's source and scan its imports
            sibling_imports = _get_module_top_level_imports(sibling_dotted)
            for imported_module in sibling_imports:
                top = imported_module.split(".")[0]
                if top in other_packages:
                    warnings.append(BundleWarning(
                        kind="cross_package_coupling",
                        message=(
                            f"Cross-package coupling detected: sibling module "
                            f"'{sibling_dotted}' (pulled in wholesale for "
                            f"'{result.select_spec}') imports from '{top}', which is "
                            f"another selected source package. The bundle will NOT be "
                            f"fully independent of the original packages."
                        )
                    ))

    return warnings


def _get_module_top_level_imports(module_name: str) -> list:
    """
    Return the list of dotted module names that appear in top-level import
    statements of the given module.  Returns an empty list if the module
    can't be located or parsed (fail silently — this is a best-effort check).

    NOTE: for relative imports this returns the bare module name as written
    (e.g. "models" for `from .models import X`), not re-qualified with the
    owning package. That's fine for this specific check (cross-package
    coupling only cares about whether the top-level name matches ANOTHER
    selected package, and a relative import can never resolve to a
    different top-level package than its own), but callers doing anything
    else with these names should be aware they may be bare/unqualified.
    """
    try:
        spec = importlib.util.find_spec(module_name)
    except Exception:
        return []

    if spec is None or spec.origin is None:
        return []

    source_file = Path(spec.origin)
    if not source_file.exists():
        return []

    try:
        module_ast = ast.parse(source_file.read_text(encoding="utf-8"))
    except Exception:
        return []

    imported: list[str] = []
    for node in module_ast.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)

    return imported