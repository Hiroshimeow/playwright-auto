from playwright_auto import chatgpt as chatgpt_facade
from playwright_auto import chatgpt_models


def test_chatgpt_facade_reexports_single_models_owner():
    exported = (
        "ChatGPTState",
        "MessageSnapshot",
        "PageBinding",
        "MessageBaseline",
        "SendReceipt",
        "ChatGPTSnapshot",
        "WaitProbe",
        "PageHealth",
        "capture_message_baseline",
        "new_messages_since",
        "request_marker_from_prompt",
        "exact_prompt_seen",
        "unique_new_user_message",
        "receipt_user_message_seen",
        "all_assistant_turns_for_receipt",
        "assistant_turns_for_receipt",
        "new_assistant_turns",
        "prompt_digest",
        "message_fingerprint",
        "message_content_fingerprint",
        "capture_response_recovery_baseline",
        "merge_response_recovery_baselines",
        "response_is_stale",
        "response_transport_ui_active",
        "response_activity_signature",
        "looks_incomplete_response",
        "extract_session_id",
        "validate_page_role",
        "classify_chatgpt_state",
        "recent_assistant_messages",
        "recent_assistant_turns",
    )

    for name in exported:
        assert getattr(chatgpt_facade, name) is getattr(chatgpt_models, name)


def test_send_receipt_round_trip_uses_extracted_model_types():
    prompt = "phase-2"
    baseline = chatgpt_models.MessageBaseline(
        message_ids=frozenset({"before"}),
        turn_ids=frozenset({"turn-before"}),
        assistant_turn_ids=frozenset(),
        user_message_ids=frozenset({"before"}),
    )
    receipt = chatgpt_models.SendReceipt(
        prompt=prompt,
        prompt_sha256=chatgpt_models.prompt_digest(prompt),
        binding=chatgpt_models.PageBinding(page_id="page-1", role="PLAN"),
        baseline=baseline,
        attempts=1,
        accepted_via="exact_user_message",
        session_id_before="session-1",
        user_message_id="user-1",
        user_turn_id="turn-user-1",
        conversation_id="conversation-1",
    )

    restored = chatgpt_facade.SendReceipt.from_dict(receipt.to_dict())

    assert restored == receipt
    assert type(restored) is chatgpt_models.SendReceipt


def test_persistent_logical_baseline_ignores_virtualized_history_remount():
    baseline = chatgpt_models.capture_message_baseline(
        (), ("group:user:old", "group:assistant:old")
    )
    remounted_old = chatgpt_models.MessageSnapshot(
        "user", "DOM:user:group:user:old", "group:user:old", "old manual history", ()
    )
    exact_new = chatgpt_models.MessageSnapshot(
        "user", "u-new", "group:user:new", "automation prompt", ()
    )

    assert chatgpt_models.unique_new_user_message((remounted_old,), baseline) is None
    assert chatgpt_models.unique_new_user_message(
        (remounted_old, exact_new), baseline
    ) == exact_new


def test_capture_baseline_persists_rendered_turn_ids_without_extra_snapshot_metadata():
    old = chatgpt_models.MessageSnapshot(
        "user", "real-old-node", "stable-old-turn", "old history", ()
    )

    baseline = chatgpt_models.capture_message_baseline((old,))
    remounted = chatgpt_models.MessageSnapshot(
        "user", "DOM:user:stable-old-turn", "stable-old-turn", "old history", ()
    )

    assert "stable-old-turn" in baseline.logical_turn_ids
    assert chatgpt_models.unique_new_user_message((remounted,), baseline) is None
    assert (
        chatgpt_models.MessageBaseline.from_dict(baseline.to_dict()).logical_turn_ids
        == baseline.logical_turn_ids
    )
