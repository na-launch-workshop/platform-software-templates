FROM python:3.11-slim

ARG OC_URL=https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz
ARG HELM_VERSION=3.14.3

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl git tar \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sL ${OC_URL} | tar -xz -C /usr/local/bin oc

RUN curl -fsSL https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz \
    | tar -xz -C /usr/local/bin --strip-components=1 linux-amd64/helm

RUN pip install --no-cache-dir requests urllib3 pyyaml

RUN oc version --client && helm version --short
