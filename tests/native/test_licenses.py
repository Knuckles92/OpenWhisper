"""Native packages retain notices without exporting installation metadata."""
from pathlib import PurePosixPath

from scripts import collect_rune_licenses as licenses


def test_python_notices_exclude_private_metadata_and_outside_paths(tmp_path, monkeypatch):
    installed = tmp_path / "site-packages"
    files = {
        "example-1.dist-info/licenses/LICENSE": "Upstream license © authors\n",
        "example-1.dist-info/licenses/AUTHORS.rst": "Attribution\n",
        "example-1.dist-info/direct_url.json": "private-install-token",
        "example-1.dist-info/METADATA": "Private build metadata",
    }
    for name, content in files.items():
        path = installed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    outside = tmp_path / "LICENSE"
    outside.write_text("Outside the installed distribution", encoding="utf-8")

    record_files = [*(PurePosixPath(name) for name in files), PurePosixPath("../LICENSE")]

    class Distribution:
        metadata = {"Name": "example", "Version": "1"}
        version = "1"
        files = record_files

        def locate_file(self, name):
            return installed / str(name)

    monkeypatch.setattr(licenses.metadata, "distributions", lambda: [Distribution()])
    destination = tmp_path / "notices"
    index = licenses.python_licenses(destination)
    copied = list(destination.rglob("*"))
    assert {path.name for path in copied if path.is_file()} == {"LICENSE", "AUTHORS.rst"}
    assert "private-install-token" not in repr(index)
    assert (destination / index[0]["files"][0]).read_text(encoding="utf-8") in files.values()


def test_rune_inherits_only_its_checkout_workspace_license(tmp_path):
    checkout = tmp_path / "cargo/git/checkouts/rune-hash/781e2b6"
    crate = checkout / "crates/rune"
    crate.mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname="rune"\nversion="0.0.1"\n', encoding="utf-8")
    (checkout / "Cargo.toml").write_text('[workspace]\nmembers=["crates/rune"]\n', encoding="utf-8")
    (checkout / "LICENSE").write_text("Rune MIT license\n", encoding="utf-8")
    (tmp_path / "cargo/LICENSE").write_text("Unrelated Cargo-home file", encoding="utf-8")
    package = {"id": "rune-id", "name": "rune", "version": "0.0.1", "license": "MIT",
               "manifest_path": str(crate / "Cargo.toml"), "source": "git+https://example.invalid/rune#" + "a" * 40}
    metadata = {"workspace_members": [], "resolve": {"nodes": [{"id": "rune-id"}]}, "packages": [package]}
    destination = tmp_path / "notices"
    index = licenses.rust_licenses(destination, metadata)
    assert index[0]["files"] == ["rust/rune-0.0.1/workspace/LICENSE"]
    assert (destination / index[0]["files"][0]).read_text(encoding="utf-8") == "Rune MIT license\n"
    assert index[0]["revision"] == "a" * 40
    assert "https://" not in repr(index)
