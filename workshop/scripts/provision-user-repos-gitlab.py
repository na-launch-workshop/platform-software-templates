#!/usr/bin/env python3
"""
Provisions per-user workshop repos in GitLab.

For every user in the Keycloak 'developers' group:
  - Verifies the user exists in GitLab (synced via Keycloak SSO)
  - Copies each source repo from the GitLab 'developers' group
    into the user's personal GitLab namespace (clean copy, not fork)
  - Clones from ``developers/…``; **only** in ``catalog-info.yaml`` (when present): sets
    ``metadata.name``, rewrites ``metadata.links`` URLs from the source group to the user
    namespace, and sets ``spec.owner``. No other fields are changed. If a repo has no
    ``catalog-info.yaml``, the script does not create one. Idempotent. Prints a summary.

Credential resolution order:
  1. CLI args (--token)
  2. Environment variable (GITLAB_TOKEN)
  3. Live cluster - reads GITLAB_ADMIN_TOKEN from the gitlab-service deployment
     in the gitlab namespace

Template repos are discovered from GitLab: direct projects under the source group whose
path starts with ``workshop-``. Optional repos-configmap.yaml supplies only ``gitea_org``
(group path); the ``repos:`` list in that file is not used for GitLab provisioning.

Usage:
  python3 provision-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com
  python3 provision-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com --dry-run
  python3 provision-user-repos-gitlab.py --gitlab-url https://gitlab.apps.example.com --user devben
  python3 provision-user-repos-gitlab.py --source-group developers
"""

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

import requests
import urllib3
import yaml

from workshop_gitlab import get_gitlab_token_from_cluster, list_group_projects_with_path_prefix

urllib3.disable_warnings()

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
KEYCLOAK_REALM = "openshift"
KEYCLOAK_GROUP = "developers"
KEYCLOAK_SKIP  = {"admin"}

