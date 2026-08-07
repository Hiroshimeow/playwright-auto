from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import playwright_auto.cdpa_bootstraps as bootstrap_module
from playwright_auto.cdpa_bootstraps import BootstrapCatalog, normalize_bootstrap_record


CONVERSATION_ID = "6a7454dd-2aa4-83e8-9531-18a1e82551d0"
MESSAGE_ID = "74c3d85a-f0ba-4412-bc26-996134bb49ab"
CONVERSATION_ID_2 = "134f7ed8-54bf-4caf-a15e-8d35aa0de283"
MESSAGE_ID_2 = "7780c60a-1e29-4b05-b2bd-268383fcfe7b"


def valid_record(bootstrap_id: str = "general-team", **overrides):
    record = {
        "bootstrap_id": bootstrap_id,
        "name": "General team bootstrap",
        "description": "Shared bootstrap anchor",
        "conversation_id": CONVERSATION_ID,
        "terminal_assistant_message_id": MESSAGE_ID,
        "enabled": True,
        "tags": ["general"],
        "created_at": "2026-08-06T09:30:00+00:00",
        "updated_at": "2026-08-06T09:30:00+00:00",
    }
    record.update(overrides)
    return record


def test_missing_catalog_is_empty_without_creating_file(tmp_path: Path):
    catalog = BootstrapCatalog(tmp_path)

    assert catalog.list() == []
    assert catalog.get("missing") is None
    assert not (tmp_path / ".runtime" / "cdpa-bootstraps.json").exists()


def test_normalize_legacy_bootstrap_record_migrates_to_unified_donor_defaults():
    normalized = normalize_bootstrap_record(valid_record())

    assert normalized == {
        "bootstrap_id": "general-team",
        "name": "General team bootstrap",
        "description": "Shared bootstrap anchor",
        "source_conversation_id": CONVERSATION_ID,
        "prewarm_prompt": None,
        "max_backups": 7,
        "donors": [
            {"conversation_id": CONVERSATION_ID, "assistant_message_id": MESSAGE_ID},
        ],
        "enabled": True,
        "tags": ["general"],
        "created_at": "2026-08-06T09:30:00+00:00",
        "updated_at": "2026-08-06T09:30:00+00:00",
    }


def test_upsert_creates_then_get_and_list_return_normalized_record(tmp_path: Path):
    catalog = BootstrapCatalog(tmp_path)
    expected = normalize_bootstrap_record(valid_record())

    assert catalog.upsert(valid_record()) == expected
    assert catalog.get("general-team") == expected
    assert catalog.list() == [expected]

    payload = json.loads((tmp_path / ".runtime" / "cdpa-bootstraps.json").read_text())
    assert payload == {"version": 2, "entries": {"general-team": expected}}


def test_upsert_replaces_exact_entry_preserves_others_and_replay_is_noop(
    tmp_path: Path, monkeypatch
):
    catalog = BootstrapCatalog(tmp_path)
    first = catalog.upsert(valid_record())
    second = catalog.upsert(
        valid_record(
            "review-team",
            conversation_id=CONVERSATION_ID_2,
            terminal_assistant_message_id=MESSAGE_ID_2,
            tags=["review"],
        )
    )
    real_replace = bootstrap_module.os.replace
    replace_calls = []

    def counted_replace(source, target):
        replace_calls.append((source, target))
        return real_replace(source, target)

    monkeypatch.setattr(bootstrap_module.os, "replace", counted_replace)
    replacement = {**first, "description": "Updated anchor"}

    assert catalog.upsert(replacement) == replacement
    assert catalog.get("review-team") == second
    assert len(replace_calls) == 1

    assert catalog.upsert(replacement) == replacement
    assert len(replace_calls) == 1
    assert catalog.list() == [replacement, second]


