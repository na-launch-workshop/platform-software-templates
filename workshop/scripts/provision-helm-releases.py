#!/usr/bin/env python3
"""
Provisions the workshop helm release in each user Dev Spaces namespace.

For every user in the Keycloak 'developers' group:
  - Ensures the <user>-devspaces namespace exists
  - Runs ``helm upgrade --install workshop-deployer`` against the chart root

This script is idempotent and supports a manual fast path via repeatable
``--user`` arguments.

Credentials and cluster endpoints are read automatically (requires oc login).

Usage:
  python3 provision-helm-releases.py
  python3 provision-helm-releases.py --user devben
  python3 provision-helm-releases.py --user devben --user jane
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

import requests
import urllib3

urllib3.disable_warnings()

KEYCLOAK_REALM = "openshift"
KEYCLOAK_GROUP = "developers"
KEYCLOAK_SKIP = {"admin"}
HELM_RELEASE = "workshop-deployer"
CHART_REPO = "https://github.com/na-launch-workshop/workshop_resources.git"
CHART_ROOT = "/tmp/chart-source"


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


def get_keycloak_admin_password():
    result = subprocess.run(
        ["oc", "get", "secret", "credential-keycloak", "-n", "keycloak",
         "-o", "jsonpath={.data.ADMIN_PASSWORD}"],
        capture_output=True, text=True, check=True,
    )
    return base64.b64decode(result.stdout.strip()).decode()


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
    resp = requests.get(
        f"{keycloak_url}/auth/admin/realms/{KEYCLOAK_REALM}/groups",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    resp.raise_for_status()
    group_id = next((g["id"] for g in resp.json() if g["name"] == KEYCLOAK_GROUP), None)
    if not group_id:
        sys.exit(f"Keycloak group '{KEYCLOAK_GROUP}' not found")

    resp = requests.get(
        f"{keycloak_url}/auth/admin/realms/{KEYCLOAK_REALM}/groups/{group_id}/members?max=200",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    resp.raise_for_status()
    return [
        m["username"] for m in resp.json()
        if m["username"] not in KEYCLOAK_SKIP
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
# OpenShift / Helm helpers
# ---------------------------------------------------------------------------

def namespace_exists(namespace):
    result = subprocess.run(
        ["oc", "get", "namespace", namespace],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def ensure_namespace(namespace, dry_run=False):
    if namespace_exists(namespace):
        return False
    if dry_run:
        print(f"   WOULD create namespace {namespace}")
        return True
    subprocess.run(["oc", "create", "namespace", namespace], check=True)
    return True


def ensure_helm_available():
    if shutil.which("helm"):
        return
    sys.exit("helm not found in PATH")


def clone_chart():
    if os.path.isdir(CHART_ROOT):
        shutil.rmtree(CHART_ROOT, ignore_errors=True)
    subprocess.run(["git", "clone", "--depth", "1", CHART_REPO, CHART_ROOT], check=True)


def helm_upgrade(namespace, dry_run=False):
    ensure_helm_available()
    if dry_run:
        print(f"   WOULD helm upgrade --install {HELM_RELEASE} {CHART_ROOT} -n {namespace}")
        return
    subprocess.run(
        ["helm", "upgrade", "--install", HELM_RELEASE, CHART_ROOT, "-n", namespace],
        check=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", action="append", help="Provision a single user only (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    args = parser.parse_args()

    print("Reading cluster credentials...")
    kc_url = get_keycloak_url()
    kc_pass = get_keycloak_admin_password()
    kc_token = get_keycloak_token(kc_url, kc_pass)

    if args.user:
        users = []
        for username in args.user:
            kc_user = get_keycloak_user(kc_url, kc_token, username)
            if not kc_user:
                sys.exit(f"Keycloak user '{username}' not found")
            users.append(kc_user["username"])
    else:
        users = get_developers(kc_url, kc_token)

    print(f"  Users: {users}\n")

    clone_chart()

    errors = []
    for username in users:
        namespace = f"{username}-devspaces"
        print(f"── {username}")

        try:
            ensure_namespace(namespace, dry_run=args.dry_run)
            helm_upgrade(namespace, dry_run=args.dry_run)
            print(f"   OK  {namespace}")
        except Exception as exc:
            print(f"   ERR {namespace}: {exc}")
            errors.append(username)

        print()

    if errors:
        print(f"Failed: {errors}")
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
