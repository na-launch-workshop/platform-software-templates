#!/usr/bin/env python3
"""
Provisions per-user workshop repos in Gitea.

For every user in the Keycloak 'developers' group:
  - Copies each source repo from the Gitea 'developers' org
    into the user's personal Gitea namespace (clean copy, not fork)
  - Upserts a per-user catalog-info.yaml into the repo

Idempotent: skips repos that already exist for a user.
Run register-rhdh-catalog.py afterwards to register repos in the RHDH catalog.

Credentials are read automatically from the cluster (requires oc login).

Usage:
  python3 provision-user-repos.py --gitea-url https://gitea.apps.example.com
  python3 provision-user-repos.py --gitea-url https://gitea.apps.example.com --dry-run
  python3 provision-user-repos.py --gitea-url https://gitea.apps.example.com --user devben
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import requests
import urllib3
import yaml

urllib3.disable_warnings()

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
KEYCLOAK_REALM = "workshop"
KEYCLOAK_GROUP = "developers"
KEYCLOAK_SKIP  = {"admin"}

CATALOG_INFO_TEMPLATE = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: {entity_name}
  title: "{title}"
  description: "Quarkus workshop - {title}"
  annotations:
    backstage.io/techdocs-ref: dir:.
    backstage.io/source-location: url:{repo_url}
  links:
    - url: {devspaces_url}/#{repo_url}
      title: Open in Dev Spaces
      icon: web
spec:
  type: workshop-lab
  lifecycle: workshop
  owner: user:default/{username}
"""


# ---------------------------------------------------------------------------
# Cluster credential helpers
# ---------------------------------------------------------------------------

def get_gitea_credentials():
    """Read admin username and password from the gitea-service deployment."""
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
        sys.exit(f"Failed to read Gitea credentials from cluster: {exc}")


def get_keycloak_admin_password():
    """Read Keycloak admin password from the keycloak-bootstrap-admin secret."""
    result = subprocess.run(
        ["oc", "get", "secret", "credential-keycloak", "-n", "keycloak",
         "-o", "jsonpath={.data.ADMIN_PASSWORD}"],
        capture_output=True, text=True, check=True,
    )
    return base64.b64decode(result.stdout.strip()).decode()


