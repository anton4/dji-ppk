"""Run rnx2rtkp and locate its outputs."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RtkResult:
    command: list[str]
    returncode: int
    seconds: float
    pos_path: Path
    events_path: Path
    log_path: Path


def rnx2rtkp_binary() -> str:
    return os.environ.get("PPK_RNX2RTKP", "rnx2rtkp")


def rtklib_version() -> str:
    try:
        out = subprocess.run([rnx2rtkp_binary(), "--version"], capture_output=True, text=True, timeout=10)
        txt = (out.stdout + out.stderr).strip()
        return txt.splitlines()[0] if txt else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def write_conf(base_conf: str | Path, dst: str | Path, overrides: dict[str, str] | None = None) -> Path:
    """Copy a .conf and append overrides; RTKLIB keeps the last assignment of a key."""
    text = Path(base_conf).read_text()
    if overrides:
        text = text.rstrip("\n") + "\n\n# --- overrides from command line ---\n"
        for k, v in overrides.items():
            text += f"{k}={v}\n"
    Path(dst).write_text(text)
    return Path(dst)


def run_rnx2rtkp(conf: Path, out_pos: Path, rover_obs: Path, base_obs: Path, nav_files: list[Path],
                 log_path: Path, extra_args: list[str] | None = None) -> RtkResult:
    binary = shutil.which(rnx2rtkp_binary()) or rnx2rtkp_binary()
    cmd = [binary, "-k", str(conf), "-o", str(out_pos), *(extra_args or []),
           str(rover_obs), str(base_obs), *[str(n) for n in nav_files]]
    t0 = time.time()
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    # rnx2rtkp prints a '\r'-separated "processing : <time> Q=n" progress stream; keep only real messages.
    lines = [l.strip() for l in proc.stdout.replace("\r", "\n").splitlines()]
    progress = [l for l in lines if l.startswith("processing :")]
    messages = [l for l in lines if l and not l.startswith("processing :")]
    with open(log_path, "w") as log:
        log.write("$ " + " ".join(cmd) + "\n\n")
        log.write(f"exit code: {proc.returncode}, {len(progress)} progress updates, last: {progress[-1] if progress else '-'}\n")
        if messages:
            log.write("\n".join(messages) + "\n")
    events = out_pos.with_name(out_pos.stem + "_events.pos")
    return RtkResult(cmd, proc.returncode, time.time() - t0, out_pos, events, log_path)
