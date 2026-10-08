"""Build the committed Pi artifact as an E2B template; no repository is embedded."""

import argparse
import json
import subprocess
from pathlib import Path

from e2b import Template, default_build_logger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    context = Path(__file__).resolve().parent
    subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", str(context)],
        cwd=context,
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=context, text=True).strip()
    dockerfile = (context / "Dockerfile").read_text()
    # Docker keeps PID 1 alive across control connections. E2B owns that VM
    # lifetime itself; commands.run opens each admitted controller explicitly.
    lines = [line for line in dockerfile.splitlines() if not line.startswith("ENTRYPOINT ")]
    template = (
        Template(file_context_path=context)
        .from_dockerfile("\n".join(lines))
        .set_ready_cmd("test -r /opt/puppyone-agent/worker.mjs && test -w /workspace")
    )
    result = Template.build(
        template,
        alias=args.alias,
        cpu_count=2,
        memory_mb=2048,
        on_build_logs=default_build_logger(),
    )
    args.output.write_text(
        json.dumps(
            {
                "source_sha": sha,
                "alias": args.alias,
                "template_id": result.template_id,
                "build_id": result.build_id,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
