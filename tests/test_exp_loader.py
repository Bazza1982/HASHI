from exp.loader import ExpStore


def test_exp_store_lists_barry_office_desktop():
    store = ExpStore()

    assert "barry/office_desktop" in store.list_ids()


def test_exp_store_loads_manifest_and_playbook():
    store = ExpStore()

    manifest = store.get_manifest("barry/office_desktop")
    playbook = store.get_playbook("barry/office_desktop", "powerpoint")

    assert manifest["type"] == "exp"
    assert manifest["owner"] == "barry"
    assert "context-specific" in manifest["summary"]
    assert "PowerPoint EXP" in playbook
    assert "large pasted text blocks" in playbook
