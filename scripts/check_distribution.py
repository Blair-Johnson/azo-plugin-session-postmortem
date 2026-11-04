"""Build and smoke-test the dependency-free wheel without installing it anywhere."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main():
    from setuptools.build_meta import build_wheel
    repo = Path(__file__).resolve().parents[1]
    wheel = (repo / "dist" / build_wheel(str(repo / "dist"))).resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "azo_session_postmortem/cli.py" in names
        assert "azo_session_postmortem/engine.py" in names
        entries = next(name for name in names if name.endswith("entry_points.txt"))
        assert "azo-postmortem = azo_session_postmortem.cli:main" in archive.read(entries).decode()
        assert not any(name.startswith(("tests/", ".work/", "examples/")) for name in names)
    with tempfile.TemporaryDirectory(prefix="postmortem-wheel-") as temporary:
        args = {"workspace": str(Path(temporary) / "study"),
                "sessions": [str(repo / "examples/session.json")], "descendants": False}
        program = ("import sys; sys.path.insert(0, sys.argv[1]); "
                   "from azo_session_postmortem.cli import main; "
                   "raise SystemExit(main(sys.argv[2:]))")
        proc = subprocess.run([sys.executable, "-I", "-c", program, str(wheel),
                               "trace_open", "--json", json.dumps(args)],
                              cwd=temporary, capture_output=True, text=True, check=True)
        result = json.loads(proc.stdout)
        assert result["sources"][0]["id"] == "example-parser"
        assert result["counts"]["events"] > 0
    print(json.dumps({"wheel": str(wheel), "isolated_cli_smoke": "passed",
                      "console_entrypoint": "azo-postmortem", "installed": False}))


if __name__ == "__main__":
    main()
