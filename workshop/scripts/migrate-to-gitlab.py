#!/usr/bin/env python3
"""
Mirrors workshop repos from GitHub into a GitLab group.

Reads repo list from a ConfigMap YAML file (default: repos-configmap.yaml).
Reads GitLab admin token from the cluster deployment (requires oc login).

Credential resolution order:
  1. CLI args (--token)
  2. Environment variable (GITLAB_TOKEN)
  3. Live cluster - reads common GitLab deployment env vars or secrets
  4. Live cluster - falls back to a short-lived OAuth token for root

Usage:
  python3 migrate-to-gitlab.py --gitlab-url https://gitlab.apps.example.com
  python3 migrate-to-gitlab.py --gitlab-url https://gitlab.apps.example.com --dry-run

Options:
  --configmap  Path to the ConfigMap YAML file (default: repos-configmap.yaml)
  --group      GitLab group path to migrate repos into (overrides ConfigMap value)
  --dry-run    Print what would happen without making any changes.
"""

import argparse
import base64
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


def get_gitlab_url():
    result = subprocess.run(
        ["oc", "get", "route", "gitlab", "-n", "gitlab-system",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


def get_token_from_cluster(base_url):
    """Read an admin-capable GitLab API token from the cluster."""
    errors = []

    deployment_candidates = (
        ("gitlab", "gitlab-service"),
        ("gitlab-system", "gitlab-webservice-default"),
    )
    env_var_names = {"GITLAB_ADMIN_TOKEN", "GITLAB_TOKEN"}

    for namespace, deployment in deployment_candidates:
        try:
            result = subprocess.run(
                ["oc", "get", "deployment", deployment, "-n", namespace,
                 "-o", "jsonpath={.spec.template.spec.containers}"],
                capture_output=True, text=True, check=True,
            )
            containers = json.loads(result.stdout)
            for container in containers:
                for env in container.get("env", []):
                    if env.get("name") in env_var_names and env.get("value"):
                        return env["value"]
        except Exception as exc:
            errors.append(f"deployment {namespace}/{deployment}: {exc}")

    try:
        result = subprocess.run(
            ["oc", "get", "secret", "gitlab-gitlab-initial-root-password",
             "-n", "gitlab-system", "-o", "jsonpath={.data.password}"],
            capture_output=True, text=True, check=True,
        )
        password = base64.b64decode(result.stdout.strip()).decode()
        resp = requests.post(
            f"{base_url}/oauth/token",
            data={
                "grant_type": "password",
                "username": "root",
                "password": password,
                "scope": "api read_user read_repository write_repository",
            },
            verify=False,
        )
        resp.raise_for_status()
        access_token = resp.json().get("access_token")
        if access_token:
            return access_token
    except Exception as exc:
        errors.append(f"root oauth token: {exc}")

    secret_candidates = (
        ("gitlab", "gitlab-admin-token", ("token",)),
        ("gitlab-system", "gitlab-group-access-pat", ("GITLAB_TOKEN", "token")),
        ("backstage", "gitlab-token", ("GITLAB_TOKEN", "token")),
    )

    for namespace, secret, keys in secret_candidates:
        for key in keys:
            try:
                result = subprocess.run(
                    ["oc", "get", "secret", secret, "-n", namespace,
                     "-o", f"jsonpath={{.data.{key}}}"],
                    capture_output=True, text=True, check=True,
                )
                token = result.stdout.strip()
                if token:
                    return base64.b64decode(token).decode()
            except Exception as exc:
                errors.append(f"secret {namespace}/{secret} key {key}: {exc}")

    joined = "; ".join(errors)
    sys.exit(f"Failed to read token from cluster. Tried common deployments and secrets. Details: {joined}")


def load_configmap(path):
    with open(path) as f:
        cm = yaml.safe_load(f)
    return yaml.safe_load(cm["data"]["repos.yaml"])


def gl_headers(token):
    headers = {"Content-Type": "application/json"}
    if token.startswith("glpat-"):
        headers["PRIVATE-TOKEN"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def gl_get(base_url, token, path):
    return requests.get(f"{base_url}/api/v4{path}", headers=gl_headers(token), verify=False)


def gl_post(base_url, token, path, data):
    return requests.post(f"{base_url}/api/v4{path}", headers=gl_headers(token), json=data, verify=False)


def gl_delete(base_url, token, path):
    return requests.delete(f"{base_url}/api/v4{path}", headers=gl_headers(token), verify=False)


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


def get_project_id(base_url, token, group_path, name):
    encoded = quote(f"{group_path}/{name}", safe="")
    resp = gl_get(base_url, token, f"/projects/{encoded}")
    if resp.status_code == 200:
        return resp.json()["id"]
    return None


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
    return resp.json()["id"]


def ensure_branch_unprotected(base_url, token, project_id, branch="main"):
    """Remove default branch protection so workshop users can push directly."""
    encoded_branch = quote(branch, safe="")
    resp = gl_delete(base_url, token, f"/projects/{project_id}/protected_branches/{encoded_branch}")
    if resp.status_code not in (204, 404):
        raise RuntimeError(f"failed to unprotect {branch}: {resp.status_code} {resp.text}")


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
    parser.add_argument("--gitlab-url", default=None, help="GitLab base URL (default: read from cluster route)")
    parser.add_argument("--token",      default=os.environ.get("GITLAB_TOKEN"), help="GitLab admin personal access token (optional, reads from cluster if omitted)")
    parser.add_argument("--group",      default=None,                            help="GitLab group path (overrides ConfigMap)")
    parser.add_argument("--configmap",  default=os.path.join(SCRIPT_DIR, "repos-configmap.yaml"), help="Path to ConfigMap YAML")
    parser.add_argument("--dry-run",    action="store_true",                     help="Print actions without executing")
    args = parser.parse_args()

    if not os.path.exists(args.configmap):
        sys.exit(f"ConfigMap file not found: {args.configmap}")

    base_url = args.gitlab_url.rstrip("/") if args.gitlab_url else get_gitlab_url()

    token = args.token
    if not token:
        print("No token provided — reading from cluster...")
        token = get_token_from_cluster(base_url)
        if not token:
            sys.exit("Could not read token from cluster.")
        print("  token: (loaded from cluster)\n")

    config     = load_configmap(args.configmap)
    group_path = args.group or config.get("gitea_org", "developers")
    github_org = config.get("github_org", "na-launch-workshop")
    repos      = config.get("repos", [])

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

        github_url = f"https://github.com/{github_org}/{name}.git"

        if args.dry_run:
            print(f"   WOULD clone {github_url}")
            print(f"   WOULD push  {base_url}/{group_path}/{name}.git\n")
            continue

        try:
            existing_project_id = get_project_id(base_url, token, group_path, name)
            if existing_project_id:
                ensure_branch_unprotected(base_url, token, existing_project_id)
                print(f"   SKIP: already exists\n")
                continue

            project_id = create_project(base_url, token, group_id, name)
            ensure_branch_unprotected(base_url, token, project_id)
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
