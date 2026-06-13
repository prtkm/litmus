"""The recompute sandbox (DESIGN §7 G3, §15).

Runs verifier recompute scripts and untrusted synthesized/contributed verifier code in an
isolated subprocess that is:

  * **network-less**  — a safety invariant; do NOT weaken (DESIGN §15). Enforced by an
        auto-imported ``sitecustomize`` that neutralizes ``socket`` before any user code runs,
        plus a stripped environment (no creds, no proxies).
  * **resource-limited** — CPU seconds, memory, and file size via ``setrlimit`` set in the
        parent before ``exec`` (so the child cannot raise its own hard limits), plus a
        wall-clock timeout that kills the whole process group.
  * **deterministic** — ``PYTHONHASHSEED=0``, ``LC_ALL=C``, a scratch cwd, minimal env.

This is the *recompute* sandbox only. The networked T6 reproducibility container (a paper's
own data/code) is a separate, heavier environment with a different trust model (DESIGN §15) —
they must not share an environment.

NOTE on the trust boundary: subprocess isolation + the socket block + rlimits are the
portable local guarantee. True filesystem/OS confinement for *adversarial* contributed code
is provided by the managed-agents sandbox in production (DESIGN §13). Locally we run our own
first-party + synthesized recompute scripts under this profile.
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_CPU_S = 10
DEFAULT_MEM_MB = 1024
DEFAULT_FSIZE_MB = 32

# Auto-imported at interpreter startup (it is placed on PYTHONPATH and `site` is enabled).
# Runs *before* the user's recompute script, so the network is dead before line 1.
_SITECUSTOMIZE = '''\
# Installed by the LITMUS recompute sandbox. Network is a safety invariant (DESIGN §15).
import socket as _s


class _NetworkBlocked(OSError):
    pass


def _deny(*_a, **_k):
    raise _NetworkBlocked(
        "network access is disabled in the LITMUS recompute sandbox (DESIGN §15)"
    )


# Kill socket creation and name resolution at the root: urllib/http/requests all go through here.
_s.socket = _deny
_s.create_connection = _deny
_s.getaddrinfo = _deny
_s.gethostbyname = _deny
try:
    _s.create_server = _deny
except Exception:
    pass
'''


@dataclass
class SandboxResult:
    """Outcome of running one script in the recompute sandbox."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _apply_limits(cpu_s: int, mem_bytes: int, fsize_bytes: int):
    """Return a preexec_fn that sets hard resource limits in the child before exec."""

    def _apply() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
        except (ValueError, OSError):
            pass
        if mem_bytes:
            # RLIMIT_AS is enforced on Linux (managed-agents executor); macOS often ignores it.
            for name in ("RLIMIT_AS", "RLIMIT_DATA"):
                lim = getattr(resource, name, None)
                if lim is not None:
                    try:
                        resource.setrlimit(lim, (mem_bytes, mem_bytes))
                    except (ValueError, OSError):
                        pass

    return _apply


def run_script(
    script: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    cpu_s: int = DEFAULT_CPU_S,
    mem_mb: int = DEFAULT_MEM_MB,
    fsize_mb: int = DEFAULT_FSIZE_MB,
    stdin: Optional[str] = None,
) -> SandboxResult:
    """Run ``script`` (a self-contained Python program) in the recompute sandbox.

    Returns a :class:`SandboxResult`. Does not raise on script error — inspect ``.ok`` /
    ``.returncode`` / ``.stderr``.
    """
    with tempfile.TemporaryDirectory(prefix="litmus-sbx-") as tmp_name:
        tmp = Path(tmp_name)
        site_dir = tmp / "_sandbox_site"
        site_dir.mkdir()
        (site_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE)
        script_path = tmp / "recompute.py"
        script_path.write_text(script)

        # Minimal, deterministic, credential-free environment.
        env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(site_dir),
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(tmp),
            "HOME": str(tmp),
            "no_proxy": "*",
            "NO_PROXY": "*",
        }

        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(tmp),
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_apply_limits(cpu_s, mem_mb * 1024 * 1024, fsize_mb * 1024 * 1024),
            start_new_session=True,  # own process group, so timeout can kill children too
        )
        try:
            out, err = proc.communicate(input=stdin, timeout=timeout_s)
            return SandboxResult(out, err, proc.returncode, timed_out=False)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = "", ""
            return SandboxResult(
                out or "",
                (err or "") + "\n[litmus-sandbox] killed: wall-clock timeout",
                returncode=-signal.SIGKILL,
                timed_out=True,
            )


def reproduces(
    script: str,
    expected_output: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    strip: bool = True,
) -> tuple[bool, SandboxResult]:
    """Run ``script`` and check its stdout reproduces ``expected_output`` (DESIGN §7 G3).

    Returns ``(reproduced, result)``. ``reproduced`` is True iff the script exited 0 and its
    stdout matches ``expected_output`` (trailing-whitespace-insensitive by default).
    """
    result = run_script(script, timeout_s=timeout_s)
    if not result.ok:
        return False, result
    got = result.stdout
    want = expected_output
    if strip:
        got = got.strip()
        want = want.strip()
    return (got == want), result
