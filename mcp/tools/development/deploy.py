import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def deploy_agent(agent_dir: str, env_file: str | None = None) -> dict:
        """Install + enable + START agent sidecar via systemd, on the LOCAL host.

        Calls (locally, via subprocess):
            sidecar.py service --name <name> install
                --workdir <agent_dir> --env-file <env> --sidecar-path <sidecar.py>

        This is local-only — it cannot deploy to a remote production host
        (no SSH). For remote deploys, the one canonical command is:

            ssh <user>@<host> 'cd <agents_root> && sudo .venv/bin/python sidecar/sidecar.py \\
                service --name <slug> install --env-file test-agents/.env.<slug>'

        `install` already enables and starts the unit — no need to follow with
        `systemctl start`. Pass the bare slug to `--name` (auto-suffixed with
        `-ctlx-agent`). If sidecar.py lives under a subpath on the host,
        symlink `<agents_root>/sidecar` → its directory so the command stays
        canonical.
        """
        project_root = os.getenv("CATALLAXY_PROJECT_ROOT", "/media/second_disk/cont5")
        env_path = str(Path(env_file or str(Path(agent_dir) / ".env")).resolve())

        env_values: dict[str, str] = {}
        for line in Path(env_path).read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env_values[k.strip()] = v.strip()

        agent_name = env_values.get("AGENT_NAME", Path(agent_dir).name).lower().replace(" ", "-")
        service_name = f"catallaxy-{agent_name}"
        sidecar_py = str(Path(project_root) / "sidecar" / "sidecar.py")
        python_bin = str(Path(project_root) / ".venv" / "bin" / "python")

        cmd = [
            python_bin, sidecar_py,
            "service", "--name", service_name,
            "install",
            "--workdir", str(Path(agent_dir).resolve()),
            "--env-file", env_path,
            "--sidecar-path", sidecar_py,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_root)
        if result.returncode != 0:
            raise RuntimeError(f"Deploy failed: {result.stderr or result.stdout}")

        return {
            "service_name": service_name,
            "status": "active",
            "command": f"{python_bin} {sidecar_py} run --env-file {env_path}",
        }
