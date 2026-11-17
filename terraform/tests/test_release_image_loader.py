"""Frozen Starter Kits release loading and host runtime boundaries; no cloud access."""
import copy
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts/release"))
import load_release_image as bootstrap
import vm_release_updater as updater


def release(component="aidp-lab"):
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
        "schema_version": 1, "updater_protocol": 2, "deployment_profile": "kits-only", "release": "v2.3.0",
        "commit_sha": "b" * 40, "repository": bootstrap.REPOSITORY.removesuffix(".git"),
        "image": {"asset_name": "aidp-lab-image-amd64.tar.gz", "sha256": "a" * 64,
                  "platform": "linux/amd64", "image_tag": "aidp-lab:" + "b" * 40},
    }
    arguments = dict(tag="v2.3.0", commit="b" * 40, component="aidp-lab", digest="a" * 64)
    assert bootstrap.validate_manifest(manifest, **arguments) == manifest["image"]["image_tag"]
    for key, value in (("platform", "linux/arm64"), ("sha256", "c" * 64), ("image_tag", "latest")):
        invalid = copy.deepcopy(manifest)
        invalid["image"][key] = value
        with pytest.raises(ValueError):
            bootstrap.validate_manifest(invalid, **arguments)
    with pytest.raises(ValueError):
        bootstrap.validate_manifest(manifest, **{**arguments, "commit": "c" * 40})


@pytest.mark.parametrize("published", ["aidp-lab", "gods-eye-view", "partial"])
def test_loader_requires_the_complete_kits_asset_pair(monkeypatch, capsys, published):
    component = "aidp-lab"
    document = release(component)
    if published == "partial":
        document["assets"].pop()
    elif published != component:
        names = ("gods-eye-view-image-amd64.tar.gz", "gods-eye-view-release.json")
        for asset, name in zip(document["assets"], names):
            asset.update(name=name, browser_download_url=f"{bootstrap.REPOSITORY.removesuffix('.git')}/releases/download/v2.3.0/{name}")
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(json.dumps(document).encode()))
    downloaded = []
    def download(_url, destination, digest, _limit):
        downloaded.append(destination.name)
        destination.write_text(json.dumps({"schema_version": 1, "updater_protocol": 2, "deployment_profile": "kits-only", "release": "v2.3.0",
            "commit_sha": "b" * 40, "repository": bootstrap.REPOSITORY.removesuffix(".git"),
            "image": {"asset_name": bootstrap.COMPONENTS[component][0], "sha256": digest,
                      "platform": "linux/amd64", "image_tag": component + ":" + "b" * 40}}))
    monkeypatch.setattr(bootstrap, "_download", download)
    def docker_load(_args, **kwargs):
        print("Loaded image: " + component + ":" + "b" * 40, file=kwargs.get("stdout", sys.stdout))
    monkeypatch.setattr(bootstrap.subprocess, "run", docker_load)
    monkeypatch.setattr(bootstrap, "_image_environment", lambda *_: {
        "APP_RELEASE_TAG": "v2.3.0", "APP_RELEASE_SHA": "b" * 40,
        "APP_REPOSITORY": bootstrap.REPOSITORY.removesuffix(".git"), "APP_DEPLOYMENT_PROFILE": "kits-only",
    })
    monkeypatch.setattr(bootstrap.subprocess, "check_output", lambda *_args, **_kwargs: "linux/amd64")
    if published != component:
        with pytest.raises(ValueError, match="asset set"):
            bootstrap.load("v2.3.0", "b" * 40, component)
        assert downloaded == []
    else:
        assert bootstrap.load("v2.3.0", "b" * 40, component) == component + ":" + "b" * 40
        assert set(downloaded) == set(bootstrap.COMPONENTS[component])
        output = capsys.readouterr()
        assert output.out == "" and "Loaded image:" in output.err


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
    source = (ROOT / "scripts/deployment" / "ensure_public_ip_tls.sh").read_text()
    assert "--preferred-profile shortlived" in source and '--ip-address "$PUBLIC_IP"' in source
    assert source.index("openssl verify") < source.index('mv -f "$TLS/tls.crt.next"') < source.index("nginx -s reload")
    cloud_init = (ROOT / "terraform/templatefile/user_data.sh").read_text()
    assert "retry 6 /usr/local/sbin/aidp-lab-public-tls issue" in cloud_init
    assert "OnCalendar=*-*-* 00,12:00:00" in cloud_init
    assert 'https://$PUBLIC_IP/api/health' in cloud_init


@pytest.mark.parametrize("component", ["gods-eye-view", "territorial-viewer", "prisma-viewer", "ai_data_governance", "ai_market_intelligence"])
def test_other_release_components_are_rejected_before_network(monkeypatch, component):
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("No network for another product"))
    with pytest.raises(ValueError, match="Unsupported release component"):
        bootstrap.load("v5.0.0", "b" * 40, component)


@pytest.mark.parametrize("protocol,profile", [(1, None), (1, "kits-only"), (2, None), (2, "combined")])
def test_loader_rejects_manifests_across_the_release_boundary(protocol, profile):
    manifest = {"schema_version": 1, "updater_protocol": protocol, "deployment_profile": profile,
        "release": "v5.0.0", "commit_sha": "b" * 40, "repository": bootstrap.REPOSITORY.removesuffix(".git"),
        "image": {"asset_name": "aidp-lab-image-amd64.tar.gz", "sha256": "a" * 64,
                  "platform": "linux/amd64", "image_tag": "aidp-lab:" + "b" * 40}}
    with pytest.raises(ValueError, match="manifest"):
        bootstrap.validate_manifest(manifest, tag="v5.0.0", commit="b" * 40, component="aidp-lab", digest="a" * 64)


def test_kits_proxy_exposes_only_the_portal_and_api():
    for name in ("nginx.conf", "nginx.oci-local.conf"):
        source = (ROOT / "docker" / name).read_text()
        assert "location /api/ {" in source
        assert "proxy_pass http://127.0.0.1:8000;" in source
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in source
        assert "proxy_set_header X-Forwarded-Host $http_host;" in source
        assert "location ~ ^/admin/(login|users|settings)$" in source
        assert "try_files $uri $uri/ =404;" in source
        assert "try_files /index.html =404;" in source
        assert "8081" not in source and "auth_request" not in source and "gods-eye" not in source
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text()
    assert "uvicorn app.main:app --host 127.0.0.1 --port 8000" in entrypoint
    assert "GODS_EYE" not in entrypoint
