import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "hl_guard"
OPERATOR_CLI = SRC / "ledger" / "cli.py"
ENTRY_POINT = SRC / "cli" / "__main__.py"
FORBIDDEN_NAMES = {"unlatch", "UNLATCH", "_write"}
CLI_MODULES = {
    "hl_guard.ledger.cli",
    "hl_guard.policy.cli",
    "hl_guard.cli",
    "hl_guard.cli.__main__",
}


def _module(path: Path) -> str:
    return ".".join(("hl_guard", *path.relative_to(SRC).with_suffix("").parts))


def _imported(node: ast.Import | ast.ImportFrom, path: Path) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    package = _module(path).rsplit(".", node.level)[0] if node.level else ""
    base = ".".join(part for part in (package, node.module) if part)
    return {base, *(f"{base}.{alias.name}" for alias in node.names)}


def _references(source: str, path: Path) -> list[tuple[str, str, str]]:
    found = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = None
            match child:
                case ast.Attribute(attr=attr) if attr in FORBIDDEN_NAMES:
                    name = attr
                case ast.Name(id=ident) if ident in FORBIDDEN_NAMES:
                    name = ident
                case ast.Constant(value=str(text)) if text in FORBIDDEN_NAMES | CLI_MODULES:
                    name = repr(text)
                case ast.alias(name=imported, asname=alias) if {
                    imported.rpartition(".")[2],
                    alias,
                } & FORBIDDEN_NAMES:
                    name = imported if alias not in FORBIDDEN_NAMES else alias
                case ast.FunctionDef(name=fn) if fn in FORBIDDEN_NAMES:
                    name = f"def {fn}"
                case ast.Import() | ast.ImportFrom() if path != ENTRY_POINT:
                    if hit := sorted(_imported(child, path) & CLI_MODULES):
                        name = f"import {hit[0]}"
            if name is not None:
                found.append((type(child).__name__, name, scope))
            inner = child.name if isinstance(child, ast.FunctionDef | ast.ClassDef) else scope
            visit(child, inner)

    visit(ast.parse(source, str(path)), "<module>")
    return found


def test_only_the_operator_cli_reaches_an_unlatch() -> None:
    found = {
        str(path.relative_to(SRC)): refs
        for path in sorted(SRC.rglob("*.py"))
        if path != OPERATOR_CLI and (refs := _references(path.read_text(), path))
    }
    assert sorted(set(found.pop("ledger/ledger.py"))) == [
        ("Attribute", "UNLATCH", "_appendable"),
        ("Attribute", "UNLATCH", "_apply"),
        ("Attribute", "UNLATCH", "unlatch"),
        ("Constant", "'unlatch'", "Kind"),
        ("FunctionDef", "def _write", "<module>"),
        ("FunctionDef", "def unlatch", "Tail"),
        ("Name", "UNLATCH", "Kind"),
        ("Name", "_write", "append"),
        ("Name", "_write", "unlatch"),
    ]
    assert found == {}


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("from hl_guard.ledger.cli import latch_clear", "import hl_guard.ledger.cli"),
        ("import hl_guard.ledger.cli as c", "import hl_guard.ledger.cli"),
        ("from hl_guard.ledger import cli", "import hl_guard.ledger.cli"),
        ("from ..ledger import cli", "import hl_guard.ledger.cli"),
        ("from ..ledger.cli import latch_clear", "import hl_guard.ledger.cli"),
        ("from hl_guard.policy.cli import gate_try", "import hl_guard.policy.cli"),
        ("from hl_guard.cli.__main__ import main", "import hl_guard.cli.__main__"),
        ("x = Kind.UNLATCH", "UNLATCH"),
        ("x = Kind['UNLATCH']", "'UNLATCH'"),
        ("lg._write(1)", "_write"),
        ("led.unlatch()", "unlatch"),
        ("getattr(led, 'unlatch')()", "'unlatch'"),
        ("importlib.import_module('hl_guard.ledger.cli')", "'hl_guard.ledger.cli'"),
        ("sys.modules['hl_guard.cli.__main__'].main()", "'hl_guard.cli.__main__'"),
        ("from hl_guard.ledger.ledger import _write as w", "_write"),
        ("from hl_guard.ledger.ledger import Ledger as unlatch", "unlatch"),
    ],
)
def test_the_lint_catches_each_route_to_an_unlatch(source: str, name: str) -> None:
    assert [ref[1] for ref in _references(source, SRC / "exchange" / "probe.py")] == [name]
