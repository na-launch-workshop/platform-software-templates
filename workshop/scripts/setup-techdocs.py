#!/usr/bin/env python3
"""
TechDocs setup script

1. Adds mkdocs.yml + docs/index.md to GitHub source repos (where missing)
2. Adds mkdocs.yml + docs/index.md to GitLab developers group repos (where missing)
3. Adds backstage.io/techdocs-ref annotation to all user repos' catalog-info.yaml
4. Adds mkdocs.yml + docs/index.md to user repos where missing

Usage:
  python3 setup-techdocs.py
  python3 setup-techdocs.py --dry-run
  python3 setup-techdocs.py --skip-github
"""

import argparse
import base64
import json
import subprocess
import sys

import requests
import urllib3
urllib3.disable_warnings()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITLAB_URL    = "https://gitlab.apps.rosa.rosa-bvwwp.naah.p3.openshiftapps.com"
GITHUB_ORG    = "na-launch-workshop"
GITHUB_API    = "https://api.github.com"
KEYCLOAK_URL  = "https://keycloak-keycloak.apps.rosa.rosa-bvwwp.naah.p3.openshiftapps.com/auth"
KEYCLOAK_REALM = "openshift"
KEYCLOAK_DEVELOPERS_GROUP = "developers"
KEYCLOAK_SKIP_USERS = {"admin"}
DEVSPACES_URL = "https://devspaces.apps.rosa.rosa-bvwwp.naah.p3.openshiftapps.com"

SOURCE_GROUP = "developers"

REPOS = [
    ("workshop-quarkus-hello_quarkus",                       "Module 1 - Hello Quarkus",                      "Introduction to Red Hat Dev Spaces with a Quarkus application"),
    ("workshop-quarkus-using_camel",                         "Module 2 - Using Camel",                        "Using Apache Camel with Quarkus"),
    ("workshop-quarkus-using_funqy_stateless_events",        "Module 3 - Using Funqy Stateless Events",       "Stateless event processing with Quarkus Funqy"),
    ("workshop-quarkus-blue_green_deployments_openshift",    "Module 4 - Blue Green Deployments OpenShift",   "Blue/green deployment strategies on OpenShift"),
    ("workshop-quarkus-openshift_telemetry_with_micrometer", "Module 5 - OpenShift Telemetry With Micrometer","Observability and telemetry with Micrometer"),
    ("workshop-quarkus-interacting_with_kafka",              "Module 6 - Interacting With Kafka",             "Event streaming with Apache Kafka and Quarkus"),
]

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