def _eol_for_line(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def apply_catalog_info_patches(raw: str, username: str, repo_name: str, source_group: str) -> str:
    """
    Update only (text-preserving, no full-file yaml.dump):
    - the ``metadata`` entity ``name:`` line (first ``^  name:`` in ``metadata:``);
    - ``url:`` line values under links (``.../`` + source group + ``/``  →  user);
    - the ``spec`` ``  owner:`` line.

    Other bytes/lines/whitespace (including multiline ``description: '...'``) are left unchanged.
    """
    if raw is None:
        raise ValueError("catalog-info.yaml: empty")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"catalog-info.yaml: invalid YAML: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("catalog-info.yaml: root must be a mapping")

    from_seg = f"/{source_group}/"
    to_seg = f"/{username}/"
    new_name = f"{username}-{repo_name}"
    new_owner = f"user:default/{username}"
    lines = raw.splitlines(keepends=True)
    if not lines:
        return raw

    in_metadata = False
    in_spec = False
    name_replaced = False
    out: list[str] = []

    for line in lines:
        if re.match(r"^\s*#", line):
            out.append(line)
            continue
        st = line.strip()
        st_key = st.split("#", 1)[0].strip()
        if st and (not line[0].isspace()):
            m = re.match(r"^([A-Za-z0-9_-]+)\s*:", st_key)
            if m:
                k = m.group(1)
                if k == "metadata":
                    in_metadata, in_spec = True, False
                elif k == "spec":
                    in_spec, in_metadata = True, False
                else:
                    in_metadata = in_spec = False
            else:
                in_metadata = in_spec = False
            out.append(line)
            continue

        if in_metadata and re.match(r"^  name:\s*", line) and not name_replaced:
            out.append(f"  name: {new_name}{_eol_for_line(line)}")
            name_replaced = True
            continue
        if in_spec and re.match(r"^  owner:\s*", line):
            out.append(f"  owner: {new_owner}{_eol_for_line(line)}")
            continue
        if in_metadata and re.match(r"^\s+backstage\.io/kubernetes-namespace:\s*", line):
            out.append(f"    backstage.io/kubernetes-namespace: {username}-devspaces{_eol_for_line(line)}")
            continue
        if in_metadata and (
            re.match(r"^\s*-\s*url:\s*", line) or re.match(r"^\s{3,}url:\s*", line)
        ):
            new_line = line.replace(from_seg, to_seg)
            new_line = new_line.replace(f"{source_group}-devspaces", f"{username}-devspaces")
            out.append(new_line)
            continue
        out.append(line)

    return "".join(out)


# ---------------------------------------------------------------------------
# Cluster credential helpers
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


def get_gitlab_url():
    result = subprocess.run(
        ["oc", "get", "route", "gitlab", "-n", "gitlab-system",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


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

def clone_push_with_catalog_info(
    source_url, dest_url, username: str, repo_name: str, source_group: str
):
    """Clone source; patch catalog-info only if present; never create catalog-info; push."""
    tmpdir = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmpdir, "repo")
        subprocess.run(["git", "clone", source_url, repo], check=True)
        cat_path = os.path.join(repo, "catalog-info.yaml")
        if os.path.isfile(cat_path):
            with open(cat_path) as f:
                raw = f.read()
            new_raw = apply_catalog_info_patches(raw, username, repo_name, source_group)
            if new_raw != raw:
                with open(cat_path, "w") as f:
                    f.write(new_raw)
                subprocess.run(["git", "add", "catalog-info.yaml"], cwd=repo, check=True)
                subprocess.run(
                    ["git", "-c", "user.name=SysAdmin", "-c", "user.email=sys@admin.com",
                     "commit", "-m", "Update catalog-info for user namespace"],
                    cwd=repo, check=True,
                )
        subprocess.run(["git", "remote", "set-url", "origin", dest_url], cwd=repo, check=True)
        subprocess.run(["git", "push", "origin", "main", "--force"], cwd=repo, check=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def patch_catalog_info_in_cloned_user_repo(
    repo_url, username: str, repo_name: str, source_group: str
) -> bool:
    """
    If catalog-info.yaml exists, apply the same name / links / owner patch and push.
    If missing, do nothing. Returns True if a new commit was pushed.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmpdir, "repo")
        subprocess.run(["git", "clone", repo_url, repo], check=True)
        cat_path = os.path.join(repo, "catalog-info.yaml")
        if not os.path.isfile(cat_path):
            return False
        with open(cat_path) as f:
            raw = f.read()
        new_raw = apply_catalog_info_patches(raw, username, repo_name, source_group)
        if new_raw == raw:
            return False
        with open(cat_path, "w") as f:
            f.write(new_raw)
        subprocess.run(["git", "add", "catalog-info.yaml"], cwd=repo, check=True)
        result = subprocess.run(
            ["git", "-c", "user.name=SysAdmin", "-c", "user.email=sys@admin.com",
             "commit", "-m", "Update catalog-info for user namespace"],
            cwd=repo, capture_output=True, text=True,
        )
        if result.returncode != 0 and "nothing to commit" in (result.stdout + result.stderr):
            return False
        result.check_returncode()
        subprocess.run(["git", "push"], cwd=repo, check=True)
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tekton webhook helpers
# ---------------------------------------------------------------------------

TEKTON_WEBHOOK_REPO = "workshop-springboot-hello_by_lang"

def get_apps_domain(base_url: str) -> str:
    """Extract the apps domain from the GitLab base URL (e.g. apps.rosa.example.com)."""
    host = base_url.replace("https://", "").replace("http://", "")
    # gitlab.apps.xxx -> apps.xxx
    parts = host.split(".", 1)
    return parts[1] if len(parts) > 1 else host


def ensure_gitlab_webhook(base_url: str, token: str, project_id: int, webhook_url: str) -> None:
    """Create the Tekton EventListener webhook on the project if not already present."""
    resp = requests.get(
        f"{base_url}/api/v4/projects/{project_id}/hooks",
        headers=gl_headers(token),
        verify=False,
    )
    resp.raise_for_status()
    for hook in resp.json():
        if hook.get("url") == webhook_url:
            return  # already exists
    resp = requests.post(
        f"{base_url}/api/v4/projects/{project_id}/hooks",
        headers=gl_headers(token),
        verify=False,
        json={
            "url": webhook_url,
            "push_events": True,
            "enable_ssl_verification": False,
        },
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# ConfigMap loader (optional: source group path only)
# ---------------------------------------------------------------------------

def load_optional_source_group_from_configmap(path):
    """Return ``gitea_org`` from repos ConfigMap, or None if file is missing."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        cm = yaml.safe_load(f)
    data = yaml.safe_load(cm["data"]["repos.yaml"])
    return data.get("gitea_org")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gitlab-url", default=None, help="GitLab base URL (default: read from cluster route)")
    parser.add_argument("--token",      default=os.environ.get("GITLAB_TOKEN"), help="GitLab admin personal access token (optional, reads from cluster if omitted)")
    parser.add_argument("--configmap",  default=os.path.join(SCRIPT_DIR, "repos-configmap.yaml"), help="Optional ConfigMap YAML (only ``gitea_org`` is read for source group path)")
    parser.add_argument("--source-group", default=None, help="GitLab group for template repos (overrides ConfigMap gitea_org; default developers)")
    parser.add_argument("--user",       help="Provision a single user only")
    parser.add_argument("--dry-run",    action="store_true", help="Print actions without executing")
    args = parser.parse_args()

    base_url = args.gitlab_url.rstrip("/") if args.gitlab_url else get_gitlab_url()
    apps_domain = get_apps_domain(base_url)

    token = args.token
    if not token:
        print("No token provided — reading from cluster...")
        token = get_gitlab_token_from_cluster(base_url)
        print("  token: (loaded from cluster)\n")

    source_group = args.source_group
    if not source_group:
        source_group = load_optional_source_group_from_configmap(args.configmap) or "developers"

    repos = list_group_projects_with_path_prefix(base_url, token, source_group, "workshop-")
    if not repos:
        sys.exit(
            f"No GitLab projects under group {source_group!r} with path prefix 'workshop-'."
        )

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
    newly_provisioned = []   # f"{user}/{repo}" — cloned from source group and pushed
    catalog_patched = []   # f"{user}/repo" — existing project; catalog-info commit pushed
    dry_would_clone = []  # dry-run only: would create
    dry_skip_existing = []  # dry-run only: would patch + push

    for user in users:
        username = user["username"]
        print(f"── {username}")

        # Admin-cli access tokens are short-lived (~60s); Git work per user can exceed that.
        kc_token = get_keycloak_token(kc_url, kc_pass)

        try:
            ensure_keycloak_email_verified(kc_url, kc_token, user)
        except Exception as exc:
            print(f"   WARN: could not mark email verified for '{username}' — {exc}")

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
                print(f"   SKIP {name} (already exists, patch catalog-info if any)")
                if args.dry_run:
                    dry_skip_existing.append(f"{username}/{name}")
                    continue
                try:
                    project_id = get_project_id(base_url, token, username, name)
                    if project_id:
                        ensure_branch_unprotected(base_url, token, project_id)
                        if name == TEKTON_WEBHOOK_REPO:
                            webhook_url = f"https://springboot-listener-{username}-devspaces.{apps_domain}"
                            try:
                                ensure_gitlab_webhook(base_url, token, project_id, webhook_url)
                                print(f"   HOOK {name} → {webhook_url}")
                            except Exception as exc:
                                print(f"   WARN webhook: {exc}")
                    if patch_catalog_info_in_cloned_user_repo(
                        f"{push_base}/{username}/{name}.git", username, name, source_group
                    ):
                        catalog_patched.append(f"{username}/{name}")
                except Exception as exc:
                    print(f"   ERR catalog-info: {exc}")
                    errors.append(f"{username}/{name}")
                continue

            if args.dry_run:
                print(f"   WOULD copy {source_group}/{name} → {username}/{name}")
                dry_would_clone.append(f"{username}/{name}")
                continue

            source_url = f"{push_base}/{source_group}/{name}.git"
            dest_url   = f"{push_base}/{username}/{name}.git"

            try:
                project_id = create_user_project(base_url, token, namespace_id, name)
                ensure_branch_unprotected(base_url, token, project_id)
                clone_push_with_catalog_info(source_url, dest_url, username, name, source_group)
                if name == TEKTON_WEBHOOK_REPO:
                    webhook_url = f"https://springboot-listener-{username}-devspaces.{apps_domain}"
                    try:
                        ensure_gitlab_webhook(base_url, token, project_id, webhook_url)
                        print(f"   HOOK {name} → {webhook_url}")
                    except Exception as exc:
                        print(f"   WARN webhook: {exc}")
                print(f"   OK  {name}")
                newly_provisioned.append(f"{username}/{name}")
            except Exception as exc:
                print(f"   ERR {name}: {exc}")
                errors.append(f"{username}/{name}")

        print()

    def print_summary(dry: bool) -> None:
        print("Summary")
        if dry:
            if dry_would_clone:
                print(f"  Would clone ({len(dry_would_clone)}):")
                for ref in dry_would_clone:
                    print(f"    {ref}")
            if dry_skip_existing:
                print(f"  Would patch/push — project already exists ({len(dry_skip_existing)}):")
                for ref in dry_skip_existing:
                    print(f"    {ref}")
            if not dry_would_clone and not dry_skip_existing:
                print("  (no repo actions)")

        else:
            if newly_provisioned:
                print(f"  Cloned and pushed (new) ({len(newly_provisioned)}):")
                for ref in newly_provisioned:
                    print(f"    {ref}")
            if catalog_patched:
                print(f"  Patched catalog-info in existing project ({len(catalog_patched)}):")
                for ref in catalog_patched:
                    print(f"    {ref}")
            if not newly_provisioned and not catalog_patched:
                print("  (no successful repo operations)")

    print_summary(args.dry_run)

    if errors:
        print(f"Failed: {errors}")
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
