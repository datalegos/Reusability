"""
builder.py -- assembles the new bundle package on disk.

Responsibilities:
  - Accept a list of ExtractionResult objects (one per --select spec) plus
    bundle metadata (name, version, output directory).
  - Create the output directory structure.
  - Write each extracted module as a .py file using ast.unparse().
  - Copy any required sibling modules verbatim, rewriting THEIR imports too
    (a sibling can itself import another sibling, e.g. models.py importing
    from constants.py -- both need to end up pointing at the bundled copies).
  - Generate pyproject.toml (hatchling backend, merged external deps).
  - Generate README.md (human-readable summary of what was bundled).
  - Generate dlctl_manifest.json (machine-readable record of the whole run).
  - Write __init__.py that re-exports all selected functions.
  - Print a clear summary to stdout including any warnings.

All file I/O is isolated here -- extractor.py has no knowledge of the filesystem.
"""

import ast
import importlib.util
import json
import re
import shutil
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from dlctl.extractor import ExtractionResult, BundleWarning


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flat_name(dotted_module: str) -> str:
    """
    Convert a dotted module name to a flat identifier safe for use as a
    Python filename stem.

    e.g.  "package_a.utils"  ->  "package_a__utils"
          "package_a.module" ->  "package_a__module"
    """
    return dotted_module.replace(".", "__")


