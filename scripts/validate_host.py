"""Dry-build both host pipelines with only this plugin in an isolated plugin root."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

TOOLS = {"trace_open", "trace_query", "trace_search", "trace_read", "trace_edit"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-zoo", required=True, type=Path, help="Agent Zoo source checkout")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    host = args.agent_zoo.expanduser().resolve()
    inspector = host / "skills/agent-zoo-userspace-plugins/scripts/inspect_agent_zoo_plugins.py"
    if not inspector.is_file():
        parser.error("The host checkout does not contain the plugin inspector")
    with tempfile.TemporaryDirectory(prefix="azo-postmortem-validation-") as temp:
        root = Path(temp)
        common = root / "plugins/common"
        common.mkdir(parents=True)
        (common / "session_postmortem.py").symlink_to(repo / "src/session_postmortem.py")
        env = dict(os.environ, AGENT_ZOO_HOME=str(root / "state"))
        proc = subprocess.run([sys.executable, str(inspector), "--repo-root", str(host),
                               "--plugin-root", str(root / "plugins"), "--pipeline", "both",
                               "--init-state", "--json"], capture_output=True, text=True, env=env)
        if proc.returncode:
            print(proc.stdout)
            print(proc.stderr, file=sys.stderr)
            return proc.returncode
        report = json.loads(proc.stdout)
        if report.get("errors"):
            raise RuntimeError(report["errors"])
        result = {}
        for name in ("default", "rlm"):
            pipeline = report["pipelines"][name]
            tools = [tool for tool in pipeline["tools"] if tool["name"] in TOOLS]
            counts = Counter(tool["name"] for tool in tools)
            if set(counts) != TOOLS or any(count != 1 for count in counts.values()):
                raise RuntimeError(f"{name}: missing or duplicate postmortem tools: {counts}")
            for tool in tools:
                if "workspace" not in tool["parameters"] or "state" in tool["parameters"]:
                    raise RuntimeError(f"{name}: invalid tool schema: {tool}")
            result[name] = {"tools": tools, "initialized": True,
                            "warnings": pipeline.get("warnings", [])}
        print(json.dumps({"isolated_plugin_root": True, "live_install_changed": False,
                          "pipelines": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
