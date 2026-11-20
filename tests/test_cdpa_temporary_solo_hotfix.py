from pathlib import Path

import playwright_auto.cdpa_cli as cli
from test_cdpa_solo_task import make_store


def test_solo_can_explicitly_request_temporary(tmp_path: Path):
    _, store = make_store(tmp_path)
    state = store.create_task(
        "Temporary work",
        task_id="temporary-work",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
        conversation_mode="temporary",
    )
    assert state["conversation_mode"] == "temporary"
    assert state["conversation_mode_explicit"] is True


def test_solo_cli_sends_temporary_flag(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_post(config, path, payload, **kwargs):
        captured.update(payload)
        return {"ok": True}

    monkeypatch.setattr(cli, "_post", fake_post)
    cli.submit_task(
        object(),
        task="implement",
        repository=tmp_path,
        team="temporary-test",
        new_roles=(),
        new_all=False,
        temporary=True,
        browser_endpoint_id="9212",
        workflow_mode="solo",
        solo_role="DEV",
        goal="implement",
    )
    assert captured["workflow_mode"] == "solo"
    assert captured["temporary"] is True
    assert cli.build_parser().parse_args(["-e", "9212", "-p", "job", "-m", "solo", "--temp"]).temp