def _unparse_nodes(nodes: list) -> str:
    """
    Turn a list of AST nodes into Python source code via ast.unparse().

    Each node is unparsed separately and joined with appropriate blank lines:
      - Two blank lines before/after function/class definitions.
      - One blank line between imports and after assignments.
    """
    parts: list[str] = []
    for node in nodes:
        code = ast.unparse(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Surround top-level defs with two blank lines (PEP 8)
            parts.append("\n\n" + code)
        else:
            parts.append(code)

    return "\n".join(parts).lstrip("\n")


# ---------------------------------------------------------------------------
# Sibling import rewriting
# ---------------------------------------------------------------------------
#
# ast.unparse() on a RELATIVE ImportFrom node (node.level > 0) reproduces the
# original relative-dot syntax, e.g.:
#
#     from .models import Bug, BugComment
#
# NOT the fully-qualified absolute form. The rewrite logic below must match
# THAT shape -- matching only "from <dotted.sibling.name> import ..." (the
# absolute form) means every real-world relative import silently fails to
# match and passes through unrewritten, still pointing at a module that will
# not exist in the standalone bundle.
#
# We match by TAIL name (the last dotted component of the sibling's fully
# qualified name, e.g. "models" for "bug_lifecycle_engine.models") against
# the bare module name that appears after the leading dot(s) in a relative
# import line. This is safe within a single bundle run because all sibling
# modules being rewritten here belong to the same source_package and cannot
# collide with each other's tails without already triggering a name-collision
# warning elsewhere in the pipeline.

_RELATIVE_FROM_IMPORT_RE = re.compile(r'^from (\.+)([A-Za-z_][A-Za-z0-9_]*) import (.+)$')
_RELATIVE_BARE_IMPORT_RE = re.compile(r'^from (\.+) import (.+)$')


def _rewrite_sibling_imports(source: str, sibling_modules: set) -> str:
    """
    Rewrite import statements in the generated/copied source so that
    references to sibling modules point to the bundled _local_*.py copies
    rather than the original installed package.

    Handles three shapes:

      1. Relative, named module (the common real-world case):
             from .models import Bug, BugComment
         ->  from ._local_bug_lifecycle_engine__models import Bug, BugComment

      2. Relative, bare submodule import:
             from . import models
         ->  from . import _local_bug_lifecycle_engine__models as models

      3. Absolute dotted form (kept for robustness, e.g. if a module
         imports a sibling via its fully-qualified name instead of a
         relative import):
             from bug_lifecycle_engine.models import Bug
             import bug_lifecycle_engine.models
         ->  from ._local_bug_lifecycle_engine__models import Bug
             from . import _local_bug_lifecycle_engine__models as models
    """
    # tail (last dotted component) -> fully-qualified sibling dotted name
    tail_to_sibling: dict[str, str] = {}
    for sibling in sibling_modules:
        tail_to_sibling[sibling.rsplit(".", 1)[-1]] = sibling

    lines = source.splitlines()
    result_lines: list[str] = []

    for line in lines:
        rewritten = line

        # --- Case 1: "from .<tail> import X, Y" (any number of leading dots) ---
        m = _RELATIVE_FROM_IMPORT_RE.match(rewritten)
        if m and m.group(2) in tail_to_sibling:
            sibling = tail_to_sibling[m.group(2)]
            local_name = f"_local_{_flat_name(sibling)}"
            result_lines.append(f"from .{local_name} import {m.group(3)}")
            continue

        # --- Case 2: "from . import name1, name2 [as alias]" ---
        m2 = _RELATIVE_BARE_IMPORT_RE.match(rewritten)
        if m2:
            names = [n.strip() for n in m2.group(2).split(",")]
            new_import_lines = []
            all_matched = True
            for n in names:
                if " as " in n:
                    orig_name, alias = [p.strip() for p in n.split(" as ", 1)]
                else:
                    orig_name, alias = n, n
                if orig_name in tail_to_sibling:
                    sibling = tail_to_sibling[orig_name]
                    local_name = f"_local_{_flat_name(sibling)}"
                    new_import_lines.append(f"from . import {local_name} as {alias}")
                else:
                    all_matched = False
            if all_matched and new_import_lines:
                result_lines.extend(new_import_lines)
                continue
            # Partial or no match on a bare relative import: leave as-is
            # rather than guess -- falls through to append original line below.

        # --- Case 3: absolute dotted form (legacy / defensive) ---
        matched_absolute = False
        for sibling in sibling_modules:
            flat = _flat_name(sibling)
            local_name = f"_local_{flat}"

            prefix = f"from {sibling} import "
            if rewritten.startswith(prefix):
                rest = rewritten[len(prefix):]
                rewritten = f"from .{local_name} import {rest}"
                matched_absolute = True
                break

            if rewritten.startswith(f"import {sibling}"):
                rest = rewritten[len(f"import {sibling}"):]
                if rest == "" or rest.startswith(" as "):
                    alias = rest[len(" as "):] if rest.startswith(" as ") else flat
                    rewritten = f"from . import {local_name} as {alias}"
                    matched_absolute = True
                    break

        result_lines.append(rewritten)

    return "\n".join(result_lines)


def _copy_sibling_module(dotted_module: str, dest_dir: Path, all_sibling_modules: set) -> bool:
    """
    Locate a sibling module's source file and copy it into dest_dir using the
    flat naming convention (_local_<flat_name>.py), rewriting ITS OWN import
    statements so that any references to OTHER sibling modules (e.g. models.py
    importing from constants.py) point at their bundled _local_*.py copies
    too -- a sibling can depend on another sibling, and both need to resolve
    inside the new standalone package.

    Returns True on success, False if the module could not be located.
    """
    try:
        spec = importlib.util.find_spec(dotted_module)
    except Exception:
        spec = None

    if spec is None or spec.origin is None:
        return False

    src_path = Path(spec.origin)
    if not src_path.exists():
        return False

    dest_name = f"_local_{_flat_name(dotted_module)}.py"
    dest_path = dest_dir / dest_name

    header = (
        f"# This file was copied verbatim from: {src_path}\n"
        f"# Source module: {dotted_module}\n"
        f"# Copied by dlctl bundle -- do not edit manually.\n\n"
    )

    original_source = src_path.read_text(encoding="utf-8")
    # Rewrite this sibling's own imports so that references to OTHER sibling
    # modules point to their bundled _local_*.py copies too.
    rewritten_source = _rewrite_sibling_imports(original_source, all_sibling_modules)

    dest_path.write_text(header + rewritten_source, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Individual file writers
# ---------------------------------------------------------------------------

def _write_extracted_module(
    result: ExtractionResult,
    package_src_dir: Path,
    bundle_package_name: str,
) -> str:
    """
    Write the extracted source file for one ExtractionResult.

    File name:  _extracted_<flat_source_module>.py
    Contents:   header comment + unparsed AST nodes (imports, helpers, function).

    Returns the module stem (without .py) for use in __init__.py imports.
    """
    flat = _flat_name(result.source_module)
    filename = f"_extracted_{flat}.py"
    dest = package_src_dir / filename

    header = (
        f"# Extracted by dlctl bundle\n"
        f"# Source package : {result.source_package}\n"
        f"# Source module  : {result.source_module}\n"
        f"# Source file    : {result.source_file}\n"
        f"# Function       : {result.function_name}\n"
        f"# Generated at   : {datetime.now(timezone.utc).isoformat()}\n"
        f"#\n"
        f"# This file contains the target function plus every definition and import\n"
        f"# it transitively depends on from the source module.\n"
        f"# Do not edit manually -- re-run `dlctl bundle` to regenerate.\n\n"
    )

    body = _unparse_nodes(result.nodes_to_emit)

    # Rewrite imports that reference sibling modules so they point to the
    # bundled _local_*.py copies instead of the original package.
    # e.g.  "from .models import Bug, BugComment"
    #   ->  "from ._local_bug_lifecycle_engine__models import Bug, BugComment"
    # This makes the bundle standalone -- it no longer needs the original
    # package installed.
    if result.local_sibling_modules:
        body = _rewrite_sibling_imports(body, result.local_sibling_modules)

    dest.write_text(header + body + "\n", encoding="utf-8")
    return f"_extracted_{flat}"


def _write_init(
    results: list,
    package_src_dir: Path,
    bundle_package_name: str,
    version: str,
) -> None:
    """
    Write __init__.py that re-exports every selected function.

    Pattern:
        from ._extracted_<flat> import <function_name>
        __all__ = [...]
    """
    import_lines: list[str] = []
    all_names: list[str] = []

    # Group by source module so we emit one import line per extracted file
    # (multiple functions from the same source module share one file).
    module_to_functions: dict[str, list[str]] = {}
    for r in results:
        flat = _flat_name(r.source_module)
        module_to_functions.setdefault(f"_extracted_{flat}", []).append(r.function_name)

    for module_stem, func_names in sorted(module_to_functions.items()):
        names_str = ", ".join(sorted(func_names))
        import_lines.append(f"from .{module_stem} import {names_str}")
        all_names.extend(func_names)

    all_list = ", ".join(f'"{n}"' for n in sorted(all_names))
    import_block = "\n".join(import_lines)

    content = (
        f"# {bundle_package_name} -- generated by dlctl bundle\n"
        f"# Version: {version}\n"
        f"#\n"
        f"# This package contains functions extracted from their original packages\n"
        f"# and bundled together as a standalone, self-contained distribution.\n"
        f"# Re-generated by running: dlctl bundle  (see dlctl_manifest.json for details)\n"
        f"\n"
        f"{import_block}\n"
        f"\n"
        f"__all__ = [{all_list}]\n"
        f'__version__ = "{version}"\n'
    )

    (package_src_dir / "__init__.py").write_text(content, encoding="utf-8")


def _write_pyproject_toml(
    output_dir: Path,
    bundle_name: str,
    py_package_name: str,
    version: str,
    external_deps: set,
) -> None:
    """
    Generate pyproject.toml for the bundle package.

    Uses hatchling as the build backend. External deps are listed as
    dependencies with no pinned versions (the consumer decides pins).
    py_package_name is the normalised name (hyphens -> underscores) used
    as the actual Python package directory name.
    """
    if external_deps:
        deps_lines = [f'    "{dep}",' for dep in sorted(external_deps)]
        deps_block = "\n".join(deps_lines)
        dependencies_section = f"dependencies = [\n{deps_block}\n]"
    else:
        dependencies_section = "dependencies = []"

    content = textwrap.dedent(f"""\
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [project]
        name = "{bundle_name}"
        version = "{version}"
        description = "Auto-generated bundle produced by dlctl bundle"
        readme = "README.md"
        requires-python = ">=3.9"
        {dependencies_section}

        [tool.hatch.build.targets.wheel]
        packages = ["src/{py_package_name}"]
    """)

    (output_dir / "pyproject.toml").write_text(content, encoding="utf-8")


def _write_readme(
    output_dir: Path,
    bundle_name: str,
    version: str,
    results: list,
    warnings: list,
) -> None:
    """
    Write a short human-readable README.md summarising what was bundled.
    """
    lines: list[str] = [
        f"# {bundle_name}",
        "",
        f"Auto-generated bundle (version `{version}`) produced by **dlctl bundle**.",
        f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Bundled functions",
        "",
    ]

    for r in results:
        lines.append(f"### `{r.function_name}`")
        lines.append(f"- **Source package:** `{r.source_package}`")
        lines.append(f"- **Source module:** `{r.source_module}`")
        lines.append(f"- **Source file:** `{r.source_file}`")
        if r.external_deps:
            lines.append(
                f"- **External deps:** "
                + ", ".join(f"`{d}`" for d in sorted(r.external_deps))
            )
        if r.local_sibling_modules:
            lines.append(
                f"- **Sibling modules copied wholesale:** "
                + ", ".join(f"`{m}`" for m in sorted(r.local_sibling_modules))
            )
        lines.append("")

    lines += [
        "## Usage",
        "",
        "```python",
        f"from {bundle_name} import {', '.join(r.function_name for r in results)}",
        "```",
        "",
    ]

    if warnings:
        lines += [
            "## Warnings",
            "",
            "> The following issues were detected during bundling. Review carefully.",
            "",
        ]
        for w in warnings:
            lines.append(f"- **[{w.kind}]** {w.message}")
        lines.append("")

    lines += [
        "## Regenerating",
        "",
        "Re-run the original `dlctl bundle` command (see `dlctl_manifest.json`) to",
        "regenerate this package after source changes.",
    ]

    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def _write_manifest(
    output_dir: Path,
    bundle_name: str,
    version: str,
    results: list,
    warnings: list,
    select_specs: list,
) -> None:
    """
    Write dlctl_manifest.json -- machine-readable record of the bundle run.

    Schema:
    {
      "dlctl_version": "...",
      "bundle_name": "...",
      "bundle_version": "...",
      "generated_at": "ISO-8601",
      "select_specs": [...],
      "functions": [
        {
          "select_spec": "...",
          "source_package": "...",
          "source_module": "...",
          "function_name": "...",
          "source_file": "...",
          "local_defs_pulled_in": [...],
          "sibling_modules_copied": [...],
          "external_deps": [...],
        },
        ...
      ],
      "all_external_deps": [...],
      "warnings": [{"kind": "...", "message": "..."}, ...]
    }
    """
    from dlctl import __version__ as dlctl_version

    def _node_label(node: ast.AST) -> str:
        """Extract a human-readable label for an AST node."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.name
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            return ", ".join(names) if names else "<assignment>"
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                return node.target.id
        elif isinstance(node, ast.Import):
            return "import " + ", ".join(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            return f"from {node.module} import ..."
        return type(node).__name__

    all_external: set[str] = set()
    for r in results:
        all_external.update(r.external_deps)

    manifest = {
        "dlctl_version": dlctl_version,
        "bundle_name": bundle_name,
        "bundle_version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "select_specs": list(select_specs),
        "functions": [
            {
                "select_spec": r.select_spec,
                "source_package": r.source_package,
                "source_module": r.source_module,
                "function_name": r.function_name,
                "source_file": str(r.source_file),
                # Helper defs and assignments pulled in -- excludes imports and
                # the target function itself for clarity.
                "local_defs_pulled_in": [
                    _node_label(n) for n in r.nodes_to_emit
                    if not isinstance(n, (ast.Import, ast.ImportFrom))
                    and not (
                        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name == r.function_name
                    )
                ],
                "sibling_modules_copied": sorted(r.local_sibling_modules),
                "external_deps": sorted(r.external_deps),
            }
            for r in results
        ],
        "all_external_deps": sorted(all_external),
        "warnings": [
            {"kind": w.kind, "message": w.message}
            for w in warnings
        ],
    }

    (output_dir / "dlctl_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Main build entry point
# ---------------------------------------------------------------------------

def build_bundle(
    results: list,
    warnings: list,
    bundle_name: str,
    version: str,
    output_dir: Path,
) -> None:
    """
    Assemble the full bundle package on disk.

    Parameters
    ----------
    results : list[ExtractionResult]
        One result per --select spec, from extractor.extract().
    warnings : list[BundleWarning]
        Warnings from extractor.detect_warnings().
    bundle_name : str
        The Python package name for the new bundle (e.g. "new-bundled-package"
        is normalised to "new_bundled_package" for use as a Python identifier).
    version : str
        PEP 440 version string (e.g. "0.1.0").
    output_dir : Path
        Root directory to create the bundle in. Created if it does not exist.
    """
    # Normalise bundle_name to a valid Python identifier
    # (hyphens -> underscores, as PEP 517 build tools do)
    py_package_name = bundle_name.replace("-", "_")

    print(f"\n{'='*60}")
    print(f"  dlctl bundle -- assembling '{bundle_name}' v{version}")
    print(f"  Output: {output_dir.resolve()}")
    print(f"{'='*60}\n")

    # --- Create directory structure ---
    output_dir.mkdir(parents=True, exist_ok=True)
    package_src_dir = output_dir / "src" / py_package_name
    package_src_dir.mkdir(parents=True, exist_ok=True)
    print(f"[+] Created directory structure: {package_src_dir}")

    # --- Collect all external deps across all results ---
    all_external_deps: set[str] = set()
    for r in results:
        all_external_deps.update(r.external_deps)

    # --- Write extracted module files ---
    print("\n[+] Writing extracted module files:")
    for result in results:
        module_stem = _write_extracted_module(result, package_src_dir, py_package_name)
        print(f"    * {module_stem}.py  <- {result.select_spec}")

    # --- Copy sibling modules ---
    # r.local_sibling_modules is now the FULL transitive closure (including
    # siblings-of-siblings) per the extractor fix, so the union across all
    # results is already complete -- no further expansion needed here.
    all_siblings: set[str] = set()
    for r in results:
        all_siblings.update(r.local_sibling_modules)

    if all_siblings:
        print("\n[+] Copying sibling modules wholesale:")
        for sibling in sorted(all_siblings):
            # Pass the FULL sibling set so that if this sibling module itself
            # imports another sibling (e.g. models.py importing constants.py),
            # that import gets rewritten too.
            ok = _copy_sibling_module(sibling, package_src_dir, all_siblings)
            status = "OK" if ok else "FAILED (module not found -- copy manually)"
            print(f"    * _local_{_flat_name(sibling)}.py  <- {sibling}  [{status}]")
    else:
        print("\n[+] No sibling modules to copy.")

    # --- Write __init__.py ---
    _write_init(results, package_src_dir, py_package_name, version)
    print(f"\n[+] Wrote {package_src_dir / '__init__.py'}")

    # --- Write pyproject.toml ---
    _write_pyproject_toml(output_dir, bundle_name, py_package_name, version, all_external_deps)
    print(f"[+] Wrote {output_dir / 'pyproject.toml'}")
    if all_external_deps:
        print(f"    External deps included: {', '.join(sorted(all_external_deps))}")
    else:
        print("    No external (third-party) deps detected.")

    # --- Write README.md ---
    _write_readme(output_dir, bundle_name, version, results, warnings)
    print(f"[+] Wrote {output_dir / 'README.md'}")

    # --- Write dlctl_manifest.json ---
    select_specs = [r.select_spec for r in results]
    _write_manifest(output_dir, bundle_name, version, results, warnings, select_specs)
    print(f"[+] Wrote {output_dir / 'dlctl_manifest.json'}")

    # --- Print warnings prominently ---
    print(f"\n{'='*60}")
    if warnings:
        print(f"  !! {len(warnings)} WARNING(S) -- review before using this bundle:")
        print(f"{'='*60}")
        for i, w in enumerate(warnings, 1):
            print(f"\n  [{i}] [{w.kind.upper()}]")
            for line in textwrap.wrap(w.message, width=56, subsequent_indent="      "):
                print(f"      {line}")
        print()
    else:
        print("  OK  No warnings. Bundle looks self-contained.")
        print(f"{'='*60}")

    # --- Final summary ---
    print(f"\n  Summary")
    print(f"  -------")
    print(f"  Bundle package : {bundle_name} ({py_package_name})")
    print(f"  Version        : {version}")
    print(f"  Functions      : {len(results)}")
    for r in results:
        print(f"    - {r.function_name}  (from {r.source_module})")
    print(f"  External deps  : {len(all_external_deps)}")
    print(f"  Sibling copies : {len(all_siblings)}")
    print(f"\n  Install with:")
    print(f"    pip install {output_dir.resolve()}")
    print(f"  Or build a wheel:")
    print(f"    python -m build {output_dir.resolve()}")
    print()