def test_disable_updates_only_target_and_is_idempotent(tmp_path: Path, monkeypatch):
    catalog = BootstrapCatalog(tmp_path)
    original = catalog.upsert(valid_record())
    other = catalog.upsert(
        valid_record(
            "review-team",
            conversation_id=CONVERSATION_ID_2,
            terminal_assistant_message_id=MESSAGE_ID_2,
        )
    )

    disabled = catalog.disable("general-team")
    assert disabled is not None
    assert disabled["enabled"] is False
    assert datetime.fromisoformat(disabled["updated_at"]) >= datetime.fromisoformat(
        original["updated_at"]
    )
    assert {key: value for key, value in disabled.items() if key not in {"enabled", "updated_at"}} == {
        key: value for key, value in original.items() if key not in {"enabled", "updated_at"}
    }
    assert catalog.get("review-team") == other

    def reject_replace(*_args):
        raise AssertionError("already-disabled record must not be rewritten")

    monkeypatch.setattr(bootstrap_module.os, "replace", reject_replace)
    assert catalog.disable("general-team") == disabled


def test_disable_and_delete_missing_are_noops_without_catalog_file(tmp_path: Path):
    catalog = BootstrapCatalog(tmp_path)

    assert catalog.disable("missing") is None
    assert catalog.delete("missing") is False
    assert not (tmp_path / ".runtime" / "cdpa-bootstraps.json").exists()


def test_delete_removes_only_exact_entry(tmp_path: Path):
    catalog = BootstrapCatalog(tmp_path)
    catalog.upsert(valid_record())
    other = catalog.upsert(
        valid_record(
            "review-team",
            conversation_id=CONVERSATION_ID_2,
            terminal_assistant_message_id=MESSAGE_ID_2,
        )
    )

    assert catalog.delete("general-team") is True
    assert catalog.get("general-team") is None
    assert catalog.list() == [other]
    assert catalog.delete("general-team") is False


def test_malformed_existing_catalog_fails_closed_on_read_and_mutation(tmp_path: Path):
    valid = normalize_bootstrap_record(valid_record())
    malformed_payloads = [
        b"{not-json",
        json.dumps({"entries": {}}).encode(),
        json.dumps({"version": 3, "entries": {}}).encode(),
        json.dumps({"version": 1, "entries": []}).encode(),
        b'{"version":1,"version":1,"entries":{}}',
        json.dumps({"version": 2, "entries": {"wrong-key": valid}}).encode(),
    ]

    for index, raw in enumerate(malformed_payloads):
        root = tmp_path / str(index)
        runtime = root / ".runtime"
        runtime.mkdir(parents=True)
        path = runtime / "cdpa-bootstraps.json"
        path.write_bytes(raw)
        catalog = BootstrapCatalog(root)

        with pytest.raises(ValueError):
            catalog.list()
        before = path.read_bytes()
        with pytest.raises(ValueError):
            catalog.upsert(valid_record("new-team"))
        assert path.read_bytes() == before


def test_bootstraps_may_share_a_donor_without_single_anchor_uniqueness(tmp_path: Path):
    catalog = BootstrapCatalog(tmp_path)
    first = catalog.upsert(valid_record())
    second = catalog.upsert(valid_record("other-team"))

    assert catalog.list() == [first, second]
    assert first["donors"] == second["donors"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"bootstrap_id": " bad"},
        {"bootstrap_id": "a" * 129},
        {"name": ""},
        {"name": "n" * 201},
        {"name": "bad\nname"},
        {"description": "d" * 2001},
        {"description": "bad\tdescription"},
        {"conversation_id": "WEB:temporary"},
        {"conversation_id": f"https://chatgpt.com/c/{CONVERSATION_ID}"},
        {"conversation_id": CONVERSATION_ID.upper()},
        {"conversation_id": CONVERSATION_ID.replace("-", "")},
        {"conversation_id": "not-a-uuid"},
        {"terminal_assistant_message_id": MESSAGE_ID.upper()},
        {"terminal_assistant_message_id": MESSAGE_ID.replace("-", "")},
        {"terminal_assistant_message_id": "not-a-uuid"},
        {"enabled": 1},
        {"tags": ("general",)},
        {"tags": ["dup", "dup"]},
        {"tags": [""]},
        {"tags": ["x" * 65]},
        {"tags": [str(index) for index in range(33)]},
        {"created_at": "2026-08-06T09:30:00"},
        {"created_at": "not-a-time"},
        {"updated_at": None},
        {"expires_at": "2026-08-06T09:30:00"},
        {"last_verified_at": "not-a-time"},
        {"source_fingerprints": []},
        {"source_fingerprints": {"AGENTS.md": "A" * 64}},
        {"source_fingerprints": {"AGENTS.md": "a" * 63}},
        {"source_fingerprints": {"AGENTS.md": "g" * 64}},
        {"source_fingerprints": {"": "a" * 64}},
        {"source_fingerprints": {"bad\nkey": "a" * 64}},
        {"source_fingerprints": {"x" * 257: "a" * 64}},
        {"source_fingerprints": {str(index): "a" * 64 for index in range(33)}},
        {"unexpected": True},
    ],
)
def test_invalid_records_are_rejected(overrides):
    with pytest.raises(ValueError):
        normalize_bootstrap_record(valid_record(**overrides))


