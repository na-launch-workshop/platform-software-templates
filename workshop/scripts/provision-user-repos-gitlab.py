#!/usr/bin/env python3
"""
Provisions per-user workshop repos in GitLab.

For every user in the Keycloak 'developers' group:
  - Verifies the user exists in GitLab (synced via Keycloak SSO)
  - Copies each source repo from the GitLab 'developers' group
    into the user's personal GitLab namespace (clean copy, not fork)
  - Upserts a per-user catalog-info.yaml into the repo

Idempotent: skips repos that already exist for a user.
Run register-rhdh-catalog.py afterwards to register repos in the RHDH catalog.

Credential resolution order:
  1. CLI args (--token)
  2. Environment variable (GITLAB_TOKEN)
  3. Live cluster - reads GITLAB_ADMIN_TOKEN from the gitlab-service deployment
     in the gitlab namespace

Usage:
  python3 provision-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com
  python3 provision-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com --dry-run
  python3 provision-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com --user devben
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

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
KEYCLOAK_REALM = "openshift"
KEYCLOAK_GROUP = "developers"
KEYCLOAK_SKIP  = {"admin"}
GITLAB_NAMESPACES = ("gitlab", "gitlab-system")

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

    # Prefer a short-lived root OAuth token when the cluster exposes only a
    # group access token. This works on the GitLab Operator install used here.
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


def get_gitlab_url():
    result = subprocess.run(
        ["oc", "get", "route", "gitlab", "-n", "gitlab-system",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


def get_devspaces_url():
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
    """Return list of user dicts (username, id, email) in the Keycloak developers group."""
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
        {"username": m["username"], "id": m["id"], "email": m.get("email", f"{m['username']}@workshop.local")}
        for m in resp.json() if m["username"] not in KEYCLOAK_SKIP
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


def ensure_keycloak_email_verified(keycloak_url, kc_token, user):
    """Mark a Keycloak user's email as verified so GitLab OIDC login is not blocked."""
    if user.get("emailVerified"):
        return

    user = dict(user)
    user["emailVerified"] = True
    resp = requests.put(
        f"{keycloak_url}/auth/admin/realms/{KEYCLOAK_REALM}/users/{user['id']}",
        headers={
            "Authorization": f"Bearer {kc_token}",
            "Content-Type": "application/json",
        },
        json=user,
        verify=False,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# GitLab API helpers
# ---------------------------------------------------------------------------

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


def gl_put(base_url, token, path, data):
    return requests.put(f"{base_url}/api/v4{path}", headers=gl_headers(token), json=data, verify=False)


def gl_delete(base_url, token, path):
    return requests.delete(f"{base_url}/api/v4{path}", headers=gl_headers(token), verify=False)


def get_gitlab_user(base_url, token, username):
    """Return GitLab user dict or None if not found."""
    resp = gl_get(base_url, token, f"/users?username={username}")
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return None


def get_gitlab_namespace_id(base_url, token, gl_user):
    """Return the numeric personal namespace ID for a GitLab user."""
    if gl_user.get("namespace_id"):
        return gl_user["namespace_id"]

    username = gl_user["username"]
    resp = gl_get(base_url, token, f"/namespaces?search={quote(username)}")
    if resp.status_code != 200:
        raise RuntimeError(f"failed to read namespace for {username}: {resp.status_code} {resp.text}")

    for namespace in resp.json():
        if namespace.get("kind") == "user" and namespace.get("path") == username:
            return namespace["id"]

    raise RuntimeError(f"personal namespace not found for {username}")


def ensure_gitlab_user_confirmed(gl_user):
    """Confirm an auto-created GitLab OIDC user if GitLab still marks them unconfirmed."""
    if gl_user.get("confirmed_at"):
        return

    username = gl_user["username"].replace("\\", "\\\\").replace("'", "\\'")
    ruby = (
        "u = User.find_by_username('%s'); "
        "raise 'GitLab user not found' unless u; "
        "u.confirm unless u.confirmed?; "
        "u.reload; "
        "raise 'GitLab user confirmation did not persist' unless u.confirmed?"
    ) % username
    subprocess.run(
        ["oc", "exec", "-n", "gitlab-system", "deploy/gitlab-toolbox", "--",
         "gitlab-rails", "runner", ruby],
        capture_output=True, text=True, check=True,
    )


def project_exists(base_url, token, namespace, name):
    encoded = quote(f"{namespace}/{name}", safe="")
    return gl_get(base_url, token, f"/projects/{encoded}").status_code == 200


def get_project_id(base_url, token, namespace, name):
    encoded = quote(f"{namespace}/{name}", safe="")
    resp = gl_get(base_url, token, f"/projects/{encoded}")
    if resp.status_code == 200:
        return resp.json()["id"]
    return None


def create_user_project(base_url, token, namespace_id, name):
    """Create a project in a user's personal namespace."""
    resp = gl_post(base_url, token, "/projects", {
        "name": name,
        "path": name,
        "namespace_id": namespace_id,
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


# ---------------------------------------------------------------------------
# Git helper
# ---------------------------------------------------------------------------

def update_catalog_info(repo_path, username, repo_name, base_url, devspaces_url):
    """
    Update catalog-info.yaml in place without touching any other formatting.
    Changes only:
      - metadata.name       → {username}-{repo_name}
      - source-location     → url:{base_url}/{username}/{repo_name}
      - Dev Spaces link url → {devspaces_url}/#{base_url}/{username}/{repo_name}
      - spec.owner          → user:default/{username}
    """
    import re
    catalog_path = os.path.join(repo_path, "catalog-info.yaml")
    repo_url = f"{base_url}/{username}/{repo_name}"

    with open(catalog_path) as f:
        lines = f.readlines()

    in_metadata = False
    metadata_name_set = False

    for i, line in enumerate(lines):
        stripped = line.rstrip()

        # Track metadata section
        if re.match(r"^metadata:\s*$", stripped):
            in_metadata = True
        elif re.match(r"^[a-z]", stripped) and not stripped.startswith("metadata"):
            in_metadata = False

        # metadata.name (first `name:` inside metadata block)
        if in_metadata and not metadata_name_set and re.match(r"^\s+name:\s*", stripped):
            indent = len(line) - len(line.lstrip())
            lines[i] = " " * indent + f"name: {username}-{repo_name}\n"
            metadata_name_set = True

        # backstage.io/source-location annotation
        elif "backstage.io/source-location:" in stripped:
            indent = len(line) - len(line.lstrip())
            lines[i] = " " * indent + f"backstage.io/source-location: url:{repo_url}\n"

        # Dev Spaces link url (line before `title: Open in Dev Spaces`)
        elif i + 1 < len(lines) and "Open in Dev Spaces" in lines[i + 1]:
            indent = len(line) - len(line.lstrip())
            lines[i] = " " * indent + f"url: {devspaces_url}/#{repo_url}\n"

        # spec.owner
        elif re.match(r"^\s+owner:\s*", stripped):
            indent = len(line) - len(line.lstrip())
            lines[i] = " " * indent + f"owner: user:default/{username}\n"

    with open(catalog_path, "w") as f:
        f.writelines(lines)


def clone_push_with_catalog_info(source_url, dest_url, username, repo_name, base_url, devspaces_url):
    """Clone source, update catalog-info.yaml fields, push to destination."""
    tmpdir = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmpdir, "repo")
        subprocess.run(["git", "clone", source_url, repo], check=True)
        update_catalog_info(repo, username, repo_name, base_url, devspaces_url)
        subprocess.run(["git", "add", "catalog-info.yaml"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=SysAdmin", "-c", "user.email=sys@admin.com",
                        "commit", "-m", "Update catalog-info.yaml for user"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "set-url", "origin", dest_url], cwd=repo, check=True)
        subprocess.run(["git", "push", "origin", "main", "--force"], cwd=repo, check=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def update_catalog_info_via_git(repo_url, username, repo_name, base_url, devspaces_url):
    """Clone existing user repo, update catalog-info.yaml fields, push back."""
    tmpdir = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmpdir, "repo")
        subprocess.run(["git", "clone", repo_url, repo], check=True)
        update_catalog_info(repo, username, repo_name, base_url, devspaces_url)
        subprocess.run(["git", "add", "catalog-info.yaml"], cwd=repo, check=True)
        result = subprocess.run(["git", "-c", "user.name=SysAdmin", "-c", "user.email=sys@admin.com",
                                  "commit", "-m", "Update catalog-info.yaml for user"], cwd=repo, capture_output=True, text=True)
        if result.returncode != 0 and "nothing to commit" in result.stdout + result.stderr:
            return
        result.check_returncode()
        subprocess.run(["git", "push"], cwd=repo, check=True)
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
    parser.add_argument("--gitlab-url", default=None, help="GitLab base URL (default: read from cluster route)")
    parser.add_argument("--token",      default=os.environ.get("GITLAB_TOKEN"), help="GitLab admin personal access token (optional, reads from cluster if omitted)")
    parser.add_argument("--configmap",  default=os.path.join(SCRIPT_DIR, "repos-configmap.yaml"), help="Path to ConfigMap YAML")
    parser.add_argument("--user",       help="Provision a single user only")
    parser.add_argument("--dry-run",    action="store_true", help="Print actions without executing")
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

    # Load repos from ConfigMap
    config      = load_configmap(args.configmap)
    source_group = config.get("gitea_org", "developers")
    repos        = config.get("repos", [])

    if not repos:
        sys.exit("No repos found in ConfigMap.")

    devspaces_url = get_devspaces_url()

    # Push URL with embedded token
    push_base = base_url.replace("https://", f"https://oauth2:{token}@") \
                        .replace("http://",  f"http://oauth2:{token}@")

    # Fetch Keycloak users
    print("Reading Keycloak users...")
    kc_url   = get_keycloak_url()
    kc_pass  = get_keycloak_admin_password()
    kc_token = get_keycloak_token(kc_url, kc_pass)

    if args.user:
        kc_user = get_keycloak_user(kc_url, kc_token, args.user)
        if not kc_user:
            sys.exit(f"Keycloak user '{args.user}' not found")
        users = [{
            "username": kc_user["username"],
            "id": kc_user["id"],
            "email": kc_user.get("email", f"{kc_user['username']}@workshop.local"),
            "emailVerified": kc_user.get("emailVerified", False),
        }]
    else:
        users = get_developers(kc_url, kc_token)

    print(f"  Users: {[u['username'] for u in users]}\n")
    print(f"GitLab:       {base_url}")
    print(f"Source group: {source_group}")
    print(f"Repos:        {len(repos)}\n")

    errors = []
    for user in users:
        username = user["username"]
        print(f"── {username}")

        try:
            ensure_keycloak_email_verified(kc_url, kc_token, user)
        except Exception as exc:
            print(f"   WARN: could not mark email verified for '{username}' — {exc}")
            errors.append(username)
            continue

        # Look up user in GitLab
        gl_user = get_gitlab_user(base_url, token, username)
        if not gl_user:
            print(f"   WARN: '{username}' not found in GitLab — skipping (SSO login may not have occurred yet)\n")
            errors.append(username)
            continue

        try:
            ensure_gitlab_user_confirmed(gl_user)
            gl_user = get_gitlab_user(base_url, token, username)
        except Exception as exc:
            print(f"   WARN: could not confirm GitLab user '{username}' — {exc}\n")
            errors.append(username)
            continue

        try:
            namespace_id = get_gitlab_namespace_id(base_url, token, gl_user)
        except Exception as exc:
            print(f"   WARN: could not resolve personal namespace for '{username}' — {exc}\n")
            errors.append(username)
            continue

        for repo in repos:
            name = repo.get("name")

            if project_exists(base_url, token, username, name):
                print(f"   SKIP {name} (already exists, updating catalog-info)")
                if not args.dry_run:
                    try:
                        project_id = get_project_id(base_url, token, username, name)
                        if project_id:
                            ensure_branch_unprotected(base_url, token, project_id)
                        update_catalog_info_via_git(f"{push_base}/{username}/{name}.git", username, name, base_url, devspaces_url)
                    except Exception as exc:
                        print(f"   ERR updating catalog-info: {exc}")
                        errors.append(f"{username}/{name}")
                continue

            if args.dry_run:
                print(f"   WOULD copy {source_group}/{name} → {username}/{name}")
                continue

            source_url = f"{push_base}/{source_group}/{name}.git"
            dest_url   = f"{push_base}/{username}/{name}.git"

            try:
                project_id = create_user_project(base_url, token, namespace_id, name)
                ensure_branch_unprotected(base_url, token, project_id)
                clone_push_with_catalog_info(source_url, dest_url, username, name, base_url, devspaces_url)
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
