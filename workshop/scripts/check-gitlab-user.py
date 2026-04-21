#!/usr/bin/env python3
"""
Check if a user exists in GitLab.

Usage:
  python3 check-gitlab-user.py --user will
"""

import argparse
import base64
import subprocess
import sys

import requests
import urllib3

urllib3.disable_warnings()


def get_gitlab_url():
    result = subprocess.run(
        ["oc", "get", "route", "gitlab", "-n", "gitlab-system",
         "-o", "jsonpath={.spec.host}"],
        capture_output=True, text=True, check=True,
    )
    return f"https://{result.stdout.strip()}"


def get_gitlab_token(base_url):
    result = subprocess.run(
        ["oc", "get", "secret", "gitlab-gitlab-initial-root-password",
         "-n", "gitlab-system", "-o", "jsonpath={.data.password}"],
        capture_output=True, text=True, check=True,
    )
    password = base64.b64decode(result.stdout.strip()).decode()
    resp = requests.post(
        f"{base_url}/oauth/token",
        data={"grant_type": "password", "username": "root",
              "password": password, "scope": "api"},
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", required=True, help="Username to look up")
    args = parser.parse_args()

    base_url  = get_gitlab_url()
    token     = get_gitlab_token(base_url)

    resp = requests.get(
        f"{base_url}/api/v4/users?username={args.user}",
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
    )
    resp.raise_for_status()
    users = [u for u in resp.json() if u["username"] == args.user]

    if not users:
        print(f"'{args.user}' not found in GitLab")
        sys.exit(1)

    u = users[0]
    print(f"username : {u['username']}")
    print(f"id       : {u['id']}")
    print(f"name     : {u['name']}")
    print(f"email    : {u.get('email', 'n/a')}")
    print(f"state    : {u['state']}")
    print(f"confirmed: {bool(u.get('confirmed_at'))}")


if __name__ == "__main__":
    main()
