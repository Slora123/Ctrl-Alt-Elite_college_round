"""Best-effort static checks on cell source. Uses ``ast.parse`` only: nothing is ever executed or eval'd."""
from __future__ import annotations

import ast
import builtins
import re

_BUILTINS = set(dir(builtins)) | {"datasets", "In", "Out", "get_ipython", "display", "__file__", "__name__"}
_PIP_MAGIC = re.compile(r"^\s*[%!]\s*(?:python\s+-m\s+)?(?:pip|pip3|conda|uv|mamba|apt|apt-get)\b")


def _sanitize(source: str) -> tuple[str, list[str]]:
    """Blank IPython magics/shell lines so ``ast`` can parse; report package-install magics."""
    notes, out = [], []
    for line in source.splitlines():
        s = line.lstrip()
        if s.startswith(("%", "!")):
            if _PIP_MAGIC.match(line):
                notes.append("installs packages inside a cell; use install_package()/pin_version() instead")
            out.append(line[: len(line) - len(s)] + "pass")
        else:
            out.append(line)
    return "\n".join(out), notes


class _Names(ast.NodeVisitor):
    def __init__(self):
        self.defined: set[str] = set()
        self.loaded: dict[str, int] = {}
        self.pip_calls = False

    def _def(self, name: str):
        self.defined.add(name)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            self.loaded.setdefault(node.id, node.lineno)
        else:
            self._def(node.id)

    def visit_FunctionDef(self, node):
        self._def(node.name)
        a = node.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, *(x for x in (a.vararg, a.kwarg) if x)]:
            self._def(arg.arg)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda):
        a = node.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, *(x for x in (a.vararg, a.kwarg) if x)]:
            self._def(arg.arg)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self._def(node.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            self._def((a.asname or a.name).split(".")[0])

    def visit_ImportFrom(self, node):
        for a in node.names:
            self._def(a.asname or a.name)

    def visit_ExceptHandler(self, node):
        if node.name:
            self._def(node.name)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.defined.update(node.names)

    visit_Nonlocal = visit_Global

    def visit_Call(self, node: ast.Call):
        strs = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        text = " ".join(strs)
        fn = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
        if (fn.startswith(("subprocess.", "os.system", "os.popen")) or fn in ("system", "run", "check_call", "Popen")) \
                and re.search(r"\bpip3?\b.*\binstall\b|\buv\s+pip\s+install\b|\bconda\s+install\b", text):
            self.pip_calls = True
        self.generic_visit(node)


def analyze_cell(source: str, declared_inputs: list[str]) -> list[str]:
    """Warnings (never errors): variables read but not declared as inputs, package installs, parse problems."""
    clean, warnings = _sanitize(source)
    try:
        tree = ast.parse(clean)
    except SyntaxError as e:
        return [*dict.fromkeys(warnings), f"could not analyze: syntax error at line {e.lineno}"]
    v = _Names()
    v.visit(tree)
    undeclared = sorted(n for n in v.loaded if n not in v.defined and n not in _BUILTINS and n not in declared_inputs)
    warnings += [f"reads '{n}' (line {v.loaded[n]}) which is not declared as an input" for n in undeclared]
    if v.pip_calls:
        warnings.append("installs packages inside a cell; use install_package()/pin_version() instead")
    return list(dict.fromkeys(warnings))
