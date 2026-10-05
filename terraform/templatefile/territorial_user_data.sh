#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/prisma-bootstrap.log /dev/console) 2>&1
umask 077
dnf -y install dnf-plugins-core curl git python3 firewalld sudo
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io
systemctl enable --now docker
systemctl stop firewalld >/dev/null 2>&1 || true
firewall-offline-cmd --zone=public --add-port=8081/tcp
systemctl enable --now firewalld
install -d -m 0700 /opt/prisma
install -d -m 0700 -o 65534 -g 65534 /opt/prisma/native-cache
git clone --filter=blob:none '${source_repo_url}' /opt/prisma/source
git -C /opt/prisma/source checkout --detach '${source_commit_sha}'
test "$(git -C /opt/prisma/source rev-parse HEAD)" = '${source_commit_sha}'
RELEASE=$(git -C /opt/prisma/source describe --tags --exact-match '${source_commit_sha}')
IMAGE=$(python3 /opt/prisma/source/scripts/load_release_image.py --release "$RELEASE" --commit '${source_commit_sha}' --component territorial-viewer)
cat >/usr/local/sbin/prisma-release-update <<'EOF'
#!/bin/sh
exec python3 /opt/prisma/source/scripts/territorial_release_update.py "$@"
EOF
chmod 0755 /usr/local/sbin/prisma-release-update
cat >/etc/sudoers.d/102-prisma-release-update <<'EOF'
ocarun ALL=(root) NOPASSWD: /usr/local/sbin/prisma-release-update *
EOF
chmod 0440 /etc/sudoers.d/102-prisma-release-update
visudo -cf /etc/sudoers.d/102-prisma-release-update
cat >/opt/prisma/runtime.env <<'EOF'
TERRITORIAL_MODE=oci
TERRITORIAL_ADMIN_URL=http://${admin_private_ip}:8000
PRISMA_MODE=oci
PRISMA_ADMIN_URL=http://${admin_private_ip}:8000
OCI_REGION=${region}
TERRITORIAL_BUCKET=${bucket_name}
TERRITORIAL_NAMESPACE=${objectstorage_namespace}
TERRITORIAL_SNAPSHOT_KEY=04_gold/prisma/current.json
TERRITORIAL_AGENT_ENDPOINT_KEY=.control/prisma/agent.json
PRISMA_BUCKET=${bucket_name}
PRISMA_NAMESPACE=${objectstorage_namespace}
PRISMA_SNAPSHOT_KEY=04_gold/prisma/current.json
PRISMA_AGENT_ENDPOINT_KEY=.control/prisma/agent.json
EOF
docker run -d --name prisma-viewer --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --security-opt no-new-privileges:true --cap-drop ALL \
  -v /opt/prisma/native-cache:/app/.upstream/.gev-cache:rw,z \
  --env-file /opt/prisma/runtime.env -p 8081:8081 "$IMAGE"
for attempt in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:8081/health; then
    touch /opt/prisma/ready
    exit 0
  fi
  sleep 5
done
exit 1
