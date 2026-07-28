"""Read-only release preflight for Arthur and the managed-agent API."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.seed_arthur import (  # noqa: E402
    ARTHUR_SKILL_BUNDLE_VERSION,
    load_arthur_source_bundle,
)


def _run(*args: str) -> str:
    result = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_report(image: str) -> dict[str, Any]:
    _, sources = load_arthur_source_bundle()
    git_status = _run("git", "status", "--porcelain")
    image_id = _run("docker", "image", "inspect", image, "--format", "{{.Id}}")
    container_rows = _run(
        "docker",
        "ps",
        "--filter",
        "label=com.docker.compose.project=deploy",
        "--filter",
        "label=com.docker.compose.service=api",
        "--format",
        "{{.Names}}|{{.Image}}|{{.Status}}",
    )
    replicas = []
    for row in container_rows.splitlines():
        if not row:
            continue
        name, container_image, status = row.split("|", 2)
        replicas.append(
            {
                "name": name,
                "image": container_image,
                "status": status,
                "healthy": "(healthy)" in status,
            }
        )
    return {
        "git_sha": _run("git", "rev-parse", "HEAD"),
        "worktree_dirty": bool(git_status),
        "dirty_paths": [line[3:] for line in git_status.splitlines()],
        "image": image,
        "image_id": image_id,
        "skill_bundle_version": ARTHUR_SKILL_BUNDLE_VERSION,
        "skills": [
            {
                "name": source["name"],
                "version": source["version"],
                "checksum": hashlib.sha256(
                    source["content_md"].encode("utf-8")
                ).hexdigest(),
            }
            for source in sources
        ],
        "api_replicas": replicas,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Exact image tag to inspect")
    parser.add_argument("--expected-api-replicas", type=int, default=5)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()

    try:
        report = build_report(args.image)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        raise SystemExit(1) from exc

    errors: list[str] = []
    replicas = report["api_replicas"]
    if len(replicas) != args.expected_api_replicas:
        errors.append(
            f"expected {args.expected_api_replicas} API replicas, found {len(replicas)}"
        )
    if replicas and not all(replica["healthy"] for replica in replicas):
        errors.append("one or more API replicas are not healthy")
    if args.require_clean and report["worktree_dirty"]:
        errors.append("worktree is dirty")
    report["ok"] = not errors
    report["errors"] = errors
    print(json.dumps(report, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
