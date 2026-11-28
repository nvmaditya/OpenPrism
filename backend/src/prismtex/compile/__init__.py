"""Compile a project with hardened latexmk and report structured diagnostics."""

from prismtex.compile.argv import build_latexmk_argv, build_tex_env, is_shell_safe_name
from prismtex.compile.compiler import (
    MAX_LOG_TEXT_BYTES,
    NO_TOOLCHAIN,
    TEX_MISSING_MESSAGE,
    CompileBackend,
    Compiler,
    CompileResult,
    CompileService,
    CompileStatus,
    pdf_file,
)
from prismtex.compile.logparse import (
    MAX_DIAGNOSTICS,
    Diagnostic,
    Severity,
    combine_diagnostics,
    parse_blg,
    parse_log,
    parse_tool_output,
)
from prismtex.compile.logs import KEEP_LOGS, CompileLogStore, LogSummary
from prismtex.compile.runner import ExecutableNotFound, RunOutcome, run_hardened

__all__ = [
    "KEEP_LOGS",
    "MAX_DIAGNOSTICS",
    "MAX_LOG_TEXT_BYTES",
    "NO_TOOLCHAIN",
    "TEX_MISSING_MESSAGE",
    "CompileBackend",
    "CompileLogStore",
    "CompileResult",
    "CompileService",
    "CompileStatus",
    "Compiler",
    "Diagnostic",
    "ExecutableNotFound",
    "LogSummary",
    "RunOutcome",
    "Severity",
    "build_latexmk_argv",
    "build_tex_env",
    "combine_diagnostics",
    "is_shell_safe_name",
    "parse_blg",
    "parse_log",
    "parse_tool_output",
    "pdf_file",
    "run_hardened",
]
