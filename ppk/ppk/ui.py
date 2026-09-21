"""Terminal presentation: colors, step markers and report highlighting.

Colors are used only on the terminal: enabled when PPK_COLOR=1 (the launcher sets it when the host has a
TTY, the container itself never has one), or when stdout is a TTY, and never when NO_COLOR is set or
PPK_COLOR=0. Report files on disk stay plain because coloring happens at print time.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36"}


def enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    v = os.environ.get("PPK_COLOR")
    if v is not None:
        return v.lower() in ("1", "true", "yes", "on")
    return sys.stdout.isatty()


def c(text: str, *styles: str) -> str:
    if not enabled() or not styles:
        return text
    return "".join(f"\033[{_CODES[s]}m" for s in styles) + text + "\033[0m"


def ok(text: str) -> str:
    return c("✔ " + text, "green")


def warn(text: str) -> str:
    return c("! " + text, "yellow")


def bad(text: str) -> str:
    return c("✘ " + text, "red", "bold")


def step(n: int, total: int, text: str) -> str:
    return c(f"[{n}/{total}]", "cyan", "bold") + " " + text


def pct(value: float, good: float = 99.0, fair: float = 95.0) -> str:
    """Percentage colored by how good it is."""
    s = f"{value:.1f} %"
    return c(s, "green") if value >= good else c(s, "yellow") if value >= fair else c(s, "red", "bold")


_RULE = re.compile(r"^[=\-]{10,}$")
_LEVEL = re.compile(r"\[(PASS|INFO|WARN|FAIL)\]")


def colorize_report(text: str) -> str:
    """Highlight a plain report: rules dim, titles bold, PASS/WARN/FAIL and verdict words colored."""
    if not enabled():
        return text
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.rstrip()
        if _RULE.match(s.strip()):
            out.append(c(s, "dim"))
        elif i > 0 and _RULE.match(lines[i - 1].strip()) and (i + 1 < len(lines) and _RULE.match(lines[i + 1].strip())):
            out.append(c(s, "bold", "cyan"))  # a title between two rules
        elif s.lstrip().startswith("### "):
            out.append(c(s, "bold", "cyan"))
        else:
            s = re.sub(r"\b(RESULT: PASS[^\n]*)", lambda m: c(m.group(1), "green", "bold"), s)
            s = re.sub(r"\b(RESULT: NOTICE[^\n]*)", lambda m: c(m.group(1), "yellow"), s)
            s = re.sub(r"(100\.0 %|\b(?:9[5-9]|100)\.\d %)", lambda m: c(m.group(1), "green"), s)
            s = _LEVEL.sub(lambda m: "[" + {"PASS": c("PASS", "green"), "INFO": c("INFO", "dim"), "WARN": c("WARN", "yellow", "bold"),
                                          "FAIL": c("FAIL", "red", "bold")}[m.group(1)] + "]", s)
            s = re.sub(r"(Overall: )(PASS)", lambda m: m.group(1) + c(m.group(2), "green", "bold"), s)
            s = re.sub(r"(Overall: )(FAIL)", lambda m: m.group(1) + c(m.group(2), "red", "bold"), s)
            s = re.sub(r"(after PPK with the RINEX base\s+)(about [\d.]+ cm)(\s+)(about [\d.]+ cm)",
                       lambda m: m.group(1) + c(m.group(2), "green", "bold") + m.group(3) + c(m.group(4), "green", "bold"), s)
            s = re.sub(r"(DJI on-board RTK \(as flown\)\s+)(about [\d.]+ cm)(\s+)(about [\d.]+ cm)",
                       lambda m: m.group(1) + c(m.group(2), "yellow") + m.group(3) + c(m.group(4), "yellow"), s)
            out.append(s)
    return "\n".join(out)


NEXT_COLORS = {"done": ("green",), "order": ("yellow", "bold"), "photos": ("yellow",), "process": ("cyan", "bold"),
               "reprocess": ("cyan",), "expired": ("red", "bold")}


def colorize_next(value: str) -> str:
    return c(value, *NEXT_COLORS.get(value, ()))


class ColorFormatter(logging.Formatter):
    """HH:MM:SS  LEVEL  message, with the level colored and INFO shown as a dim timestamp only."""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        if not enabled():
            return f"{ts} {record.levelname:<7} {msg}"
        if record.levelno >= logging.ERROR:
            return f"{c(ts, 'dim')} {c('ERROR  ', 'red', 'bold')} {c(msg, 'red')}"
        if record.levelno >= logging.WARNING:
            return f"{c(ts, 'dim')} {c('WARNING', 'yellow', 'bold')} {c(msg, 'yellow')}"
        if record.levelno <= logging.DEBUG:
            return f"{c(ts, 'dim')} {c('DEBUG  ', 'dim')} {c(msg, 'dim')}"
        return f"{c(ts, 'dim')} {c('INFO   ', 'blue')} {msg}"
