#!/usr/bin/env python3
"""
Purges workshop users from Keycloak and GitLab.

Credentials and cluster endpoints are read automatically (requires oc login).

Usage:
  python3 delete-workshop-user.py --user ken
  python3 delete-workshop-user.py --user ken --user helen
  python3 delete-workshop-user.py --user ken --dry-run
  python3 delete-workshop-user.py --gitlab-url https://gitlab.apps.example.com --user ken

Options:
  --user        Username to delete (repeatable for multiple users)
  --gitlab-url  Override GitLab URL (default: read from cluster route)
  --dry-run     Print what would be deleted without making any changes
"""

import argparse
import base64
import os
import subprocess
import sys

import requests
import urllib3

urllib3.disable_warnings()

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
KEYCLOAK_REALM = "openshift"


# ---------------------------------------------------------------------------
# Cluster helpers
# ---------------------------------------------------------------------------

def get_keycloak_url():
    result = subprocess.run(
        ["oc", "get", "route", "keycloak", "-n", "keycloak",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


def get_gitlab_url():
    result = subprocess.run(
        ["oc", "get", "route", "gitlab", "-n", "gitlab-system",
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


def get_gitlab_token(base_url):
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
            "scope": "api",
        },
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Keycloak
# ---------------------------------------------------------------------------

def get_keycloak_token(keycloak_url, password):
    resp = requests.post(
        f"{keycloak_url}/auth/realms/master/protocol/openid-connect/token",
        data={"client_id": "admin-cli", "grant_type": "password",
              "username": "admin", "password": password},
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def find_keycloak_user(keycloak_url, kc_token, username):
    resp = requests.get(
        f"{keycloak_url}/auth/admin/realms/{KEYCLOAK_REALM}/users?username={username}",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    resp.raise_for_status()
    for user in resp.json():
        if user["username"] == username:
            return user
    return None


def delete_keycloak_user(keycloak_url, kc_token, user_id, dry_run):
    if dry_run:
        print(f"   WOULD delete Keycloak user id={user_id}")
        return
    resp = requests.delete(
        f"{keycloak_url}/auth/admin/realms/{KEYCLOAK_REALM}/users/{user_id}",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    if resp.status_code == 204:
        print("   Keycloak: deleted")
    else:
        raise RuntimeError(f"Keycloak delete failed: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------

def find_gitlab_user(base_url, gl_token, username):
    resp = requests.get(
        f"{base_url}/api/v4/users?username={username}",
        headers={"Authorization": f"Bearer {gl_token}"},
        verify=False,
    )
    resp.raise_for_status()
    users = resp.json()
    return users[0] if users else None


def delete_gitlab_user(base_url, gl_token, user_id, dry_run):
    if dry_run:
        print(f"   WOULD hard-delete GitLab user id={user_id} (all repos included)")
        return
    resp = requests.delete(
        f"{base_url}/api/v4/users/{user_id}?hard_delete=true",
        headers={"Authorization": f"Bearer {gl_token}"},
        verify=False,
    )
    if resp.status_code == 204:
        print("   GitLab: hard-deleted (repos removed)")
    else:
        raise RuntimeError(f"GitLab delete failed: {resp.status_code} {resp.text}")




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gitlab-url", default=None,      help="GitLab base URL (default: read from cluster route)")
    parser.add_argument("--user",       action="append", required=True, help="Username to delete (repeatable)")
    parser.add_argument("--dry-run",    action="store_true",            help="Print actions without executing")
    args = parser.parse_args()

    if args.gitlab_url:
        base_url = args.gitlab_url.rstrip("/")
    else:
        base_url = get_gitlab_url()

    print("Reading cluster credentials...")
    kc_url   = get_keycloak_url()
    kc_pass  = get_keycloak_admin_password()
    kc_token = get_keycloak_token(kc_url, kc_pass)
    gl_token = get_gitlab_token(base_url)
    print("  credentials loaded\n")

    errors = []
    for username in args.user:
        print(f"── {username}")

        # Keycloak
        kc_user = find_keycloak_user(kc_url, kc_token, username)
        if kc_user:
            try:
                delete_keycloak_user(kc_url, kc_token, kc_user["id"], args.dry_run)
            except Exception as exc:
                print(f"   ERR Keycloak: {exc}")
                errors.append(f"{username}/keycloak")
        else:
            print(f"   Keycloak: '{username}' not found — skipping")

        # GitLab
        gl_user = find_gitlab_user(base_url, gl_token, username)
        if gl_user:
            try:
                delete_gitlab_user(base_url, gl_token, gl_user["id"], args.dry_run)
            except Exception as exc:
                print(f"   ERR GitLab: {exc}")
                errors.append(f"{username}/gitlab")
        else:
            print(f"   GitLab: '{username}' not found — skipping")

        print()

    if errors:
        print(f"Failed: {errors}")
        sys.exit(1)
    else:
        print("Done.")


if __name__ == "__main__":
    main()
