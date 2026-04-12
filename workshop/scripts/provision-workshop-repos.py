#!/usr/bin/env python3
"""
Workshop repo provisioner

For every user in the Keycloak 'developers' group:
  - Copies each of the 6 source repos from GitLab 'developers' group
    into the user's personal GitLab namespace (clean copy, not fork)
  - Adds a catalog-info.yaml to each repo
  - Registers each repo in the RHDH catalog

Idempotent: skips repos that already exist for a user.

Usage:
  python3 provision-workshop-repos.py
  python3 provision-workshop-repos.py --dry-run
  python3 provision-workshop-repos.py --user devben
"""

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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITLAB_URL   = "https://gitlab.apps.rosa.rosa-bvwwp.naah.p3.openshiftapps.com"
KEYCLOAK_URL = "https://keycloak-keycloak.apps.rosa.rosa-bvwwp.naah.p3.openshiftapps.com/auth"
DEVSPACES_URL = "https://devspaces.apps.rosa.rosa-bvwwp.naah.p3.openshiftapps.com"

KEYCLOAK_REALM        = "openshift"
KEYCLOAK_DEVELOPERS_GROUP = "developers"
KEYCLOAK_SKIP_USERS   = {"admin"}

SOURCE_GROUP = "developers"

REPOS = [
    ("workshop-quarkus-hello_quarkus",                        "Module 1 - Hello Quarkus"),
    ("workshop-quarkus-using_camel",                          "Module 2 - Using Camel"),
    ("workshop-quarkus-using_funqy_stateless_events",         "Module 3 - Using Funqy Stateless Events"),
    ("workshop-quarkus-blue_green_deployments_openshift",     "Module 4 - Blue Green Deployments OpenShift"),
    ("workshop-quarkus-openshift_telemetry_with_micrometer",  "Module 5 - OpenShift Telemetry With Micrometer"),
    ("workshop-quarkus-interacting_with_kafka",               "Module 6 - Interacting With Kafka"),
]

CATALOG_INFO_TEMPLATE = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: {entity_name}
  title: "{title}"
  description: "Quarkus workshop - {title}"
  links:
    - url: "{devspaces_url}"
      title: Open in DevSpaces
      icon: launch
  annotations:
    gitlab.com/project-slug: {username}/{repo_name}
