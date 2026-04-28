#!/usr/bin/env python3
"""
Bootstrap resources into Dev Spaces user namespaces.

This script is intended to run repeatedly, for example from a CronJob. It:
  - discovers Dev Spaces namespaces created by Che
  - optionally renders plain YAML manifests with simple per-user substitutions
  - optionally renders a Helm chart into plain manifests
  - applies the rendered resources into each namespace
  - annotates the namespace when reconciliation succeeds

Template variables available in YAML files:
  ${USERNAME}                  Dev Spaces username from che.eclipse.org/username
  ${NAMESPACE}                 Namespace name, for example pk-devspaces
  ${DEVSPACES_NAMESPACE}       Same as ${NAMESPACE}
  ${DEVSPACES_USER_NAMESPACE}  Same as ${NAMESPACE}

Usage:
  python3 bootstrap-devspaces-namespaces.py --manifests-dir ./bootstrap-manifests
  python3 bootstrap-devspaces-namespaces.py --helm-chart-dir ./helm --helm-release-name minio
  python3 bootstrap-devspaces-namespaces.py --helm-chart-dir ./helm --helm-release-name minio --user pk
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
    if not manifests_dir:
        return []
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
    if not payload.strip():
        return
    if dry_run:
        print(f"  WOULD apply rendered manifests to namespace {namespace_name}")
        return
    run_oc(["apply", "-f", "-"], input_text=payload)


def render_helm_chart(chart_dir, release_name, namespace_name):
    if not os.path.isdir(chart_dir):
        raise FileNotFoundError(f"helm chart directory not found: {chart_dir}")
    result = subprocess.run(
        ["helm", "template", release_name, chart_dir, "--namespace", namespace_name],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def bootstrap_namespace(
    namespace,
    files,
    helm_chart_dir,
    helm_release_name,
    annotation_key,
    annotation_value,
    dry_run,
):
    payload_parts = []
    if files:
        payload_parts.append(build_manifest_payload(files, namespace).strip())
    if helm_chart_dir:
        payload_parts.append(
            render_helm_chart(helm_chart_dir, helm_release_name, namespace.name).strip()
        )
    payload = "\n---\n".join(part for part in payload_parts if part) + "\n"
    apply_payload(namespace.name, payload, dry_run)
    annotate_namespace(namespace.name, annotation_key, annotation_value, dry_run)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifests-dir",
        default=None,
        help="Directory containing YAML manifests to apply into each Dev Spaces namespace.",
    )
    parser.add_argument(
        "--helm-chart-dir",
        default=None,
        help="Directory containing a Helm chart to render into each Dev Spaces namespace.",
    )
    parser.add_argument(
        "--helm-release-name",
        default="workspace-bootstrap",
        help="Helm release name used when rendering the chart. Default: workspace-bootstrap",
    )
    parser.add_argument(
        "--selector",
        default=DEFAULT_SELECTOR,
        help=f"Namespace label selector. Default: {DEFAULT_SELECTOR}",
    )
    parser.add_argument(
        "--annotation-key",
        default=DEFAULT_BOOTSTRAP_ANNOTATION,
        help=f"Namespace annotation written after a successful reconciliation. Default: {DEFAULT_BOOTSTRAP_ANNOTATION}",
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
        "--dry-run",
        action="store_true",
        help="Print planned actions without applying or annotating.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    files = collect_yaml_files(args.manifests_dir)
    if not files and not args.helm_chart_dir:
        raise ValueError("at least one of --manifests-dir or --helm-chart-dir is required")
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

        action = "RECONCILE"
        if namespace.bootstrap_state != args.annotation_value:
            action = "BOOTSTRAP"
        print(f"{action} {namespace.name} (user={namespace.username})")
        bootstrap_namespace(
            namespace,
            files,
            args.helm_chart_dir,
            args.helm_release_name,
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
