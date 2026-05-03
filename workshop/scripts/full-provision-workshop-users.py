#!/usr/bin/env python3
"""
Fast-provisions workshop resources for selected users.

This wrapper runs the two user-scoped provisioning steps together:
  - GitLab personal repos
  - Dev Spaces helm release in <user>-devspaces

The interface follows the same ``--user`` repeatable pattern as the other
scripts in this directory.

Usage:
  python3 full-provision-workshop-users.py --user devben
  python3 full-provision-workshop-users.py --user devben --user jane
  python3 full-provision-workshop-users.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GITLAB_SCRIPT = os.path.join(SCRIPT_DIR, "provision-user-repos-gitlab.py")
HELM_SCRIPT = os.path.join(SCRIPT_DIR, "provision-helm-releases.py")


def run_script(script_path: str, extra_args: list[str]) -> None:
    cmd = [sys.executable, script_path, *extra_args]
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", action="append", help="Provision a single user only (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--gitlab-url", default=None, help="GitLab base URL (forwarded to the GitLab script)")
    parser.add_argument("--token", default=None, help="GitLab token (forwarded to the GitLab script)")
    parser.add_argument("--source-group", default=None, help="GitLab source group (forwarded to the GitLab script)")
    args = parser.parse_args()

    gitlab_args: list[str] = []
    for username in args.user or []:
        gitlab_args.extend(["--user", username])
    if args.dry_run:
        gitlab_args.append("--dry-run")
    if args.gitlab_url:
        gitlab_args.extend(["--gitlab-url", args.gitlab_url])
    if args.token:
        gitlab_args.extend(["--token", args.token])
    if args.source_group:
        gitlab_args.extend(["--source-group", args.source_group])

    helm_args: list[str] = []
    for username in args.user or []:
        helm_args.extend(["--user", username])
    if args.dry_run:
        helm_args.append("--dry-run")

    print("== GitLab repos ==")
    run_script(GITLAB_SCRIPT, gitlab_args)

    print("\n== Dev Spaces namespaces ==")
    run_script(HELM_SCRIPT, helm_args)

    print("\nDone.")


if __name__ == "__main__":
    main()