def test_non_mapping_record_is_rejected():
    with pytest.raises(ValueError):
        normalize_bootstrap_record([])  # type: ignore[arg-type]


def test_legacy_keeper_fields_are_validated_then_dropped_during_migration():
    normalized = normalize_bootstrap_record(
        valid_record(
            created_at="2026-08-06T18:30:00+09:00",
            updated_at="2026-08-06T18:31:00+09:00",
            expires_at="2026-08-07T18:30:00+09:00",
            last_verified_at="2026-08-06T18:32:00+09:00",
            source_fingerprints={
                "AGENTS.md": "a" * 64,
                "LEARNING.md": "b" * 64,
            },
        )
    )

    assert normalized["created_at"] == "2026-08-06T09:30:00+00:00"
    assert normalized["updated_at"] == "2026-08-06T09:31:00+00:00"
    assert normalized["source_conversation_id"] == CONVERSATION_ID
    assert normalized["donors"] == [
        {"conversation_id": CONVERSATION_ID, "assistant_message_id": MESSAGE_ID}
    ]
    assert "expires_at" not in normalized
    assert "last_verified_at" not in normalized
    assert "source_fingerprints" not in normalized


def test_list_and_get_return_detached_values(tmp_path: Path):
    catalog = BootstrapCatalog(tmp_path)
    catalog.upsert(valid_record(tags=["general"]))

    listed = catalog.list()
    listed[0]["tags"].append("mutated")
    listed[0]["donors"].append(
        {"conversation_id": CONVERSATION_ID_2, "assistant_message_id": MESSAGE_ID_2}
    )
    fetched = catalog.get("general-team")
    assert fetched is not None
    fetched["tags"].append("also-mutated")
    fetched["donors"].clear()

    assert catalog.get("general-team") == normalize_bootstrap_record(valid_record(tags=["general"]))


def test_invalid_requested_id_is_rejected_by_direct_operations(tmp_path: Path):
    catalog = BootstrapCatalog(tmp_path)

    for operation in (catalog.get, catalog.disable, catalog.delete):
        with pytest.raises(ValueError):
            operation("bad id")


