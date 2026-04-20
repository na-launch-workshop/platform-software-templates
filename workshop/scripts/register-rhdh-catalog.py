#!/usr/bin/env python3
"""
Registers per-user workshop repos in the RHDH catalog via postgres.

For every user in the Keycloak 'developers' group (or a single --user):
  - Inserts each user's catalog-info.yaml URL into the RHDH postgres catalog

Idempotent: skips URLs already registered.

Credentials and cluster endpoints are read automatically (requires oc login).

Usage:
  python3 register-rhdh-catalog.py --gitea-url https://gitea.apps.example.com
  python3 register-rhdh-catalog.py --gitea-url https://gitea.apps.example.com --user devben
  python3 register-rhdh-catalog.py --gitea-url https://gitea.apps.example.com --dry-run
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import uuid

import requests
import urllib3
import yaml

urllib3.disable_warnings()

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
KEYCLOAK_REALM = "openshift"
KEYCLOAK_GROUP = "developers"
KEYCLOAK_SKIP  = {"admin"}


# ---------------------------------------------------------------------------
# Cluster helpers
# ---------------------------------------------------------------------------

def get_keycloak_admin_password():
    result = subprocess.run(
        ["oc", "get", "secret", "credential-keycloak", "-n", "keycloak",
         "-o", "jsonpath={.data.ADMIN_PASSWORD}"],
        capture_output=True, text=True, check=True,
    )
    return base64.b64decode(result.stdout.strip()).decode()


def get_keycloak_url():
    result = subprocess.run(
        ["oc", "get", "route", "keycloak", "-n", "keycloak",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


def get_gitea_route():
    result = subprocess.run(
        ["oc", "get", "route", "gitea-http", "-n", "gitea",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Keycloak helpers
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
    return [m["username"] for m in resp.json() if m["username"] not in KEYCLOAK_SKIP]


# ---------------------------------------------------------------------------
# Postgres / RHDH helpers
# ---------------------------------------------------------------------------

def run_psql(sql):
    result = subprocess.run(
        ["oc", "exec", "-i", "-n", "backstage", "deployment/postgres", "--",
         "psql", "-U", "postgres", "-d", "backstage_plugin_catalog"],
        input=sql, capture_output=True, text=True, check=True,
    )
    return result.stdout


def register_in_rhdh(catalog_url):
    location_key = f"url:{catalog_url}"
    name         = "generated-" + hashlib.sha1(location_key.encode()).hexdigest()
    entity_ref   = f"location:default/{name}"
    entity_id    = str(uuid.uuid4())

    unprocessed = json.dumps({
        "apiVersion": "backstage.io/v1alpha1",
        "kind": "Location",
        "metadata": {
            "name": name,
            "annotations": {
                "backstage.io/managed-by-location":        location_key,
                "backstage.io/managed-by-origin-location": location_key,
            },
        },
        "spec": {"type": "url", "target": catalog_url},
    }).replace("'", "''")

    run_psql(f"""
        BEGIN;
        INSERT INTO locations (id, type, target)
          VALUES ('{entity_id}', 'url', '{catalog_url}')
          ON CONFLICT DO NOTHING;
        INSERT INTO refresh_state
          (entity_id, entity_ref, unprocessed_entity, errors, next_update_at, last_discovery_at, location_key)
          VALUES ('{entity_id}', '{entity_ref}', '{unprocessed}', '', NOW(), NOW(), '{location_key}')
          ON CONFLICT DO NOTHING;
        INSERT INTO refresh_state_references (source_entity_ref, target_entity_ref)
          VALUES (NULL, '{entity_ref}')
          ON CONFLICT DO NOTHING;
        COMMIT;
    """)


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
    parser.add_argument("--user",      help="Register a single user only")
    parser.add_argument("--dry-run",   action="store_true", help="Print actions without executing")
    args = parser.parse_args()

    if not args.gitea_url:
        sys.exit("--gitea-url or GITEA_URL is required")
    if not os.path.exists(args.configmap):
        sys.exit(f"ConfigMap file not found: {args.configmap}")

    config = load_configmap(args.configmap)
    repos  = config.get("repos", [])
    if not repos:
        sys.exit("No repos found in ConfigMap.")

    gitea_host = get_gitea_route()

    print("Reading Keycloak users...")
    kc_url   = get_keycloak_url()
    kc_pass  = get_keycloak_admin_password()
    kc_token = get_keycloak_token(kc_url, kc_pass)
    users    = [args.user] if args.user else get_developers(kc_url, kc_token)
    print(f"  Users: {users}\n")

    errors = []
    for username in users:
        print(f"── {username}")
        for repo in repos:
            name        = repo.get("name")
            catalog_url = f"https://{gitea_host}/{username}/{name}/raw/branch/main/catalog-info.yaml"

            if args.dry_run:
                print(f"   WOULD register {catalog_url}")
                continue

            try:
                register_in_rhdh(catalog_url)
                print(f"   OK  {name}")
            except Exception as exc:
                print(f"   ERR {name}: {exc}")
                errors.append(f"{username}/{name}")
        print()

    if errors:
        print(f"Failed: {errors}")
        sys.exit(1)
    else:
        print("Done. RHDH catalog will refresh within ~2 minutes.")


if __name__ == "__main__":
    main()
