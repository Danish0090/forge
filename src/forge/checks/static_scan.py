"""Static scan: read generated code with the `ast` module and flag dangerous constructs.

SECURITY-CRITICAL. Changes here need the project owner's review.
Nothing is executed. This is one layer, not the boundary: the sandbox is the real
boundary, and a determined author can get around a static rule. The scan is here to
catch the obvious, cheap, and common cases before a container is ever started.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

from forge.checks.finding import Finding

# Modules a tool has no business importing. Each one reaches the network, runs
# processes, loads native code, or reads the host environment.
DENIED_MODULES = frozenset(
    {
        "socket",
        "subprocess",
        "ctypes",
        "urllib",
        "http",
        "requests",
        "ftplib",
        "smtplib",
        "multiprocessing",
        "pty",
        "importlib",
        "os",
        "sys",
        "builtins",  # builtins.exec / getattr(builtins, ...) dodge the denied-call rule
        "posix",  # posix.system runs a shell command without importing os
        "nt",  # the Windows equivalent
    }
)

# Builtins that run arbitrary code or bypass the normal import path.
DENIED_CALLS = frozenset({"eval", "exec", "compile", "__import__", "breakpoint"})

# Builtins that read or write attributes by name. A name built at runtime defeats every other rule.
DYNAMIC_ACCESS = frozenset({"getattr", "setattr", "delattr"})

# Attributes used in classic sandbox-escape chains (walking up to object/globals).
DENIED_ATTRIBUTES = frozenset(
    {
        "__subclasses__",
        "__globals__",
        "__builtins__",
        "__code__",
        "__class__",
        "__bases__",
        "__mro__",
    }
)


def scan_source(source: str, filename: str = "tool.py") -> list[Finding]:
    """Return one Finding per dangerous construct in `source`."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [Finding("syntax", f"does not parse: {exc.msg}", line=exc.lineno)]
    except (RecursionError, MemoryError, ValueError):
        # Hostile input (extreme nesting, null bytes) must fail the scan, not crash the host.
        return [Finding("unparseable", "parser could not handle this source")]

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                findings.extend(_module_finding(alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            findings.extend(_module_finding(node.module, node.lineno))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in DENIED_CALLS:
                findings.append(
                    Finding("denied-call", f"call to {node.func.id}() is not allowed", line=node.lineno)
                )
            elif node.func.id in DYNAMIC_ACCESS and not _plain_name_arg(node):
                findings.append(
                    Finding(
                        "denied-call",
                        f"{node.func.id}() needs a plain string-literal attribute name",
                        line=node.lineno,
                    )
                )
        elif isinstance(node, ast.Name) and node.id in DENIED_ATTRIBUTES:
            findings.append(Finding("denied-attribute", f"name {node.id} is not allowed", line=node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in DENIED_ATTRIBUTES:
            findings.append(
                Finding("denied-attribute", f"attribute {node.attr} is not allowed", line=node.lineno)
            )
    return findings


def _plain_name_arg(call: ast.Call) -> bool:
    """True when the attribute name is a string literal that is not a dunder."""
    if len(call.args) < 2:
        return False
    name = call.args[1]
    return isinstance(name, ast.Constant) and isinstance(name.value, str) and not name.value.startswith("__")


def _module_finding(module: str, line: int) -> list[Finding]:
    top = module.split(".")[0]
    # Private C modules (_socket, _posixsubprocess, _ctypes, ...) are the raw versions of denied ones.
    if top in DENIED_MODULES or (top.startswith("_") and top != "__future__"):
        return [Finding("denied-import", f"import of {module} is not allowed", line=line)]
    return []


# A generated tool has no reason to be large; a huge file is a parser-exhaustion attempt.
MAX_FILE_BYTES = 256 * 1024


def scan_tool_dir(tool_dir: Path) -> list[Finding]:
    """Scan every .py file under `tool_dir`. Paths in findings are relative and use forward slashes."""
    findings: list[Finding] = []
    for py_file in sorted(tool_dir.rglob("*.py")):
        rel = py_file.relative_to(tool_dir).as_posix()
        if py_file.is_symlink():
            findings.append(Finding("symlink", "symlinks are not allowed", path=rel))
            continue
        if py_file.stat().st_size > MAX_FILE_BYTES:
            findings.append(Finding("file-too-large", f"over {MAX_FILE_BYTES} bytes", path=rel))
            continue
        source = py_file.read_text(encoding="utf-8", errors="replace")
        for finding in scan_source(source, filename=rel):
            findings.append(replace(finding, path=rel))
    return findings