def get_github_token():
    result = subprocess.run(
        ["oc", "exec", "-n", "backstage", "deployment/backstage-developer-hub",
         "--", "env"],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        if line.startswith("GITHUB_TOKEN="):
            return line.split("=", 1)[1]
    sys.exit("Could not retrieve GITHUB_TOKEN from RHDH pod")


def get_keycloak_token():
    kc_pass_result = subprocess.run(
        ["oc", "get", "secret", "-n", "keycloak", "credential-keycloak",
         "-o", "jsonpath={.data.ADMIN_PASSWORD}"],
        capture_output=True, text=True
    )
    kc_pass = base64.b64decode(kc_pass_result.stdout.strip()).decode()
    resp = requests.post(
        f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token",
        data={"client_id": "admin-cli", "grant_type": "password",
              "username": "admin", "password": kc_pass},
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_developers(kc_token):
    resp = requests.get(
        f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/groups",
        headers={"Authorization": f"Bearer {kc_token}"}, verify=False,
    )
    resp.raise_for_status()
    group_id = next(
        (g["id"] for g in resp.json() if g["name"] == KEYCLOAK_DEVELOPERS_GROUP), None
    )
    if not group_id:
        sys.exit(f"Keycloak group '{KEYCLOAK_DEVELOPERS_GROUP}' not found")
    resp = requests.get(
        f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/groups/{group_id}/members?max=200",
        headers={"Authorization": f"Bearer {kc_token}"}, verify=False,
    )
    resp.raise_for_status()
    return [m["username"] for m in resp.json() if m["username"] not in KEYCLOAK_SKIP_USERS]


def gl_get(path, gl_token):
    return requests.get(
        f"{GITLAB_URL}/api/v4{path}",
        headers={"PRIVATE-TOKEN": gl_token}, verify=False,
    )


def gl_put(path, gl_token, data):
    return requests.put(
        f"{GITLAB_URL}/api/v4{path}",
        headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
        json=data, verify=False,
    )


def gl_post(path, gl_token, data):
    return requests.post(
        f"{GITLAB_URL}/api/v4{path}",
        headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
        json=data, verify=False,
    )


def gh_get(path, gh_token):
    return requests.get(
        f"{GITHUB_API}{path}",
        headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
    )


def gh_put(path, gh_token, data):
    return requests.put(
        f"{GITHUB_API}{path}",
        headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        json=data,
    )


def make_mkdocs(site_name, site_description):
    return f"""site_name: {site_name}
site_description: {site_description}

nav:
  - Overview: index.md

plugins:
  - techdocs-core
"""


def make_index_md(title, description):
    return f"""# {title}

{description}

## Overview

This is a Quarkus workshop module. Open this repository in Red Hat Dev Spaces to get started.
"""


def get_impersonation_token(user_id, gl_token):
    resp = gl_post(
        f"/users/{user_id}/impersonation_tokens",
        gl_token,
        {"name": "techdocs-setup", "scopes": ["api"], "expires_at": "2026-12-31"},
    )
    if resp.status_code not in (200, 201):
        return None, None
    return resp.json().get("token"), resp.json().get("id")


def revoke_impersonation_token(user_id, token_id, gl_token):
    requests.delete(
        f"{GITLAB_URL}/api/v4/users/{user_id}/impersonation_tokens/{token_id}",
        headers={"PRIVATE-TOKEN": gl_token}, verify=False,
    )


def file_exists_in_gl(project_path, file_path, gl_token):
    encoded = requests.utils.quote(file_path, safe='')
    resp = gl_get(f"/projects/{requests.utils.quote(project_path, safe='')}/repository/files/{encoded}?ref=main", gl_token)
    return resp.status_code == 200


def upsert_gl_file(project_path, file_path, content, commit_msg, gl_token, dry_run=False):
    if dry_run:
        print(f"     WOULD upsert {file_path} in {project_path}")
        return True
    encoded_project = requests.utils.quote(project_path, safe='')
    encoded_file = requests.utils.quote(file_path, safe='')
    exists = file_exists_in_gl(project_path, file_path, gl_token)
    method = gl_put if exists else gl_post
    resp = method(
        f"/projects/{encoded_project}/repository/files/{encoded_file}",
        gl_token,
        {"branch": "main", "content": content, "commit_message": commit_msg},
    )
    return resp.status_code in (200, 201)


def get_gh_file_sha(repo, file_path, gh_token):
    resp = gh_get(f"/repos/{GITHUB_ORG}/{repo}/contents/{file_path}", gh_token)
    if resp.status_code == 200:
        return resp.json().get("sha")
    return None


def upsert_gh_file(repo, file_path, content, commit_msg, gh_token, dry_run=False):
    if dry_run:
        print(f"     WOULD upsert {file_path} in github/{repo}")
        return True
    sha = get_gh_file_sha(repo, file_path, gh_token)
    data = {
        "message": commit_msg,
        "content": base64.b64encode(content.encode()).decode(),
    }
    if sha:
        data["sha"] = sha
    resp = gh_put(f"/repos/{GITHUB_ORG}/{repo}/contents/{file_path}", gh_token, data)
    return resp.status_code in (200, 201)


def get_gl_user_id(username, gl_token):
    resp = gl_get(f"/users?username={username}", gl_token)
    users = resp.json()
    if not users:
        return None, None
    return users[0]["id"], users[0]["namespace_id"]


def get_catalog_info_content(project_path, gl_token):
    encoded = requests.utils.quote(project_path, safe='')
    resp = gl_get(f"/projects/{encoded}/repository/files/catalog-info.yaml/raw?ref=main", gl_token)
    if resp.status_code == 200:
        return resp.text
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-github", action="store_true", help="Skip GitHub repos")
    parser.add_argument("--skip-source", action="store_true", help="Skip GitLab developers group repos")
    parser.add_argument("--skip-users", action="store_true", help="Skip user repos")
    args = parser.parse_args()

    print("Fetching credentials...")
    gl_token = get_gitlab_token()
    gh_token = get_github_token() if not args.skip_github else None
    kc_token = get_keycloak_token()
    users = get_developers(kc_token)
    print(f"Developers: {users}\n")

    # ------------------------------------------------------------------
    # 1. GitHub source repos
    # ------------------------------------------------------------------
    if not args.skip_github:
        print("=== GitHub source repos ===")
        for repo_name, title, description in REPOS:
            print(f"  {repo_name}")
            mkdocs = make_mkdocs(title, description)
            index_md = make_index_md(title, description)

            sha_mkdocs = get_gh_file_sha(repo_name, "mkdocs.yml", gh_token)
            sha_index  = get_gh_file_sha(repo_name, "docs/index.md", gh_token)

            if not sha_mkdocs:
                ok = upsert_gh_file(repo_name, "mkdocs.yml", mkdocs, "Add mkdocs.yml for TechDocs", gh_token, args.dry_run)
                print(f"    mkdocs.yml: {'OK' if ok else 'ERR'}")
            else:
                print(f"    mkdocs.yml: already exists")

            if not sha_index:
                ok = upsert_gh_file(repo_name, "docs/index.md", index_md, "Add docs/index.md for TechDocs", gh_token, args.dry_run)
                print(f"    docs/index.md: {'OK' if ok else 'ERR'}")
            else:
                print(f"    docs/index.md: already exists")
        print()

    # ------------------------------------------------------------------
    # 2. GitLab developers group repos
    # ------------------------------------------------------------------
    if not args.skip_source:
        print("=== GitLab developers group repos ===")
        for repo_name, title, description in REPOS:
            print(f"  {repo_name}")
            project_path = f"{SOURCE_GROUP}/{repo_name}"
            mkdocs = make_mkdocs(title, description)
            index_md = make_index_md(title, description)

            if not file_exists_in_gl(project_path, "mkdocs.yml", gl_token):
                ok = upsert_gl_file(project_path, "mkdocs.yml", mkdocs, "Add mkdocs.yml for TechDocs", gl_token, args.dry_run)
                print(f"    mkdocs.yml: {'OK' if ok else 'ERR'}")
            else:
                print(f"    mkdocs.yml: already exists")

            if not file_exists_in_gl(project_path, "docs/index.md", gl_token):
                ok = upsert_gl_file(project_path, "docs/index.md", index_md, "Add docs/index.md for TechDocs", gl_token, args.dry_run)
                print(f"    docs/index.md: {'OK' if ok else 'ERR'}")
            else:
                print(f"    docs/index.md: already exists")
        print()

    # ------------------------------------------------------------------
    # 3. User repos
    # ------------------------------------------------------------------
    if not args.skip_users:
        print("=== User repos ===")
        for username in users:
            print(f"── {username}")
            user_id, _ = get_gl_user_id(username, gl_token)
            if not user_id:
                print(f"   SKIP: not found in GitLab")
                continue

            imp_token, imp_token_id = get_impersonation_token(user_id, gl_token)
            if not imp_token:
                print(f"   SKIP: could not get impersonation token")
                continue

            try:
                for repo_name, title, description in REPOS:
                    project_path = f"{username}/{repo_name}"
                    mkdocs = make_mkdocs(title, description)
                    index_md = make_index_md(title, description)

                    # Add missing mkdocs.yml
                    if not file_exists_in_gl(project_path, "mkdocs.yml", imp_token):
                        ok = upsert_gl_file(project_path, "mkdocs.yml", mkdocs, "Add mkdocs.yml for TechDocs", imp_token, args.dry_run)
                        print(f"   {repo_name}: mkdocs.yml {'OK' if ok else 'ERR'}")
                    else:
                        pass  # already exists

                    # Add missing docs/index.md
                    if not file_exists_in_gl(project_path, "docs/index.md", imp_token):
                        ok = upsert_gl_file(project_path, "docs/index.md", index_md, "Add docs/index.md for TechDocs", imp_token, args.dry_run)
                        print(f"   {repo_name}: docs/index.md {'OK' if ok else 'ERR'}")
                    else:
                        pass  # already exists

                    # Update catalog-info.yaml to add techdocs-ref if missing
                    catalog = get_catalog_info_content(project_path, imp_token)
                    if catalog and "backstage.io/techdocs-ref" not in catalog:
                        # Inject annotation after existing annotations block
                        updated = catalog.replace(
                            "  annotations:\n    gitlab.com/project-slug:",
                            "  annotations:\n    backstage.io/techdocs-ref: dir:.\n    gitlab.com/project-slug:",
                        )
                        if updated != catalog:
                            ok = upsert_gl_file(project_path, "catalog-info.yaml", updated,
                                                "Add techdocs-ref annotation", imp_token, args.dry_run)
                            print(f"   {repo_name}: catalog-info.yaml (techdocs-ref) {'OK' if ok else 'ERR'}")
                    elif catalog and "backstage.io/techdocs-ref" in catalog:
                        pass  # already has annotation
                    else:
                        print(f"   {repo_name}: catalog-info.yaml not found, skipping")
            finally:
                if imp_token_id:
                    revoke_impersonation_token(user_id, imp_token_id, gl_token)

            print()

    print("Done.")


if __name__ == "__main__":
    main()
