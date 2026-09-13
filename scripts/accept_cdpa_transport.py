"""Opt-in live CDPA transport acceptance; creates/closes only probe-owned pages.

Run with the project virtualenv. Evidence contains only this probe's conversations,
never auth tokens or unrelated tab contents. Existing browser processes stay alive.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from playwright_auto.cdpa_actions import CDPATabActions
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.connection import connect


def targets(address):
    with urlopen(address + "/json/list", timeout=5) as response:
        return sorted(item["id"] for item in json.load(response) if item.get("id"))


def canonical(value):
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


async def probe(address, evidence_path):
    tag = "cdpa-transport-" + uuid4().hex[:12]
    evidence = {"endpoint": address, "tag": tag, "started_at": datetime.now(timezone.utc).isoformat(), "events": []}
    before = await asyncio.to_thread(targets, address)
    evidence["target_ids_before"] = before
    owned = []
    pw, browser = await connect(address)
    context = browser.contexts[0]

    def save():
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")

    async def fresh(name, temporary=False):
        manifest = {"task_id": tag, "team": tag, "roles": {"PLAN": {"physical_role": name}}}
        acquired = await actions.fresh_chat(
            manifest, "PLAN", url="https://chatgpt.com/?temporary-chat=true" if temporary else "https://chatgpt.com/",
        )
        owned.append(acquired.client.page)
        return acquired.client

    async def send_anchor(client, label):
        token = tag + "-" + label
        receipt = await client.send(
            f"This is a bounded transport check. Reply with exactly {token}. Do not use tools.",
            wait_for_stop=False, max_attempts=1, recovery_reload=False,
            expected_task_id=tag, expected_team=tag,
        )
        evidence["events"].append({"kind": label + "_accepted", "receipt": receipt.to_dict()})
        save()
        def validate_marker(candidate):
            if token not in candidate.text:
                raise ValueError("transport check marker is still incomplete")
        response = await client.wait_for_response(
            receipt, timeout_ms=180000, poll_ms=500, stable_ms=1000, minimum_samples=2,
            candidate_validator=validate_marker,
        )
        snapshot = await client.snapshot()
        result = {"conversation_id": canonical(receipt.conversation_id) or canonical(snapshot.session_id),
                  "assistant_message_id": canonical(response.message_id), "visible_url": snapshot.url,
                  "response": response.to_dict(), "marker_present": token in response.text}
        evidence[label] = result
        save()
        assert result["marker_present"], result
        return result

    class ObservedActions(CDPATabActions):
        @staticmethod
        def _watch_new_branch_response(page):
            future, listener = CDPATabActions._watch_new_branch_response(page)
            def captured(done):
                if not done.cancelled() and done.exception() is None:
                    response = done.result()
                    evidence["events"].append({"kind": "new_branch_response", "url": response.url, "status": response.status})
                    save()
            future.add_done_callback(captured)
            return future, listener

    actions = ObservedActions(context, SimpleNamespace(workspace_timeout_seconds=30))
    try:
        persistent = await fresh(tag + "-persistent")
        anchor = await send_anchor(persistent, "persistent")
        assert anchor["conversation_id"] and anchor["assistant_message_id"], anchor
        manifest = {"task_id": tag, "team": tag, "roles": {"PLAN": {"physical_role": tag + "-branch"}}}
        branch = await actions.branch_from_anchor(manifest, "PLAN", source_conversation_id=anchor["conversation_id"], assistant_message_id=anchor["assistant_message_id"])
        owned.append(branch.client.page)
        evidence["persistent_branch"] = {"direct_url": f"https://chatgpt.com/branch/{anchor['conversation_id']}/{anchor['assistant_message_id']}",
                                         "canonical_conversation_id": branch.conversation_id, "visible_url": branch.url,
                                         "distinct_from_donor": branch.conversation_id != anchor["conversation_id"]}
        assert canonical(branch.conversation_id) and branch.conversation_id != anchor["conversation_id"]
        save()
        temporary = await fresh(tag + "-temporary", temporary=True)
        temp_anchor = await send_anchor(temporary, "temporary")
        if temp_anchor["conversation_id"] and temp_anchor["assistant_message_id"]:
            manifest["roles"]["PLAN"]["physical_role"] = tag + "-temp-branch"
            try:
                candidate = await actions.branch_from_anchor(manifest, "PLAN", source_conversation_id=temp_anchor["conversation_id"], assistant_message_id=temp_anchor["assistant_message_id"])
                owned.append(candidate.client.page)
                evidence["temporary_branch"] = {"outcome": "branch_created", "visible_url": candidate.url,
                                                 "canonical_conversation_id": candidate.conversation_id,
                                                 "temporary_url_preserved": "temporary-chat=true" in candidate.url}
            except Exception as exc:
                evidence["temporary_branch"] = {"outcome": "not_usable", "error": f"{type(exc).__name__}: {exc}"}
        else:
            evidence["temporary_branch"] = {"outcome": "no_usable_durable_anchor"}
        old_token = await CDPAWorker._temporary_document_token(temporary, create=True)
        await temporary.page.reload(wait_until="domcontentloaded", timeout=30000)
        await temporary.page.locator('[contenteditable="true"][role="textbox"]').first.wait_for(state="visible", timeout=30000)
        new_token = await CDPAWorker._temporary_document_token(temporary)
        after = await temporary.snapshot()
        evidence["temporary_f5"] = {"document_changed": old_token != new_token, "visible_url": after.url,
                                    "prior_marker_present": any(tag + "-temporary" in item.text for item in after.messages),
                                    "message_count": len(after.messages)}
        assert old_token and old_token != new_token
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for page in owned:
            if not page.is_closed():
                await page.close()
        await pw.stop()
        after_ids = await asyncio.to_thread(targets, address)
        for _ in range(300):
            if before == after_ids:
                break
            await asyncio.sleep(0.2)
            after_ids = await asyncio.to_thread(targets, address)
        evidence["target_ids_after"] = after_ids
        evidence["original_targets_preserved"] = set(before).issubset(after_ids)
        evidence["target_sets_unchanged"] = before == after_ids
        if not evidence["target_sets_unchanged"]:
            evidence["status"] = "failed"
            evidence["error"] = evidence.get("error") or "acceptance cleanup did not preserve exact browser target sets"
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
        print(json.dumps({"endpoint": address, "status": evidence.get("status"), "evidence": str(evidence_path), "error": evidence.get("error")}), flush=True)
        assert evidence["target_sets_unchanged"], evidence["error"]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint", action="append", default=[])
    args = parser.parse_args()
    for address in args.endpoint or ["http://127.0.0.1:9222", "http://127.0.0.1:9202"]:
        await probe(address, args.output / (address.rsplit(":", 1)[-1] + "-transport.json"))


if __name__ == "__main__":
    asyncio.run(main())
