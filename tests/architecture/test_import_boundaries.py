from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "src" / "careerkit"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_products_do_not_import_each_other() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        imports = _imports(path)
        rel = path.relative_to(ROOT)
        if rel.parts[0] == "jobs" and any(name.startswith("careerkit.resume") for name in imports):
            offenders.append(str(rel))
        if rel.parts[0] == "resume" and any(name.startswith("careerkit.jobs") for name in imports):
            offenders.append(str(rel))
    assert offenders == []


def test_domain_layers_do_not_import_higher_layers_or_legacy_templates() -> None:
    offenders: list[str] = []
    for product in ("jobs", "resume"):
        domain_root = ROOT / product / "domain"
        for path in domain_root.rglob("*.py"):
            imports = _imports(path)
            rel = path.relative_to(ROOT)
            if any(name.startswith(f"careerkit.{product}.application") for name in imports):
                offenders.append(f"{rel}:application")
            if any(name.startswith(f"careerkit.{product}.adapters") for name in imports):
                offenders.append(f"{rel}:adapters")
            if any(name.startswith(f"careerkit.{product}.cli") for name in imports):
                offenders.append(f"{rel}:cli")
            if any(name == "templates" or name.startswith("templates.") for name in imports):
                offenders.append(f"{rel}:templates")
    assert offenders == []


def test_application_layers_do_not_import_cli_or_legacy_templates() -> None:
    offenders: list[str] = []
    for product in ("jobs", "resume"):
        application_root = ROOT / product / "application"
        for path in application_root.rglob("*.py"):
            imports = _imports(path)
            rel = path.relative_to(ROOT)
            if any(name.startswith(f"careerkit.{product}.cli") for name in imports):
                offenders.append(f"{rel}:cli")
            if any(name == "templates" or name.startswith("templates.") for name in imports):
                offenders.append(f"{rel}:templates")
    assert offenders == []


def test_installable_package_avoids_legacy_template_imports() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        imports = _imports(path)
        if any(name == "templates" or name.startswith("templates.") for name in imports):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
