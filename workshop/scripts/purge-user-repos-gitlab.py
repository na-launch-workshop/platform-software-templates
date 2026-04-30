#!/usr/bin/env python3
"""
Permanently purges all personal GitLab repos for every user in the Keycloak 'developers' group.

For every user:
  - Lists all projects in their personal GitLab namespace
  - Permanently deletes each one (no soft-delete / recovery period)

Credential resolution order:
  1. CLI args (--token)
  2. Environment variable (GITLAB_TOKEN)
  3. Live cluster - reads token from cluster secrets/deployments

Usage:
  python3 purge-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com
  python3 purge-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com --dry-run
  python3 purge-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com --user devben
"""

import argparse
import base64
import os
import subprocess
import sys
from urllib.parse import quote

import requests
import urllib3

from workshop_gitlab import get_gitlab_token_from_cluster

urllib3.disable_warnings()

KEYCLOAK_REALM = "openshift"
KEYCLOAK_GROUP = "developers"
KEYCLOAK_SKIP  = {"admin"}


# ---------------------------------------------------------------------------
# Keycloak helpers
# ---------------------------------------------------------------------------

def get_keycloak_url():
    result = subprocess.run(
        ["oc", "get", "route", "keycloak", "-n", "keycloak",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


def get_keycloak_admin_password():
    result = subprocess.run(
        ["oc", "get", "secret", "credential-keycloak", "-n", "keycloak",
         "-o", "jsonpath={.data.ADMIN_PASSWORD}"],
        capture_output=True, text=True, check=True,
    )
    return base64.b64decode(result.stdout.strip()).decode()


def get_gitlab_url():
    result = subprocess.run(
        ["oc", "get", "route", "gitlab", "-n", "gitlab-system",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


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
    return [
        m["username"] for m in resp.json() if m["username"] not in KEYCLOAK_SKIP
    ]


def get_keycloak_user(keycloak_url, kc_token, username):
    resp = requests.get(
        f"{keycloak_url}/auth/admin/realms/{KEYCLOAK_REALM}/users?username={username}",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    resp.raise_for_status()
    for user in resp.json():
        if user.get("username") == username:
            return user
    return None


# ---------------------------------------------------------------------------
# GitLab helpers
# ---------------------------------------------------------------------------

def gl_headers(token):
    headers = {"Content-Type": "application/json"}
    if token.startswith("glpat-"):
        headers["PRIVATE-TOKEN"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_gitlab_user_id(base_url, token, username):
    resp = requests.get(
        f"{base_url}/api/v4/users?username={username}",
        headers=gl_headers(token),
        verify=False,
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]["id"]
    return None


def list_user_projects(base_url, token, user_id):
    projects = []
    page = 1
    while True:
        resp = requests.get(
            f"{base_url}/api/v4/users/{user_id}/projects",
            headers=gl_headers(token),
            params={"page": page, "per_page": 100},
            verify=False,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        projects.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return projects


def permanently_delete_project(base_url, token, project_id, full_path):
    # Step 1: soft delete (marks for deletion)
    resp = requests.delete(
        f"{base_url}/api/v4/projects/{project_id}",
        headers=gl_headers(token),
        verify=False,
    )
    if resp.status_code not in (202, 204):
        return resp.status_code, resp.text

    # Step 2: permanent delete
    encoded_path = quote(full_path, safe="")
    # The project path gets renamed with a deletion suffix after step 1
    # so we need to look it up again by id
    lookup = requests.get(
        f"{base_url}/api/v4/projects/{project_id}",
        headers=gl_headers(token),
        verify=False,
    )
    if lookup.status_code == 200:
        actual_path = lookup.json().get("path_with_namespace", full_path)
        encoded_path = quote(actual_path, safe="")

    resp = requests.delete(
        f"{base_url}/api/v4/projects/{project_id}?permanently_remove=true&full_path={encoded_path}",
        headers=gl_headers(token),
        verify=False,
    )
    return resp.status_code, resp.text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gitlab-url", default=None, help="GitLab base URL (default: read from cluster route)")
    parser.add_argument("--token",      default=os.environ.get("GITLAB_TOKEN"), help="GitLab admin token (optional, reads from cluster if omitted)")
    parser.add_argument("--user",       help="Purge a single user only")
    parser.add_argument("--dry-run",    action="store_true", help="Print actions without executing")
    args = parser.parse_args()

    base_url = args.gitlab_url.rstrip("/") if args.gitlab_url else get_gitlab_url()

    token = args.token
    if not token:
        print("No token provided — reading from cluster...")
        token = get_gitlab_token_from_cluster(base_url)

    print("Reading Keycloak users...")
    kc_url   = get_keycloak_url()
    kc_pass  = get_keycloak_admin_password()
    kc_token = get_keycloak_token(kc_url, kc_pass)

    if args.user:
        kc_user = get_keycloak_user(kc_url, kc_token, args.user)
        if not kc_user:
            sys.exit(f"Keycloak user '{args.user}' not found")
        users = [kc_user["username"]]
    else:
        users = get_developers(kc_url, kc_token)

    print(f"  Users: {users}\n")
    print(f"GitLab: {base_url}")
    print(f"Mode:   {'DRY RUN' if args.dry_run else 'LIVE — permanent delete'}\n")

    deleted = []
    errors  = []

    for username in users:
        print(f"── {username}")
        user_id = get_gitlab_user_id(base_url, token, username)
        if not user_id:
            print(f"   WARN: not found in GitLab, skipping")
            continue

        projects = list_user_projects(base_url, token, user_id)
        if not projects:
            print(f"   (no repos)")
            continue

        for p in projects:
            full_path = p["path_with_namespace"]
            if args.dry_run:
                print(f"   WOULD delete {full_path}")
                deleted.append(full_path)
                continue

            status, body = permanently_delete_project(base_url, token, p["id"], full_path)
            if status == 202:
                print(f"   OK  {full_path}")
                deleted.append(full_path)
            else:
                print(f"   ERR {full_path}: HTTP {status} — {body}")
                errors.append(full_path)
        print()

    print("Summary")
    print(f"  {'Would delete' if args.dry_run else 'Deleted'} ({len(deleted)}): {deleted}")
    if errors:
        print(f"  Failed ({len(errors)}): {errors}")
        sys.exit(1)
    print("Done.")


if __name__ == "__main__":
    main()