def test_atomic_replace_failure_preserves_previous_catalog_and_cleans_temp(
    tmp_path: Path, monkeypatch
):
    catalog = BootstrapCatalog(tmp_path)
    original = catalog.upsert(valid_record())
    path = tmp_path / ".runtime" / "cdpa-bootstraps.json"
    before = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(bootstrap_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        catalog.upsert({**original, "description": "must not persist"})

    assert path.read_bytes() == before
    assert list(path.parent.glob("cdpa-bootstraps.json.*.tmp")) == []
    assert catalog.get("general-team") == original


def donor_record(bootstrap_id: str = "donor-team", **overrides):
    record = {
        "bootstrap_id": bootstrap_id,
        "name": "Donor team bootstrap",
        "description": "Reusable task-neutral context",
        "source_conversation_id": CONVERSATION_ID,
        "prewarm_prompt": None,
        "max_backups": 2,
        "donors": [
            {"conversation_id": CONVERSATION_ID, "assistant_message_id": MESSAGE_ID},
        ],
        "enabled": True,
        "tags": ["general"],
        "created_at": "2026-08-06T09:30:00+00:00",
        "updated_at": "2026-08-06T09:30:00+00:00",
    }
    record.update(overrides)
    return record


def test_unified_donor_record_has_no_keeper_health_or_single_anchor_fields():
    normalized = normalize_bootstrap_record(donor_record())

    assert normalized["source_conversation_id"] == CONVERSATION_ID
    assert normalized["prewarm_prompt"] is None
    assert normalized["max_backups"] == 2
    assert normalized["donors"] == [
        {"conversation_id": CONVERSATION_ID, "assistant_message_id": MESSAGE_ID}
    ]
    for removed in (
        "conversation_id",
        "terminal_assistant_message_id",
        "expires_at",
        "last_verified_at",
        "source_fingerprints",
        "failure_count",
        "kind",
    ):
        assert removed not in normalized


def test_bootstrap_requires_source_or_prewarm_and_caps_donor_list():
    with pytest.raises(ValueError, match="source or prewarm_prompt"):
        normalize_bootstrap_record(
            donor_record(source_conversation_id=None, prewarm_prompt=None, donors=[])
        )

    with pytest.raises(ValueError, match="max_backups"):
        normalize_bootstrap_record(donor_record(max_backups=0))

    with pytest.raises(ValueError, match="donors"):
        normalize_bootstrap_record(
            donor_record(
                max_backups=1,
                donors=[
                    {"conversation_id": CONVERSATION_ID, "assistant_message_id": MESSAGE_ID},
                    {"conversation_id": CONVERSATION_ID_2, "assistant_message_id": MESSAGE_ID_2},
                ],
            )
        )


def test_catalog_migrates_v1_anchor_to_unified_donor_runtime(tmp_path: Path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "cdpa-bootstraps.json").write_text(
        json.dumps({"version": 1, "entries": {"general-team": valid_record()}}),
        encoding="utf-8",
    )

    migrated = BootstrapCatalog(tmp_path).get("general-team")

    assert migrated is not None
    assert migrated["source_conversation_id"] == CONVERSATION_ID
    assert migrated["prewarm_prompt"] is None
    assert migrated["max_backups"] == 7
    assert migrated["donors"] == [
        {"conversation_id": CONVERSATION_ID, "assistant_message_id": MESSAGE_ID}
    ]
    assert "expires_at" not in migrated


def test_catalog_promotes_dedupes_caps_and_removes_donors_atomically(tmp_path: Path):
    catalog = BootstrapCatalog(tmp_path)
    catalog.upsert(donor_record(max_backups=2))
    newest = {"conversation_id": CONVERSATION_ID_2, "assistant_message_id": MESSAGE_ID_2}
    third = {
        "conversation_id": "55555555-5555-4555-8555-555555555555",
        "assistant_message_id": "66666666-6666-4666-8666-666666666666",
    }

    promoted = catalog.add_donor("donor-team", newest)
    assert promoted["donors"] == [newest, donor_record()["donors"][0]]
    replayed = catalog.add_donor("donor-team", newest)
    assert replayed["donors"] == promoted["donors"]
    capped = catalog.add_donor("donor-team", third)
    assert capped["donors"] == [third, newest]

    removed = catalog.remove_donor("donor-team", third)
    assert removed["donors"] == [newest]


def test_source_url_or_uuid_normalization_is_canonical():
    from playwright_auto.cdpa_bootstraps import normalize_bootstrap_source

    assert normalize_bootstrap_source(CONVERSATION_ID) == CONVERSATION_ID
    assert normalize_bootstrap_source(f"https://chatgpt.com/c/{CONVERSATION_ID}") == CONVERSATION_ID
    assert normalize_bootstrap_source(f"https://www.chatgpt.com/c/{CONVERSATION_ID}?x=1") == CONVERSATION_ID
    with pytest.raises(ValueError):
        normalize_bootstrap_source("WEB:temporary")