def get_keycloak_url():
    """Read Keycloak route from the cluster."""
    result = subprocess.run(
        ["oc", "get", "route", "keycloak", "-n", "keycloak",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


def get_gitea_route():
    """Read Gitea route hostname from the cluster."""
    result = subprocess.run(
        ["oc", "get", "route", "gitea-http", "-n", "gitea",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def get_devspaces_url():
    """Read Dev Spaces URL from the CheCluster status."""
    result = subprocess.run(
        ["oc", "get", "checluster", "devspaces", "-n", "openshift-operators",
         "-o", "jsonpath={.status.cheURL}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Keycloak helpers
# ---------------------------------------------------------------------------

def get_keycloak_token(keycloak_url, password):
    resp = requests.post(
        f"{keycloak_url}/auth/realms/master/protocol/openid-connect/token",
        data={
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": "admin",
            "password": password,
        },
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_developers(keycloak_url, kc_token):
    """Return list of usernames in the Keycloak developers group."""
    resp = requests.get(
        f"{keycloak_url}/auth/admin/realms/{KEYCLOAK_REALM}/groups",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    resp.raise_for_status()
    group_id = next(
        (g["id"] for g in resp.json() if g["name"] == KEYCLOAK_GROUP), None
    )
    if not group_id:
        sys.exit(f"Keycloak group '{KEYCLOAK_GROUP}' not found")

    resp = requests.get(
        f"{keycloak_url}/auth/admin/realms/{KEYCLOAK_REALM}/groups/{group_id}/members?max=200",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    resp.raise_for_status()
    return [m["username"] for m in resp.json() if m["username"] not in KEYCLOAK_SKIP]


# ---------------------------------------------------------------------------
# Gitea API helpers
# ---------------------------------------------------------------------------

def gitea_get(base_url, auth, path):
    return requests.get(f"{base_url}/api/v1{path}", auth=auth, verify=False)


def gitea_post(base_url, auth, path, data):
    return requests.post(f"{base_url}/api/v1{path}", auth=auth, json=data, verify=False)


def gitea_put(base_url, auth, path, data):
    return requests.put(f"{base_url}/api/v1{path}", auth=auth, json=data, verify=False)


def user_exists_in_gitea(base_url, auth, username):
    return gitea_get(base_url, auth, f"/users/{username}").status_code == 200


def repo_exists(base_url, auth, owner, name):
    return gitea_get(base_url, auth, f"/repos/{owner}/{name}").status_code == 200


def create_user_repo(base_url, auth, username, repo_name):
    """Create a repo in a user's namespace via the admin API."""
    resp = gitea_post(base_url, auth, f"/admin/users/{username}/repos", {
        "name": repo_name,
        "private": False,
        "auto_init": False,
        "default_branch": "main",
    })
    if resp.status_code != 201:
        raise RuntimeError(f"{resp.status_code} {resp.text}")


def upsert_catalog_info(base_url, auth, username, repo_name, content):
    """Create or update catalog-info.yaml in a user repo via the Gitea API."""
    path = f"/repos/{username}/{repo_name}/contents/catalog-info.yaml"
    encoded = base64.b64encode(content.encode()).decode()

    # Always try POST first; fall back to PUT if file already exists (422)
    resp = gitea_post(base_url, auth, path, {
        "message": "Add catalog-info.yaml",
        "content": encoded,
    })

    if resp.status_code == 422:
        for attempt in range(5):
            time.sleep(2)
            get_resp = gitea_get(base_url, auth, path)
            try:
                sha = get_resp.json()["sha"]
                break
            except Exception:
                if attempt == 4:
                    raise RuntimeError(f"Could not get SHA after 5 attempts: {get_resp.text[:200]}")
        resp = gitea_put(base_url, auth, path, {
            "message": "Update catalog-info.yaml",
            "content": encoded,
            "sha": sha,
        })

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Failed to upsert catalog-info.yaml: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------

def clone_and_push(source_url, dest_url):
    tmpdir = tempfile.mkdtemp()
    try:
        bare = os.path.join(tmpdir, "repo.git")
        subprocess.run(["git", "clone", "--mirror", source_url, bare], check=True)
        subprocess.run(["git", "push", "--mirror", dest_url], cwd=bare, check=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# ConfigMap loader
# ---------------------------------------------------------------------------

def load_configmap(path):
    with open(path) as f:
        cm = yaml.safe_load(f)
    return yaml.safe_load(cm["data"]["repos.yaml"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gitea-url", default=os.environ.get("GITEA_URL"), help="Gitea base URL")
    parser.add_argument("--configmap", default=os.path.join(SCRIPT_DIR, "repos-configmap.yaml"), help="Path to ConfigMap YAML")
    parser.add_argument("--user",      help="Provision a single user only")
    parser.add_argument("--dry-run",   action="store_true", help="Print actions without executing")
    args = parser.parse_args()

    if not args.gitea_url:
        sys.exit("--gitea-url or GITEA_URL is required")
    if not os.path.exists(args.configmap):
        sys.exit(f"ConfigMap file not found: {args.configmap}")

    base_url = args.gitea_url.rstrip("/")

    # Load repos from ConfigMap
    config     = load_configmap(args.configmap)
    source_org = config.get("gitea_org", "developers")
    repos      = config.get("repos", [])

    if not repos:
        sys.exit("No repos found in ConfigMap.")

    # Fetch Gitea credentials from cluster
    print("Reading Gitea credentials from cluster...")
    gitea_user, gitea_pass = get_gitea_credentials()
    auth = (gitea_user, gitea_pass)
    print(f"  Gitea user: {gitea_user}")

    devspaces_url = get_devspaces_url()

    push_base = base_url.replace("https://", f"https://{gitea_user}:{gitea_pass}@") \
                        .replace("http://",  f"http://{gitea_user}:{gitea_pass}@")

    # Fetch Keycloak users
    print("\nReading Keycloak users...")
    kc_url   = get_keycloak_url()
    kc_pass  = get_keycloak_admin_password()
    kc_token = get_keycloak_token(kc_url, kc_pass)

    if args.user:
        users = [args.user]
    else:
        users = get_developers(kc_url, kc_token)

    print(f"  Users: {users}\n")

    # Provision
    errors = []
    for username in users:
        print(f"── {username}")

        if not user_exists_in_gitea(base_url, auth, username):
            print(f"   SKIP: '{username}' not found in Gitea\n")
            continue

        for repo in repos:
            name  = repo.get("name")
            title = repo.get("title", name)

            if repo_exists(base_url, auth, username, name):
                print(f"   SKIP {name} (already exists, updating catalog-info)")
                if not args.dry_run:
                    catalog_info = CATALOG_INFO_TEMPLATE.format(
                        entity_name=f"{username}-{name}",
                        title=title,
                        username=username,
                        repo_url=f"{base_url}/{username}/{name}",
                        devspaces_url=devspaces_url,
                    )
                    try:
                        upsert_catalog_info(base_url, auth, username, name, catalog_info)
                    except Exception as exc:
                        print(f"   ERR updating catalog-info: {exc}")
                        errors.append(f"{username}/{name}")
                continue

            if args.dry_run:
                print(f"   WOULD copy {source_org}/{name} → {username}/{name}")
                print(f"   WOULD register {catalog_url}")
                continue

            source_url = f"{push_base}/{source_org}/{name}.git"
            dest_url   = f"{push_base}/{username}/{name}.git"

            try:
                create_user_repo(base_url, auth, username, name)
                clone_and_push(source_url, dest_url)

                catalog_info = CATALOG_INFO_TEMPLATE.format(
                    entity_name=f"{username}-{name}",
                    title=title,
                    username=username,
                    repo_url=f"{base_url}/{username}/{name}",
                    devspaces_url=devspaces_url,
                )
                upsert_catalog_info(base_url, auth, username, name, catalog_info)

                print(f"   OK  {name}")
            except Exception as exc:
                print(f"   ERR {name}: {exc}")
                errors.append(f"{username}/{name}")

        print()

    if errors:
        print(f"Failed: {errors}")
        sys.exit(1)
    else:
        print("Done. Run register-rhdh-catalog.py to register repos in RHDH.")


if __name__ == "__main__":
    main()