spec:
  type: workshop-lab
  lifecycle: workshop
  owner: user:default/{username}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_gitlab_token():
    result = subprocess.run(
        ["oc", "exec", "-n", "backstage", "deployment/backstage-developer-hub",
         "--", "env"],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        if line.startswith("GITLAB_TOKEN="):
            return line.split("=", 1)[1]
    sys.exit("Could not retrieve GITLAB_TOKEN from RHDH pod")


def get_keycloak_token(kc_pass):
    resp = requests.post(
        f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token",
        data={
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": "admin",
            "password": kc_pass,
        },
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_keycloak_admin_password():
    result = subprocess.run(
        ["oc", "get", "secret", "-n", "keycloak", "credential-keycloak",
         "-o", "jsonpath={.data.ADMIN_PASSWORD}"],
        capture_output=True, text=True
    )
    return base64.b64decode(result.stdout.strip()).decode()


def get_developers(kc_token):
    # Find developers group
    resp = requests.get(
        f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/groups",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    resp.raise_for_status()
    group_id = next(
        (g["id"] for g in resp.json() if g["name"] == KEYCLOAK_DEVELOPERS_GROUP),
        None,
    )
    if not group_id:
        sys.exit(f"Keycloak group '{KEYCLOAK_DEVELOPERS_GROUP}' not found")

    resp = requests.get(
        f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/groups/{group_id}/members?max=200",
        headers={"Authorization": f"Bearer {kc_token}"},
        verify=False,
    )
    resp.raise_for_status()
    return [
        m["username"] for m in resp.json()
        if m["username"] not in KEYCLOAK_SKIP_USERS
    ]


def gitlab_get(path, gl_token):
    resp = requests.get(
        f"{GITLAB_URL}/api/v4{path}",
        headers={"PRIVATE-TOKEN": gl_token},
        verify=False,
    )
    return resp


def gitlab_post(path, gl_token, data):
    resp = requests.post(
        f"{GITLAB_URL}/api/v4{path}",
        headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
        json=data,
        verify=False,
    )
    return resp


def get_gitlab_user_namespace(username, gl_token):
    resp = gitlab_get(f"/users?username={username}", gl_token)
    users = resp.json()
    if not users:
        return None, None
    return users[0]["id"], users[0]["namespace_id"]


def repo_exists(username, repo_name, gl_token):
    path = f"{username}/{repo_name}"
    resp = gitlab_get(f"/projects/{requests.utils.quote(path, safe='')}", gl_token)
    if resp.status_code != 200:
        return False
    project = resp.json()
    # Treat soft-deleted (pending deletion) repos as non-existent
    if project.get("marked_for_deletion_at") or project.get("pending_delete"):
        return False
    return True


def create_repo(namespace_id, repo_name, gl_token):
    resp = gitlab_post("/projects", gl_token, {
        "name": repo_name,
        "namespace_id": namespace_id,
        "visibility": "public",
        "default_branch": "main",
        "initialize_with_readme": False,
    })
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create {repo_name}: {resp.text}")
    return resp.json()["id"]


def copy_repo(source_url, dest_url):
    tmpdir = tempfile.mkdtemp()
    try:
        subprocess.run(
            ["git", "clone", "--mirror", source_url, "repo.git"],
            cwd=tmpdir, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "push", "--mirror", dest_url],
            cwd=os.path.join(tmpdir, "repo.git"), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def add_catalog_info(project_id, username, repo_name, title, gl_token):
    entity_name = f"{username}-{repo_name}"
    devspaces_url = (
        f"{DEVSPACES_URL}/#"
        f"{GITLAB_URL}/{username}/{repo_name}"
    )
    content = CATALOG_INFO_TEMPLATE.format(
        entity_name=entity_name,
        title=title,
        devspaces_url=devspaces_url,
        username=username,
        repo_name=repo_name,
    )
    encoded = base64.b64encode(content.encode()).decode()

    # Check if file already exists
    resp = gitlab_get(
        f"/projects/{project_id}/repository/files/catalog-info.yaml?ref=main",
        gl_token,
    )
    if resp.status_code == 200:
        # Update
        requests.put(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/files/catalog-info.yaml",
            headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
            json={
                "branch": "main",
                "content": content,
                "encoding": "text",
                "commit_message": "Add catalog-info.yaml",
            },
            verify=False,
        )
    else:
        # Create
        requests.post(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/files/catalog-info.yaml",
            headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
            json={
                "branch": "main",
                "content": content,
                "encoding": "text",
                "commit_message": "Add catalog-info.yaml",
            },
            verify=False,
        )


def register_in_rhdh(username, repo_name, catalog_url):
    """Insert location into RHDH postgres catalog DB via oc exec."""
    sql = f"""
INSERT INTO locations (type, target)
VALUES ('url', '{catalog_url}')
ON CONFLICT (target) DO NOTHING;
"""
    subprocess.run(
        ["oc", "exec", "-n", "backstage", "deployment/postgres",
         "--", "psql", "-U", "postgres", "-d", "backstage_plugin_catalog", "-c", sql],
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen, do nothing")
    parser.add_argument("--user", help="Provision a single user only")
    args = parser.parse_args()

    print("Fetching credentials...")
    gl_token = get_gitlab_token()
    kc_pass   = get_keycloak_admin_password()
    kc_token  = get_keycloak_token(kc_pass)

    if args.user:
        users = [args.user]
    else:
        users = get_developers(kc_token)

    print(f"Users to provision: {users}\n")

    gl_token_at_url = gl_token  # used in git URLs

    for username in users:
        print(f"── {username}")
        user_id, namespace_id = get_gitlab_user_namespace(username, gl_token)
        if not user_id:
            print(f"   SKIP: not found in GitLab")
            continue

        for repo_name, title in REPOS:
            exists = repo_exists(username, repo_name, gl_token)
            if exists:
                print(f"   SKIP {repo_name} (already exists)")
                # Still ensure catalog-info.yaml and RHDH registration are present
                resp = gitlab_get(
                    f"/projects/{requests.utils.quote(f'{username}/{repo_name}', safe='')}", gl_token
                )
                project_id = resp.json()["id"]
                catalog_url = f"{GITLAB_URL}/{username}/{repo_name}/-/blob/main/catalog-info.yaml"
                if not args.dry_run:
                    add_catalog_info(project_id, username, repo_name, title, gl_token)
                    register_in_rhdh(username, repo_name, catalog_url)
                continue

            if args.dry_run:
                print(f"   WOULD create {repo_name}")
                continue

            # Create repo
            source_url = (
                f"{GITLAB_URL.replace('https://', f'https://oauth2:{gl_token_at_url}@')}"
                f"/{SOURCE_GROUP}/{repo_name}.git"
            )
            dest_url = (
                f"{GITLAB_URL.replace('https://', f'https://oauth2:{gl_token_at_url}@')}"
                f"/{username}/{repo_name}.git"
            )

            try:
                project_id = create_repo(namespace_id, repo_name, gl_token)
                copy_repo(source_url, dest_url)
                add_catalog_info(project_id, username, repo_name, title, gl_token)
                catalog_url = f"{GITLAB_URL}/{username}/{repo_name}/-/blob/main/catalog-info.yaml"
                register_in_rhdh(username, repo_name, catalog_url)
                print(f"   OK  {repo_name}")
            except Exception as e:
                print(f"   ERR {repo_name}: {e}")

        print()

    print("Done. RHDH catalog will refresh within ~2 minutes.")


if __name__ == "__main__":
    main()
