"""Shared GitLab helpers for workshop scripts (token resolution, listing source repos)."""

# shared helper module used by the other scripts. It contains two things:

#   1. get_gitlab_token_from_cluster — resolves a GitLab admin token automatically by trying multiple sources
#    in order: deployment env vars, root OAuth login, and known secrets across namespaces (gitlab,
#   gitlab-system, backstage)
#   2. list_group_projects_with_path_prefix — lists all projects in a GitLab group whose path starts with a
#   given prefix (e.g. workshop-), handling pagination

#   provision-user-repos-gitlab.py imports from it, and purge-user-repos-gitlab.py also imports
#   get_gitlab_token_from_cluster from it so token resolution works the same way across all scripts.

from __future__ import annotations

import base64
import json
import subprocess
import sys
from urllib.parse import quote

import requests


def gitlab_auth_headers(token: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if token.startswith("glpat-"):
        headers["PRIVATE-TOKEN"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_gitlab_token_from_cluster(base_url: str) -> str:
    """Read an admin-capable GitLab API token from the cluster."""
    errors: list[str] = []
    base_url = base_url.rstrip("/")

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


def list_group_projects_with_path_prefix(
    base_url: str,
    token: str,
    group_path: str,
    path_prefix: str = "workshop-",
    *,
    verify_ssl: bool = False,
) -> list[dict]:
    """
    Return sorted [{"name": project path, "title": display name}, ...] for direct projects
    in ``group_path`` whose GitLab ``path`` starts with ``path_prefix`` (not subgroups).
    """
    base_url = base_url.rstrip("/")
    enc = quote(group_path, safe="")
    page = 1
    per_page = 100
    seen: set[str] = set()
    result: list[dict] = []

    while True:
        resp = requests.get(
            f"{base_url}/api/v4/groups/{enc}/projects",
            headers=gitlab_auth_headers(token),
            params={
                "page": page,
                "per_page": per_page,
                "include_subgroups": "false",
            },
            verify=verify_ssl,
        )
        if resp.status_code == 404:
            sys.exit(f"GitLab group not found: {group_path!r}")
        resp.raise_for_status()
        projects = resp.json()
        if not projects:
            break
        for p in projects:
            path = p.get("path") or ""
            if not path.startswith(path_prefix):
                continue
            if path in seen:
                continue
            seen.add(path)
            result.append({"name": path, "title": p.get("name") or path})
        if len(projects) < per_page:
            break
        page += 1

    result.sort(key=lambda x: x["name"])
    return result
