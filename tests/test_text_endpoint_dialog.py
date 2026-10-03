"""Custom endpoint API choices survive saving and reopening the editor."""
from services.text_llm import upsert_custom_profile
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
