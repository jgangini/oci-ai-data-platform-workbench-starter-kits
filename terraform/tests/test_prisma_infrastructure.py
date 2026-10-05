"""Executable release, ingress and optional-capacity boundaries; no cloud access."""
import copy
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import load_release_image as bootstrap
import vm_release_updater as updater
import prisma_release_update as viewer_updater


def release(component="prisma-viewer"):
    return {
        "tag_name": "v2.3.0", "immutable": True, "draft": False, "prerelease": False,
        "assets": [{
            "name": name, "digest": "sha256:" + "a" * 64,
            "browser_download_url": f"{bootstrap.REPOSITORY.removesuffix('.git')}/releases/download/v2.3.0/{name}",
        } for name in bootstrap.COMPONENTS[component]],
    }


@pytest.mark.parametrize("component", bootstrap.COMPONENTS)
def test_bootstrap_requires_immutable_unique_origin_bound_digested_assets(component):
    document = release(component)
    assert len(bootstrap.selected_assets(document, "v2.3.0", component)) == 2
    mutations = [
        lambda item: item.update(immutable=False),
        lambda item: item.update(prerelease=True),
        lambda item: item.update(tag_name="v9.9.9"),
        lambda item: item["assets"].append(item["assets"][0]),
        lambda item: item["assets"][0].update(browser_download_url="https://example.com/image"),
        lambda item: item["assets"][0].update(digest="a" * 64),
    ]
    for mutate in mutations:
        invalid = copy.deepcopy(document)
        mutate(invalid)
        with pytest.raises(ValueError):
            bootstrap.selected_assets(invalid, "v2.3.0", component)


def test_bootstrap_manifest_freezes_component_sha_platform_and_digest():
    manifest = {
        "schema_version": 1, "updater_protocol": 1, "release": "v2.3.0",
        "commit_sha": "b" * 40, "repository": bootstrap.REPOSITORY.removesuffix(".git"),
        "image": {"asset_name": "prisma-viewer-image-amd64.tar.gz", "sha256": "a" * 64,
                  "platform": "linux/amd64", "image_tag": "prisma-viewer:" + "b" * 40},
    }
    arguments = dict(tag="v2.3.0", commit="b" * 40, component="prisma-viewer", digest="a" * 64)
    assert bootstrap.validate_manifest(manifest, **arguments) == manifest["image"]["image_tag"]
    for key, value in (("platform", "linux/arm64"), ("sha256", "c" * 64), ("image_tag", "latest")):
        invalid = copy.deepcopy(manifest)
        invalid["image"][key] = value
        with pytest.raises(ValueError):
            bootstrap.validate_manifest(invalid, **arguments)
    with pytest.raises(ValueError):
        bootstrap.validate_manifest(manifest, **{**arguments, "commit": "c" * 40})


