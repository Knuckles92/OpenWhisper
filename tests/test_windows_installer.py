"""Static contracts for the Windows Inno Setup upgrade path."""

from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "installer" / "OpenWhisper.iss"


def test_existing_install_uses_condensed_in_place_upgrade():
    script = INSTALLER.read_text(encoding="utf-8")
    assert "AppId={{CA36AD0A-13B9-4737-87AD-ADB54A28EFC9}" in script
    assert "function IsExistingInstall(): Boolean;" in script
    assert "RegKeyExists(HKEY_CURRENT_USER, UninstallKey)" in script
    assert "RegKeyExists(HKEY_LOCAL_MACHINE, UninstallKey)" in script
    assert "function ShouldSkipPage(PageID: Integer): Boolean;" in script
    for page in (
        "wpWelcome",
        "wpLicense",
        "wpSelectDir",
        "wpSelectProgramGroup",
        "wpSelectTasks",
    ):
        assert f"PageID = {page}" in script
    assert "PageID = wpReady" not in script


def test_upgrade_keeps_user_data_outside_the_replaced_install_tree():
    script = INSTALLER.read_text(encoding="utf-8")
    assert "Type: filesandordirs; Name: \"{app}\\_internal\"" in script
    assert "Type: filesandordirs; Name: \"{localappdata}\\{#AppName}\"" not in script
