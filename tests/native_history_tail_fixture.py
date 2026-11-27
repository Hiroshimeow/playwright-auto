"""Synthetic identity adapter for the real native-tail structural fixture."""
from pathlib import Path
import copy
import json


def native_tail_for(snapshot, *, observed_at=1.0):
    fixture = json.loads((Path(__file__).parent / "fixtures/native_history_tail.json").read_text(encoding="utf-8"))
    evidence = copy.deepcopy(fixture["bases"]["dev-0"]["evidence"])
    users = [message for message in snapshot["messages"] if message["role"] == "user"]
    assert users
    user_id = users[-1]["message_id"]
    evidence.update(
        observed_at=observed_at, document_url=snapshot["url"],
        owner={key: snapshot.get(key) for key in
               ("page_id", "page_task_id", "page_team", "page_role")},
        last_group_key=user_id, last_user_message_ids=[user_id],
    )
    return evidence
