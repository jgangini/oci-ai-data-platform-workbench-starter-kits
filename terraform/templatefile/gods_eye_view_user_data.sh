#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/gods-eye-view-bootstrap.log /dev/console) 2>&1
umask 077
dnf -y install dnf-plugins-core curl git python3 firewalld sudo
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io
systemctl enable --now docker
systemctl stop firewalld >/dev/null 2>&1 || true
firewall-offline-cmd --zone=public --add-port=8081/tcp
systemctl enable --now firewalld
# The viewer updater preserves the current installation and its native cache.
install -d -m 0700 /opt/gods-eye-view
install -d -m 0700 -o 65534 -g 65534 /opt/gods-eye-view/native-cache
git clone --filter=blob:none '${source_repo_url}' /opt/gods-eye-view/source
git -C /opt/gods-eye-view/source checkout --detach '${source_commit_sha}'
test "$(git -C /opt/gods-eye-view/source rev-parse HEAD)" = '${source_commit_sha}'
RELEASE=$(git -C /opt/gods-eye-view/source describe --tags --exact-match '${source_commit_sha}')
# CI verifies upstream.json and integration patch hashes before building this immutable image.
# VM2 installs that artifact; no upstream build or mutable branch runs during bootstrap.
printf "%s\n" "God's Eye View VM2: release $RELEASE (${source_commit_sha}), built from pinned upstream and verified local patches."
IMAGE=$(python3 /opt/gods-eye-view/source/scripts/release/load_release_image.py --release "$RELEASE" --commit '${source_commit_sha}' --component gods-eye-view)
cat >/usr/local/sbin/gods-eye-view-release-update <<'EOF'
#!/bin/sh
exec python3 /opt/gods-eye-view/source/scripts/release/gods_eye_view_release_update.py "$@"
EOF
chmod 0755 /usr/local/sbin/gods-eye-view-release-update
cat >/etc/sudoers.d/102-gods-eye-view-release-update <<'EOF'
ocarun ALL=(root) NOPASSWD: /usr/local/sbin/gods-eye-view-release-update *
EOF
chmod 0440 /etc/sudoers.d/102-gods-eye-view-release-update
visudo -cf /etc/sudoers.d/102-gods-eye-view-release-update
cat >/opt/gods-eye-view/runtime.env <<'EOF'
GODS_EYE_VIEW_MODE=oci
GODS_EYE_VIEW_ADMIN_URL=http://${admin_private_ip}:8000
OCI_REGION=${region}
GODS_EYE_VIEW_BUCKET=${bucket_name}
GODS_EYE_VIEW_NAMESPACE=${objectstorage_namespace}
GODS_EYE_VIEW_SNAPSHOT_KEY=04_gold/prisma/current.json
GODS_EYE_VIEW_AGENT_ENDPOINT_KEY=.control/prisma/agent.json
EOF
docker run -d --name gods-eye-view --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --security-opt no-new-privileges:true --cap-drop ALL \
  -v /opt/gods-eye-view/native-cache:/app/.upstream/.gev-cache:rw,z \
  --env-file /opt/gods-eye-view/runtime.env -p 8081:8081 "$IMAGE"
for attempt in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:8081/health; then
    touch /opt/gods-eye-view/ready
    exit 0
  fi
  sleep 5
done
exit 1
