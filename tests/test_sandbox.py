"""The recompute sandbox safety + reproducibility invariants (DESIGN §7 G3, §15).

The sandbox is the trust boundary for executable evidence: it must be network-less, kill
runaway scripts, reproduce expected output for honest stdlib programs, and be deterministic
across runs. These tests pin all four.
"""

from __future__ import annotations

from litmus.core import sandbox


# --- (a) network is dead (safety invariant, DESIGN §15) ----------------------
def test_network_connection_is_blocked():
    script = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
        "    print('CONNECTED')\n"
        "except Exception as e:\n"
        "    print('BLOCKED', type(e).__name__)\n"
    )
    res = sandbox.run_script(script, timeout_s=10)
    # The connection must NOT have succeeded.
    assert "CONNECTED" not in res.stdout
    assert "BLOCKED" in res.stdout


def test_socket_construction_is_blocked():
    """Even constructing a socket is denied at the root (urllib/http go through this)."""
    script = (
        "import socket\n"
        "try:\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    print('GOT_SOCKET')\n"
        "except Exception as e:\n"
        "    print('DENIED', type(e).__name__)\n"
    )
    res = sandbox.run_script(script, timeout_s=10)
    assert "GOT_SOCKET" not in res.stdout
    assert "DENIED" in res.stdout


# --- (b) honest stdlib compute reproduces expected (G3) ----------------------
def test_pure_stdlib_script_reproduces_expected():
    script = (
        "computed = sum([12, 25, 60])\n"
        "print('MISMATCH reported=100 computed=' + str(computed))\n"
    )
    expected = "MISMATCH reported=100 computed=97"
    reproduced, res = sandbox.reproduces(script, expected)
    assert reproduced is True
    assert res.ok
    assert res.stdout.strip() == expected


def test_reproduces_false_when_output_differs():
    script = "print('NOPE')\n"
    reproduced, res = sandbox.reproduces(script, "EXPECTED")
    assert reproduced is False


def test_reproduces_false_when_script_errors():
    script = "raise ValueError('boom')\n"
    reproduced, res = sandbox.reproduces(script, "anything")
    assert reproduced is False
    assert not res.ok
    assert res.returncode != 0


# --- (c) wall-clock timeout kills a sleeper (kept fast) -----------------------
def test_sleep_times_out():
    script = "import time\ntime.sleep(100)\nprint('WOKE')\n"
    res = sandbox.run_script(script, timeout_s=1.5)
    assert res.timed_out is True
    assert "WOKE" not in res.stdout
    assert not res.ok


# --- (d) determinism: same script twice -> same stdout -----------------------
def test_same_script_twice_is_deterministic():
    script = (
        "vals = sorted(['b', 'a', 'c'])\n"
        "print(','.join(vals))\n"
        "print(sum(range(10)))\n"
    )
    r1 = sandbox.run_script(script, timeout_s=10)
    r2 = sandbox.run_script(script, timeout_s=10)
    assert r1.ok and r2.ok
    assert r1.stdout == r2.stdout
    assert r1.stdout.strip() == "a,b,c\n45"


def test_clean_compute_prints_ok():
    """A correct total: the canonical script prints OK (sum_check's PASS-side script body)."""
    script = (
        "PARTS = [40, 35, 25]\n"
        "REPORTED = 100\n"
        "print('OK' if sum(PARTS) == REPORTED else 'MISMATCH')\n"
    )
    res = sandbox.run_script(script, timeout_s=10)
    assert res.ok
    assert res.stdout.strip() == "OK"
