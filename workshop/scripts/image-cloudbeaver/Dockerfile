FROM dbeaver/cloudbeaver:latest

USER root

# OpenShift runs containers with arbitrary UIDs but always GID 0.
# chgrp to root + group write makes the image work under restricted SCC.
RUN chgrp -R 0 /opt/cloudbeaver && chmod -R g+rwX /opt/cloudbeaver

USER 1001

EXPOSE 8978
