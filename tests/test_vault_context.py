"""The notes vault feeds the conversation: profile notes always, the notes a request is about, its own bookmark."""

from jarvis import config
from jarvis.memory import vault


def _vault(tmp_path):
    (tmp_path / "Sam.md").write_text("---\ntype: person\naliases:\n  - the user\n---\n# Sam\nSam is in 8th grade.\n", "utf-8")
    (tmp_path / "Preferences.md").write_text("# Preferences\n- Short answers.\n", "utf-8")
    (tmp_path / "Robot car.md").write_text("---\naliases: [the car, rc car]\n---\n# Robot car\n- Camera is mounted sideways.\n", "utf-8")
    (tmp_path / "Desk setup.md").write_text("# Desk setup\n- Two monitors.\n", "utf-8")
    (tmp_path / ".jarvis-keeper.json").write_text('{"last_id": 8722}', "utf-8")     # another app's bookmark
    config.store.update(vault_path=str(tmp_path), user_name="Sam")


def test_profile_notes_go_into_every_prompt(tmp_path):
    _vault(tmp_path)
    try:
        p = vault.for_prompt()
        assert "Sam is in 8th grade" in p and "Short answers" in p and "Robot car" not in p
    finally:
        config.store.update(vault_path="", user_name="")


def test_the_notes_a_request_is_about_come_along(tmp_path):
    _vault(tmp_path)
    try:
        assert "sideways" in vault.relevant("is the robot car camera fixed yet?")
        assert "sideways" in vault.relevant("drive the rc car forward")              # by alias
        assert "Two monitors" in vault.relevant("change my desk setup")
        assert vault.relevant("what's the weather like") == ""
    finally:
        config.store.update(vault_path="", user_name="")


def test_keeper_ignores_another_apps_bookmark(tmp_path):
    _vault(tmp_path)
    try:
        assert vault.keeper_state().get("last_id", 0) == 0                          # not v1's 8722
        vault.save_keeper_state(last_id=5)
        assert vault.keeper_state()["last_id"] == 5
        assert '"last_id": 8722' in (tmp_path / ".jarvis-keeper.json").read_text("utf-8")   # left alone
    finally:
        config.store.update(vault_path="", user_name="")
