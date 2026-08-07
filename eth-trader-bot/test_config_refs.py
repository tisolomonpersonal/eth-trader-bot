"""
Static check: every `config.X` / `grid_config.X` reference resolves.

This exists because of a real outage. `telegram_bot.alert_started()` referenced
config attributes left behind by an earlier strategy rewrite. It is the first
call in `run_bot()`, so the trading thread died with an AttributeError on every
boot — and nothing noticed, because the failure was inside a daemon thread and
the web process kept serving happily.

Attribute references are only checked when the line executes, so a renamed or
deleted config key stays invisible until the exact moment it matters. This
walks the AST instead: no imports, no network, no keys.

    python test_config_refs.py
"""
import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).parent

# Module alias -> file that must define the names. Add new config modules here.
CONFIG_MODULES = {
    "config": HERE / "config.py",
    "grid_config": HERE / "grid_config.py",
}


def defined_names(path: pathlib.Path) -> set:
    """Top-level names a config module assigns or imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def local_aliases(tree: ast.AST) -> dict:
    """Map the name a module imports a config module under, back to the module."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in CONFIG_MODULES:
                    aliases[a.asname or a.name] = a.name
    return aliases


def main() -> int:
    known = {mod: defined_names(path) for mod, path in CONFIG_MODULES.items()}
    for mod, names in known.items():
        print(f"{mod}.py defines {len(names)} names")

    problems = []
    checked = 0

    for path in sorted(HERE.glob("*.py")):
        if path.name in {p.name for p in CONFIG_MODULES.values()}:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            problems.append(f"{path.name}: does not parse — {e}")
            continue

        aliases = local_aliases(tree)
        if not aliases:
            continue
        checked += 1

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            mod = aliases.get(node.value.id)
            if mod and node.attr not in known[mod]:
                problems.append(
                    f"{path.name}:{node.lineno}  {node.value.id}.{node.attr} "
                    f"is not defined in {mod}.py"
                )

    print(f"checked {checked} modules that import a config module")

    if problems:
        print(f"\n{len(problems)} unresolved reference(s):")
        for p in problems:
            print(f"  {p}")
        return 1

    print("all config references resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
