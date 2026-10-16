"""Custom endpoint API choices survive saving and reopening the editor."""
from services.text_llm import get_profile, upsert_custom_profile
from ui_qt.dialogs.text_endpoint_dialog import TextEndpointDialog


def test_endpoint_editor_saves_and_restores_responses():
    dialog = TextEndpointDialog()
    assert dialog.protocol_combo.currentData() == "chat"
    dialog.name_edit.setText("Gateway")
    dialog.url_edit.setText("https://gateway.test/v1/")
    dialog.protocol_combo.setCurrentIndex(dialog.protocol_combo.findData("responses"))
    dialog._save()
    payload = dialog.result_payload()
    assert payload["protocol"] == "responses"
    profile = upsert_custom_profile({}, **payload)
    restored = TextEndpointDialog(profile)
    assert restored.protocol_combo.currentData() == "responses"
    restored._save()
    assert restored.result_payload()["protocol"] == "responses"
    dialog.close()
    restored.close()


def test_local_preset_editor_changes_only_address_and_key_variable():
    dialog = TextEndpointDialog(get_profile("llama_cpp", {}))
    assert dialog.windowTitle() == "llama.cpp server"
    assert dialog.url_edit.text() == "http://127.0.0.1:8080/v1"
    assert dialog.name_edit.parent() is None
    assert dialog.protocol_combo.parent() is None
    dialog.url_edit.setText("http://user:pw@127.0.0.1:8081")
    dialog._save()
    assert dialog.result_payload() is None
    assert "credentials" in dialog.error_label.text()
    dialog.url_edit.setText("http://127.0.0.1:8081")
    dialog.env_edit.setText("LLAMA_SERVER_KEY")
    dialog._save()
    payload = dialog.result_payload()
    assert payload["base_url"] == "http://127.0.0.1:8081/v1"
    assert payload["api_key_env"] == "LLAMA_SERVER_KEY"
    assert payload["id"] == "llama_cpp"
    dialog.close()
