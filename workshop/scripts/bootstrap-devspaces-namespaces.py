#!/usr/bin/env python3
"""
Bootstrap resources into Dev Spaces user namespaces.

This script is intended to run repeatedly, for example from a CronJob. It:
  - discovers Dev Spaces namespaces created by Che
  - skips namespaces already marked as bootstrapped
  - renders YAML manifests with simple per-user substitutions
  - applies the rendered manifests into each namespace
  - annotates the namespace when bootstrapping succeeds

Template variables available in YAML files:
  ${USERNAME}                  Dev Spaces username from che.eclipse.org/username
  ${NAMESPACE}                 Namespace name, for example pk-devspaces
  ${DEVSPACES_NAMESPACE}       Same as ${NAMESPACE}
  ${DEVSPACES_USER_NAMESPACE}  Same as ${NAMESPACE}

Usage:
  python3 bootstrap-devspaces-namespaces.py --manifests-dir ./bootstrap-manifests
  python3 bootstrap-devspaces-namespaces.py --manifests-dir ./bootstrap-manifests --dry-run
  python3 bootstrap-devspaces-namespaces.py --manifests-dir ./bootstrap-manifests --user pk
"""

import argparse
import os
import string
import subprocess
import sys
from dataclasses import dataclass


DEFAULT_SELECTOR = (
    "app.kubernetes.io/component=workspaces-namespace,"
    "app.kubernetes.io/part-of=che.eclipse.org"
)
DEFAULT_BOOTSTRAP_ANNOTATION = "workshop.redhat.com/devspaces-bootstrap"
DEFAULT_BOOTSTRAP_VALUE = "done"


@dataclass
class DevSpacesNamespace:
    name: str
    username: str
    requester: str
    bootstrap_state: str


def run_oc(args, *, input_text=None):
    return subprocess.run(
        ["oc", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )


def list_devspaces_namespaces(selector, annotation_key):
    jsonpath = (
        "{range .items[*]}"
        "{.metadata.name}{'|'}"
        "{.metadata.annotations.che\\.eclipse\\.org/username}{'|'}"
        "{.metadata.annotations.openshift\\.io/requester}{'|'}"
        "{.metadata.annotations." + annotation_key.replace("/", "\\/").replace(".", "\\.") + "}"
        "{'\\n'}"
        "{end}"
    )
    result = run_oc(["get", "ns", "-l", selector, "-o", f"jsonpath={jsonpath}"])
    namespaces = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, username, requester, bootstrap_state = (line.split("|", 3) + ["", "", "", ""])[:4]
        namespaces.append(
            DevSpacesNamespace(
                name=name.strip(),
                username=username.strip(),
                requester=requester.strip(),
                bootstrap_state=bootstrap_state.strip(),
            )
        )
    return namespaces


def collect_yaml_files(manifests_dir):
    if not os.path.isdir(manifests_dir):
        raise FileNotFoundError(f"manifests directory not found: {manifests_dir}")

    files = []
    for root, _, names in os.walk(manifests_dir):
        for name in sorted(names):
            if name.endswith((".yaml", ".yml")):
                files.append(os.path.join(root, name))
    files.sort()
    if not files:
        raise FileNotFoundError(f"no YAML files found under: {manifests_dir}")
    return files


def render_manifest(path, namespace):
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()

    template = string.Template(raw)
    return template.safe_substitute(
        USERNAME=namespace.username,
        NAMESPACE=namespace.name,
        DEVSPACES_NAMESPACE=namespace.name,
        DEVSPACES_USER_NAMESPACE=namespace.name,
    )


def build_manifest_payload(files, namespace):
    rendered_docs = []
    for path in files:
        rendered = render_manifest(path, namespace).strip()
        if not rendered:
            continue
        rendered_docs.append(rendered)
    return "\n---\n".join(rendered_docs) + "\n"


def annotate_namespace(namespace_name, annotation_key, annotation_value, dry_run):
    if dry_run:
        print(f"  WOULD annotate namespace {namespace_name}: {annotation_key}={annotation_value}")
        return
    run_oc(["annotate", "namespace", namespace_name, f"{annotation_key}={annotation_value}", "--overwrite"])


def apply_payload(namespace_name, payload, dry_run):
    if dry_run:
        print(f"  WOULD apply rendered manifests to namespace {namespace_name}")
        return
    run_oc(["apply", "-f", "-"], input_text=payload)


def bootstrap_namespace(namespace, files, annotation_key, annotation_value, dry_run):
    payload = build_manifest_payload(files, namespace)
    apply_payload(namespace.name, payload, dry_run)
    annotate_namespace(namespace.name, annotation_key, annotation_value, dry_run)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifests-dir",
        required=True,
        help="Directory containing YAML manifests to apply into each Dev Spaces namespace.",
    )
    parser.add_argument(
        "--selector",
        default=DEFAULT_SELECTOR,
        help=f"Namespace label selector. Default: {DEFAULT_SELECTOR}",
    )
    parser.add_argument(
        "--annotation-key",
        default=DEFAULT_BOOTSTRAP_ANNOTATION,
        help=f"Namespace annotation used to mark bootstrap completion. Default: {DEFAULT_BOOTSTRAP_ANNOTATION}",
    )
    parser.add_argument(
        "--annotation-value",
        default=DEFAULT_BOOTSTRAP_VALUE,
        help=f"Value written to the bootstrap annotation. Default: {DEFAULT_BOOTSTRAP_VALUE}",
    )
    parser.add_argument(
        "--user",
        action="append",
        default=[],
        help="Only process the given Dev Spaces username. Repeat for multiple users.",
    )
    parser.add_argument(
        "--include-bootstrapped",
        action="store_true",
        help="Re-apply resources even if the bootstrap annotation is already present.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without applying or annotating.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    files = collect_yaml_files(args.manifests_dir)
    user_filter = set(args.user)

    namespaces = list_devspaces_namespaces(args.selector, args.annotation_key)
    if user_filter:
        namespaces = [ns for ns in namespaces if ns.username in user_filter]

    if not namespaces:
        print("No matching Dev Spaces namespaces found.")
        return 0

    processed = 0
    skipped = 0

    for namespace in namespaces:
        if not namespace.username:
            print(f"SKIP {namespace.name}: missing che.eclipse.org/username annotation")
            skipped += 1
            continue

        if (
            namespace.bootstrap_state == args.annotation_value
            and not args.include_bootstrapped
        ):
            print(f"SKIP {namespace.name}: already bootstrapped")
            skipped += 1
            continue

        print(f"BOOTSTRAP {namespace.name} (user={namespace.username})")
        bootstrap_namespace(
            namespace,
            files,
            args.annotation_key,
            args.annotation_value,
            args.dry_run,
        )
        processed += 1

    print(f"\nProcessed: {processed}")
    print(f"Skipped:   {skipped}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            sys.stdout.write(exc.stdout)
        if exc.stderr:
            sys.stderr.write(exc.stderr)
        raise SystemExit(exc.returncode)
