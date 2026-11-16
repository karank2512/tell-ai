"""Offline shell-contract tests. No GitHub or network access."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "keepalive-workflows.sh"


class KeepaliveTests(unittest.TestCase):
    def run_case(self, state: str, read_fail: bool = False, write_fail: bool = False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "gh"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,os,sys\n"
                "args=sys.argv[1:]\n"
                "with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(args)+'\\n')\n"
                "allowed=[['api','repos/fixture/repo/actions/workflows/'+w,'--jq','.state'] for w in ('ingest.yml','weekly.yml','ci.yml')]\n"
                "if args not in allowed:\n"
                " with open(os.environ['REJECTED'],'a') as f: f.write(json.dumps(args)+'\\n')\n"
                " sys.exit(97)\n"
                "if os.environ['READ_FAIL']=='1': sys.exit(1)\n"
                "print(os.environ['STATE'])\n"
            )
            fake.chmod(493)
            calls = root / "calls"
            env = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                GITHUB_REPOSITORY="fixture/repo",
                STATE=state,
                CALLS=str(calls),
                READ_FAIL=str(int(read_fail)),
                WRITE_FAIL=str(int(write_fail)),
                REJECTED=str(root / "rejected"),
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
            )
            logged = [json.loads(x) for x in calls.read_text().splitlines()]
            assert not (root / "rejected").exists(), "Unexpected gh invocation"
            expected = [
                ["api", "repos/fixture/repo/actions/workflows/" + w, "--jq", ".state"]
                for w in ("ingest.yml", "weekly.yml", "ci.yml")
            ]
            assert logged == expected
            return (result, logged)

    def test_active_no_writes(self):
        (r, c) = self.run_case("active")
        assert r.returncode == 0
        assert len(c) == 3
        assert not any("-X" in x for x in c)

    def test_manual_pause_no_writes(self):
        (r, c) = self.run_case("disabled_manually")
        assert r.returncode == 0
        assert not any("-X" in x for x in c)

    def test_inactivity_is_reported_without_enable(self):
        r, c = self.run_case("disabled_inactivity")
        assert r.returncode != 0
        assert len(c) == 3
        assert not any("-X" in x for x in c)
        assert "owner review required" in r.stderr

    def test_unknown_and_empty_fail_closed(self):
        for state in ("disabled_fork", "deleted", "", "unexpected"):
            with self.subTest(state=state):
                (r, c) = self.run_case(state)
                assert r.returncode != 0
                assert not any("-X" in x for x in c)

    def test_failed_reads_never_enable(self):
        (r, c) = self.run_case("disabled_inactivity", read_fail=True)
        assert r.returncode != 0
        assert not any("-X" in x for x in c)

    def test_never_calls_enable_even_when_stub_would_fail(self):
        for state in ("active", "disabled_manually", "disabled_inactivity", "unknown"):
            _r, c = self.run_case(state, write_fail=True)
            assert len(c) == 3
            assert not any("-X" in x for x in c)


if __name__ == "__main__":
    unittest.main()
