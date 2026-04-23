#!/usr/bin/env python3
"""
Mirrors workshop repos from GitHub into a Gitea organization.

Reads repo list from a ConfigMap YAML file (default: repos-configmap.yaml).
Reads Gitea admin credentials from the cluster deployment (requires oc login).

Credential resolution order:
  1. CLI args (--username / --password)
  2. Environment variables (GITEA_USERNAME / GITEA_PASSWORD)
  3. Live cluster - reads GITEA_ADMIN_USERNAME / GITEA_ADMIN_PASSWORD
     from the gitea-service deployment in the gitea namespace

Usage:
  python3 migrate-to-gitea.py --gitea-url https://gitea.apps.example.com
  python3 migrate-to-gitea.py --gitea-url https://gitea.apps.example.com --dry-run

Options:
  --configmap  Path to the ConfigMap YAML file (default: repos-configmap.yaml)
  --dry-run    Print what would happen without making any changes.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import requests
import urllib3
import yaml

urllib3.disable_warnings()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_credentials_from_cluster():
    """Read GITEA_ADMIN_USERNAME and GITEA_ADMIN_PASSWORD from the gitea-service deployment."""
    try:
        result = subprocess.run(
            ["oc", "get", "deployment", "gitea-service", "-n", "gitea",
             "-o", "jsonpath={.spec.template.spec.initContainers}"],
            capture_output=True, text=True, check=True,
        )
        containers = json.loads(result.stdout)
        username, password = None, None
        for container in containers:
            for env in container.get("env", []):
                if env.get("name") == "GITEA_ADMIN_USERNAME":
                    username = env.get("value")
                if env.get("name") == "GITEA_ADMIN_PASSWORD":
                    password = env.get("value")
        return username, password
    except Exception as exc:
        sys.exit(f"Failed to read credentials from cluster: {exc}")


def load_configmap(path):
    with open(path) as f:
        cm = yaml.safe_load(f)
    return yaml.safe_load(cm["data"]["repos.yaml"])


def gitea_get(base_url, auth, path):
    return requests.get(f"{base_url}/api/v1{path}", auth=auth, verify=False)


def gitea_post(base_url, auth, path, data):
    return requests.post(f"{base_url}/api/v1{path}", auth=auth, json=data, verify=False)


def ensure_org(base_url, auth, org):
    resp = gitea_get(base_url, auth, f"/orgs/{org}")
    if resp.status_code == 200:
        print(f"  org '{org}' already exists")
        return
    resp = gitea_post(base_url, auth, "/orgs", {
        "username": org,
        "visibility": "public",
        "repo_admin_change_team_access": True,
    })
    if resp.status_code == 201:
        print(f"  org '{org}' created")
    elif resp.status_code == 422:
        print(f"  org '{org}' already exists")
    else:
        sys.exit(f"Failed to create org '{org}': {resp.status_code} {resp.text}")


def repo_exists(base_url, auth, org, name):
    return gitea_get(base_url, auth, f"/repos/{org}/{name}").status_code == 200


def create_repo(base_url, auth, org, name):
    resp = gitea_post(base_url, auth, f"/orgs/{org}/repos", {
        "name": name,
        "private": False,
        "auto_init": False,
        "default_branch": "main",
    })
    if resp.status_code != 201:
        raise RuntimeError(f"{resp.status_code} {resp.text}")


def mirror_repo(github_url, gitea_push_url):
    tmpdir = tempfile.mkdtemp()
    try:
        bare = os.path.join(tmpdir, "repo.git")
        subprocess.run(["git", "clone", "--mirror", github_url, bare], check=True)
        subprocess.run(["git", "push", "--mirror", gitea_push_url], cwd=bare, check=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gitea-url", default=os.environ.get("GITEA_URL"),      help="Gitea base URL")
    parser.add_argument("--username",  default=os.environ.get("GITEA_USERNAME"), help="Gitea admin username (optional, reads from cluster if omitted)")
    parser.add_argument("--password",  default=os.environ.get("GITEA_PASSWORD"), help="Gitea admin password (optional, reads from cluster if omitted)")
    parser.add_argument("--configmap", default=os.path.join(SCRIPT_DIR, "repos-configmap.yaml"), help="Path to ConfigMap YAML")
    parser.add_argument("--dry-run",   action="store_true", help="Print actions without executing")
    args = parser.parse_args()

    if not args.gitea_url:
        sys.exit("--gitea-url or GITEA_URL is required")
    if not os.path.exists(args.configmap):
        sys.exit(f"ConfigMap file not found: {args.configmap}")

    username = args.username
    password = args.password

    if not username or not password:
        print("No credentials provided — reading from cluster...")
        username, password = get_credentials_from_cluster()
        if not username or not password:
            sys.exit("Could not read credentials from cluster.")
        print(f"  username: {username}\n")

    config     = load_configmap(args.configmap)
    org        = config.get("gitea_org", "developers")
    github_org = config.get("github_org", "na-launch-workshop")
    repos      = config.get("repos", [])

    base_url = args.gitea_url.rstrip("/")
    auth     = (username, password)

    print(f"Gitea:      {base_url}")
    print(f"Org:        {org}")
    print(f"GitHub org: {github_org}")
    print(f"Repos:      {len(repos)}\n")

    if not repos:
        sys.exit("No repos found in ConfigMap.")

    if not args.dry_run:
        ensure_org(base_url, auth, org)

    push_base = base_url.replace("https://", f"https://{username}:{password}@") \
                        .replace("http://",  f"http://{username}:{password}@")

    errors = []
    for repo in repos:
        name  = repo.get("name")
        title = repo.get("title", name)

        print(f"── {name}")
        print(f"   {title}")

        if repo_exists(base_url, auth, org, name):
            print(f"   SKIP: already exists\n")
            continue

        github_url = f"https://github.com/{github_org}/{name}.git"

        if args.dry_run:
            print(f"   WOULD clone {github_url}")
            print(f"   WOULD push  {base_url}/{org}/{name}.git\n")
            continue

        try:
            create_repo(base_url, auth, org, name)
            mirror_repo(github_url, f"{push_base}/{org}/{name}.git")
            print(f"   OK\n")
        except Exception as exc:
            print(f"   ERR: {exc}\n")
            errors.append(name)

    if errors:
        print(f"Failed: {errors}")
        sys.exit(1)
    else:
        print("Done.")


if __name__ == "__main__":
    main()
