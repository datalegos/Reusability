"""
cli.py -- thin Click-based CLI wrapper for dlctl.

Commands:
  dlctl bundle   Select functions from installed packages and bundle them into
                 a new standalone Python package.

Run `dlctl --help` or `dlctl bundle --help` for usage details.
"""

import sys
from pathlib import Path

import click

from dlctl.extractor import extract, detect_warnings
from dlctl.builder import build_bundle


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="dlctl")
def main():
    """dlctl -- tools for working with local Python packages.

    Use `dlctl COMMAND --help` for help on a specific command.
    """


# ---------------------------------------------------------------------------
# `dlctl bundle` command
# ---------------------------------------------------------------------------

@main.command("bundle")
@click.option(
    "--select", "-s",
    "select_specs",
    multiple=True,
    required=True,
    metavar="MODULE:FUNCTION",
    help=(
        "A function to include in the bundle, specified as "
        "'dotted.module.path:function_name'. "
        "Repeat this option to select multiple functions. "
        "Example: --select package_a.utils:my_func"
    ),
)
@click.option(
    "--output", "-o",
    "output_dir",
    required=True,
    type=click.Path(file_okay=False, writable=True, path_type=Path),
    help=(
        "Directory where the new bundle package will be written. "
        "Created if it does not exist. "
        "Example: --output ./my-bundle"
    ),
)
@click.option(
    "--name", "-n",
    "bundle_name",
    required=True,
    metavar="PACKAGE_NAME",
    help=(
        "Name for the new bundle package (used in pyproject.toml and as the "
        "importable package name, with hyphens converted to underscores). "
        "Example: --name my-bundle"
    ),
)
@click.option(
    "--version", "-v",
    "bundle_version",
    default="0.1.0",
    show_default=True,
    metavar="VERSION",
    help=(
        "PEP 440 version string for the generated package. "
        "Example: --version 1.2.3"
    ),
)
def bundle(select_specs, output_dir, bundle_name, bundle_version):
    """Bundle selected functions from installed packages into a new standalone package.

    Each --select argument names one function to include, using the format:

    \b
        MODULE_PATH:FUNCTION_NAME

    where MODULE_PATH is the full dotted import path to the module (e.g.
    package_a.utils) and FUNCTION_NAME is the function defined in that module.

    \b
    Example:
        dlctl bundle \\
            --select package_a.module_a:function_a \\
            --select package_b.module_b:function_x \\
            --output ./new-bundled-package \\
            --name new-bundled-package \\
            --version 0.1.0

    The tool will:

    \b
      1. Locate each source module via importlib (must be installed/importable).
      2. Parse its AST to find the function and everything it transitively needs.
      3. Write a new package to --output containing only those pieces plus a
         generated pyproject.toml, README.md, and dlctl_manifest.json.

    \b
    Known v1 limitations:
      - Sibling modules (same top-level package) are copied wholesale, not pruned.
      - Star imports (from x import *) in source modules are preserved as-is.
      - Only functions and classes can be selected; plain variables cannot.
    """
    # ------------------------------------------------------------------
    # Step 1: extract each selected function
    # ------------------------------------------------------------------
    click.echo(f"\nExtracting {len(select_specs)} function(s)...\n")

    results = []
    any_error = False

    for spec_str in select_specs:
        click.echo(f"  -> {spec_str}")
        try:
            result = extract(spec_str)
            results.append(result)
            click.echo(
                f"     Found in {result.source_file}\n"
                f"     {len(result.nodes_to_emit)} node(s) to emit, "
                f"{len(result.local_sibling_modules)} sibling module(s), "
                f"{len(result.external_deps)} external dep(s)."
            )
        except ValueError as exc:
            # Report the error clearly but continue so we surface ALL broken
            # specs in one pass (saves multiple fix-and-retry cycles).
            click.secho(f"\n     ERROR: {exc}\n", fg="red", err=True)
            any_error = True

    if any_error:
        click.secho(
            "\nOne or more --select specs failed. Fix the errors above and retry.\n",
            fg="red",
            err=True,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2: detect warnings across the full set of results
    # ------------------------------------------------------------------
    warnings = detect_warnings(results)

    if warnings:
        click.secho(
            f"\n!! {len(warnings)} warning(s) detected -- see details in the bundle output.\n",
            fg="yellow",
        )

    # ------------------------------------------------------------------
    # Step 3: build the bundle on disk
    # ------------------------------------------------------------------
    try:
        build_bundle(
            results=results,
            warnings=warnings,
            bundle_name=bundle_name,
            version=bundle_version,
            output_dir=output_dir,
        )
    except Exception as exc:
        click.secho(f"\nBundle assembly failed: {exc}\n", fg="red", err=True)
        raise  # re-raise so the full traceback is visible for debugging

    # ------------------------------------------------------------------
    # Step 4: note where the manifest lives
    # ------------------------------------------------------------------
    manifest_path = output_dir / "dlctl_manifest.json"
    if manifest_path.exists():
        click.secho(
            f"\n  Manifest: {manifest_path.resolve()}",
            fg="cyan",
        )

    click.secho("\n  Done.\n", fg="green", bold=True)