def test_updater_preserves_private_bridge_only_on_active_container(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("PRISMA_ADMIN_BIND=10.10.0.10\n")
    calls = []
    monkeypatch.setattr(updater, "_run", lambda args: calls.append(args))
    updater._run_container(tmp_path, "active", "image", candidate=False)
    updater._run_container(tmp_path, "candidate", "image", candidate=True)
    assert "10.10.0.10:8000:8000" in calls[0]
    assert all(":8000" not in value for value in calls[1])
    (tmp_path / ".env").write_text("PRISMA_ADMIN_BIND=0.0.0.0\n")
    with pytest.raises(RuntimeError, match="invalid_prisma_admin_bind"):
        updater._run_container(tmp_path, "active", "image", candidate=False)


def test_optional_viewer_requires_capacity_for_both_vms():
    sys.path.insert(0, str(ROOT / "terraform"))
    import k_preflight
    report = SimpleNamespace(shape_availabilities=[SimpleNamespace(
        instance_shape="VM.Standard.E5.Flex", availability_status="AVAILABLE", available_count=1,
    )])
    candidates = ["VM.Standard.E5.Flex"]
    assert k_preflight._available_shape(report, candidates) == candidates[0]
    assert k_preflight._available_shape(report, candidates, 2) is None
    report.shape_availabilities[0].available_count = 2
    assert k_preflight._available_shape(report, candidates, 2) == candidates[0]
    # Live Chicago reports AVAILABLE with an omitted count; unknown is not zero.
    report.shape_availabilities[0].available_count = None
    assert k_preflight._available_shape(report, candidates, 2) == candidates[0]
    report.shape_availabilities[0].availability_status = "HARDWARE_NOT_SUPPORTED"
    assert k_preflight._available_shape(report, candidates, 2) is None


def test_active_updater_preserves_acme_webroot_and_shared_certificates(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("")
    original_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: (
        True if str(path).replace("\\", "/") == "/var/lib/letsencrypt" else original_is_dir(path)
    ))
    calls = []
    monkeypatch.setattr(updater, "_run", lambda args: calls.append(args))
    updater._run_container(tmp_path, "active", "image", candidate=False)
    assert "/var/lib/letsencrypt:/var/lib/letsencrypt:ro,z" in calls[0]
    assert any(value.endswith(":/etc/aidp-lab/tls:ro,z") for value in calls[0])
    source = (ROOT / "scripts" / "ensure_public_ip_tls.sh").read_text()
    assert "--preferred-profile shortlived" in source and '--ip-address "$PUBLIC_IP"' in source
    assert source.index("openssl verify") < source.index('mv -f "$TLS/tls.crt.next"') < source.index("nginx -s reload")
    cloud_init = (ROOT / "terraform/templatefile/user_data.sh").read_text()
    assert "retry 6 /usr/local/sbin/aidp-lab-public-tls issue" in cloud_init
    assert "OnCalendar=*-*-* 00,12:00:00" in cloud_init
    assert 'https://$PUBLIC_IP/api/health' in cloud_init


def test_both_reverse_proxies_authenticate_and_overwrite_viewer_identity():
    for name in ("nginx.conf", "nginx.oci-local.conf"):
        proxy = (ROOT / "docker" / name).read_text()
        assert "location = /_prisma_session" in proxy and "internal;" in proxy
        assert "http://127.0.0.1:8000/api/prisma/session" in proxy
        blocks = [block for block in proxy.split("location ") if "proxy_pass http://127.0.0.1:8081" in block]
        assert len(blocks) == 5
        for block in blocks:
            assert "auth_request /_prisma_session;" in block
            assert "proxy_set_header X-PRISMA-User $prisma_user;" in block
            assert 'proxy_set_header X-GEV-Origin "$scheme://$http_host";' in block
            assert ("error_page 401 = @prisma_login;" in block) == block.startswith("= /gods-eye-view/ {")
            if not block.startswith("= /gods-eye-view/ {"):
                assert "error_page 401 = @prisma_unauthorized;" in block
        assert "$http_x_prisma_user" not in proxy
        login = next(block for block in proxy.split("location ") if block.startswith("@prisma_login"))
        assert "return 302 /admin/login?next=/gods-eye-view/;" in login
        unauthorized = next(block for block in proxy.split("location ") if block.startswith("@prisma_unauthorized"))
        assert "default_type application/json;" in unauthorized
        assert 'return 401 \'{"detail":"Viewer session required"}\';' in unauthorized
        audio = next(block for block in blocks if "= /api/prisma/oci-voice/turn" in block)
        assert "client_max_body_size 3m;" in audio
        ordinary = next(block for block in blocks if block.startswith("/api/prisma/"))
        assert "client_max_body_size 1m;" in ordinary


@pytest.mark.parametrize("candidate_ready", [False, True])
def test_viewer_failed_update_keeps_or_restores_current_image(monkeypatch, tmp_path, candidate_ready):
    cache = tmp_path / "native-cache"
    cache.mkdir()
    budget = cache / "provider-budget.json"
    budget.write_text('{"used":3}')
    containers = {viewer_updater.APP: "old"}
    operations = []
    monkeypatch.setattr(viewer_updater, "load", lambda *_: "new")
    monkeypatch.setattr(viewer_updater, "_container_exists", lambda name: name in containers)
    monkeypatch.setattr(viewer_updater, "_remove_container", lambda name: containers.pop(name, None))

    def run_container(_root, name, image, _candidate):
        containers[name] = image

    def run(args):
        operations.append(args[1])
        if args[1] == "rename":
            containers[args[3]] = containers.pop(args[2])

    monkeypatch.setattr(viewer_updater, "run_container", run_container)
    monkeypatch.setattr(viewer_updater, "_run", run)
    monkeypatch.setattr(viewer_updater, "_healthy", lambda url, **_: (
        url.endswith("/health") or (":18081/ready" in url and candidate_ready)
    ))
    with pytest.raises(RuntimeError, match="viewer_.*_not_ready"):
        viewer_updater.update(tmp_path, "v2.3.0", "b" * 40)
    assert containers == {viewer_updater.APP: "old"}
    assert ("stop" in operations) is candidate_ready
    assert not (tmp_path / "release.json").exists()
    assert budget.read_text() == '{"used":3}'


def test_viewer_candidate_and_active_share_persistent_nonroot_cache(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(viewer_updater, "_run", lambda args: calls.append(args))
    for name, candidate in (("candidate", True), ("active", False)):
        viewer_updater.run_container(tmp_path, name, "image", candidate)
    mount = f"{tmp_path / 'native-cache'}:/app/.upstream/.gev-cache:rw,z"
    for install, docker in (calls[:2], calls[2:]):
        assert install == ["install", "-d", "-m", "0700", "-o", "65534", "-g", "65534", str(tmp_path / "native-cache")]
        assert mount in docker and "--read-only" in docker
        assert all(":4173" not in value for value in docker)
    assert "127.0.0.1:18081:8081" in calls[1]
    assert "8081:8081" in calls[3]
    template = (ROOT / "terraform/templatefile/prisma_user_data.sh").read_text()
    assert "-v /opt/prisma/native-cache:/app/.upstream/.gev-cache:rw,z" in template


@pytest.mark.parametrize("spawn_fails", [False, True])
def test_native_supervisor_stops_sibling_when_runtime_exits_or_cannot_start(monkeypatch, tmp_path, spawn_fails):
    monkeypatch.syspath_prepend(str(ROOT / "apps/prisma-viewer/native"))
    monkeypatch.setenv("GEV_PROVIDER_SETTINGS_DIR", str(tmp_path / "provider-settings"))
    spec = importlib.util.spec_from_file_location("native_entrypoint", ROOT / "apps/prisma-viewer/native/entrypoint.py")
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    child = SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append("terminate"),
                            wait=lambda **_: stopped.append("wait"))
    stopped = []
    calls = []

    def spawn(command, *, env):
        calls.append(command)
        if len(calls) == 1:
            assert env["GEV_PROVIDER_REVISION"] == "environment"
            return child
        assert env is None
        if spawn_fails:
            raise OSError("runtime unavailable")
        return SimpleNamespace(poll=lambda: 0, wait=lambda **_: None)

    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    if spawn_fails:
        with pytest.raises(OSError, match="runtime unavailable"):
            supervisor.supervise([["node"], ["python"]])
    else:
        assert supervisor.supervise([["node"], ["python"]]) == 1
    assert stopped == ["terminate", "wait"]
