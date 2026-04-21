#!/usr/bin/env python3
"""
Mirrors workshop repos from GitHub into a GitLab group.

Reads repo list from a ConfigMap YAML file (default: repos-configmap.yaml).
Reads GitLab admin token from the cluster deployment (requires oc login).

Credential resolution order:
  1. CLI args (--token)
  2. Environment variable (GITLAB_TOKEN)
  3. Live cluster - reads GITLAB_ADMIN_TOKEN from the gitlab-service deployment
     in the gitlab namespace

Usage:
  python3 migrate-to-gitlab.py --gitlab-url https://gitlab.apps.example.com
  python3 migrate-to-gitlab.py --gitlab-url https://gitlab.apps.example.com --dry-run

Options:
  --configmap  Path to the ConfigMap YAML file (default: repos-configmap.yaml)
  --group      GitLab group path to migrate repos into (overrides ConfigMap value)
  --dry-run    Print what would happen without making any changes.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

import requests
import urllib3
import yaml

urllib3.disable_warnings()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_token_from_cluster():
    """Read GITLAB_ADMIN_TOKEN from the gitlab-service deployment in the gitlab namespace."""
    try:
        result = subprocess.run(
            ["oc", "get", "deployment", "gitlab-service", "-n", "gitlab",
             "-o", "jsonpath={.spec.template.spec.containers}"],
            capture_output=True, text=True, check=True,
        )
        containers = json.loads(result.stdout)
        for container in containers:
            for env in container.get("env", []):
                if env.get("name") == "GITLAB_ADMIN_TOKEN":
                    return env.get("value")
    except Exception:
        pass

    # Fallback: try reading from a secret
    try:
        result = subprocess.run(
            ["oc", "get", "secret", "gitlab-admin-token", "-n", "gitlab",
             "-o", "jsonpath={.data.token}"],
            capture_output=True, text=True, check=True,
        )
        import base64
        return base64.b64decode(result.stdout.strip()).decode()
    except Exception as exc:
        sys.exit(f"Failed to read token from cluster: {exc}")


def load_configmap(path):
    with open(path) as f:
        cm = yaml.safe_load(f)
    return yaml.safe_load(cm["data"]["repos.yaml"])


def gl_headers(token):
    return {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}


def gl_get(base_url, token, path):
    return requests.get(f"{base_url}/api/v4{path}", headers=gl_headers(token), verify=False)


def gl_post(base_url, token, path, data):
    return requests.post(f"{base_url}/api/v4{path}", headers=gl_headers(token), json=data, verify=False)


def ensure_group(base_url, token, group_path):
    """Ensure the GitLab group exists; return its numeric ID."""
    encoded = quote(group_path, safe="")
    resp = gl_get(base_url, token, f"/groups/{encoded}")
    if resp.status_code == 200:
        group_id = resp.json()["id"]
        print(f"  group '{group_path}' already exists (id={group_id})")
        return group_id

    resp = gl_post(base_url, token, "/groups", {
        "name": group_path,
        "path": group_path,
        "visibility": "public",
    })
    if resp.status_code == 201:
        group_id = resp.json()["id"]
        print(f"  group '{group_path}' created (id={group_id})")
        return group_id
    elif resp.status_code == 400 and "has already been taken" in resp.text:
        # Race or slug conflict — fetch again
        resp2 = gl_get(base_url, token, f"/groups/{encoded}")
        if resp2.status_code == 200:
            return resp2.json()["id"]
    sys.exit(f"Failed to create group '{group_path}': {resp.status_code} {resp.text}")


def project_exists(base_url, token, group_path, name):
    encoded = quote(f"{group_path}/{name}", safe="")
    return gl_get(base_url, token, f"/projects/{encoded}").status_code == 200


def create_project(base_url, token, group_id, name):
    resp = gl_post(base_url, token, "/projects", {
        "name": name,
        "path": name,
        "namespace_id": group_id,
        "visibility": "public",
        "initialize_with_readme": False,
        "default_branch": "main",
    })
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"{resp.status_code} {resp.text}")


def mirror_repo(github_url, gitlab_push_url):
    tmpdir = tempfile.mkdtemp()
    try:
        bare = os.path.join(tmpdir, "repo.git")
        subprocess.run(["git", "clone", "--mirror", github_url, bare], check=True)
        subprocess.run(["git", "push", "--mirror", gitlab_push_url], cwd=bare, check=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL"),   help="GitLab base URL")
    parser.add_argument("--token",      default=os.environ.get("GITLAB_TOKEN"), help="GitLab admin personal access token (optional, reads from cluster if omitted)")
    parser.add_argument("--group",      default=None,                            help="GitLab group path (overrides ConfigMap)")
    parser.add_argument("--configmap",  default=os.path.join(SCRIPT_DIR, "repos-configmap.yaml"), help="Path to ConfigMap YAML")
    parser.add_argument("--dry-run",    action="store_true",                     help="Print actions without executing")
    args = parser.parse_args()

    if not args.gitlab_url:
        sys.exit("--gitlab-url or GITLAB_URL is required")
    if not os.path.exists(args.configmap):
        sys.exit(f"ConfigMap file not found: {args.configmap}")

    token = args.token
    if not token:
        print("No token provided — reading from cluster...")
        token = get_token_from_cluster()
        if not token:
            sys.exit("Could not read token from cluster.")
        print("  token: (loaded from cluster)\n")

    config     = load_configmap(args.configmap)
    group_path = args.group or config.get("gitea_org", "developers")
    github_org = config.get("github_org", "na-launch-workshop")
    repos      = config.get("repos", [])

    base_url = args.gitlab_url.rstrip("/")

    print(f"GitLab:     {base_url}")
    print(f"Group:      {group_path}")
    print(f"GitHub org: {github_org}")
    print(f"Repos:      {len(repos)}\n")

    if not repos:
        sys.exit("No repos found in ConfigMap.")

    group_id = None
    if not args.dry_run:
        group_id = ensure_group(base_url, token, group_path)

    # Embed token into push URL using oauth2 prefix (works with PATs)
    parsed = base_url.replace("https://", f"https://oauth2:{token}@") \
                     .replace("http://",  f"http://oauth2:{token}@")

    errors = []
    for repo in repos:
        name  = repo.get("name")
        title = repo.get("title", name)

        print(f"── {name}")
        print(f"   {title}")

        if not args.dry_run and project_exists(base_url, token, group_path, name):
            print(f"   SKIP: already exists\n")
            continue

        github_url = f"https://github.com/{github_org}/{name}.git"

        if args.dry_run:
            print(f"   WOULD clone {github_url}")
            print(f"   WOULD push  {base_url}/{group_path}/{name}.git\n")
            continue

        try:
            create_project(base_url, token, group_id, name)
            mirror_repo(github_url, f"{parsed}/{group_path}/{name}.git")
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
