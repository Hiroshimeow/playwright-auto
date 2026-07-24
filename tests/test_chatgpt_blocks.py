import asyncio
from dataclasses import dataclass

from playwright_auto.chatgpt import (
    ChatGPTState,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
)
from playwright_auto.chatgpt_blocks import (
    CaptureSnapshotBlock,
    ClearComposerBlock,
    NewChatBlock,
    PrepareTaskBlock,
    SaveRecentResponsesBlock,
    SendPromptBlock,
    SetComposerBlock,
    SetRoleBlock,
    StopResponseBlock,
    WaitResponseBlock,
    WaitStateBlock,
)
from playwright_auto.workflow import Workflow


@dataclass
class FakeSnapshot:
    state: ChatGPTState

    def to_dict(self):
        return {"state": self.state.value}


class FakeChatGPTPage:
    def __init__(self):
        self.calls = []
        self.current = FakeSnapshot(ChatGPTState.NEW_CHAT)
        self.binding = PageBinding("page-1", "DEV")
        self.receipt = SendReceipt(
            prompt="hello",
            prompt_sha256="digest",
            binding=self.binding,
            baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
            attempts=1,
            accepted_via="stop_button",
            session_id_before=None,
        )

    async def new_chat(
        self,
        discard_draft=False,
        discard_attachments=False,
        stop_first=False,
    ):
        self.calls.append(
            ("new_chat", discard_draft, discard_attachments, stop_first)
        )
        return "dom"

    async def prepare_task(self, task_id, force_new_chat=False):
        self.calls.append(("prepare_task", task_id, force_new_chat))
        return {
            "task_id": task_id,
            "previous_task_id": None,
            "reused": False,
            "new_chat_method": "dom",
        }

    async def set_role(self, role, allow_rebind=False, force_new_page_id=False):
        self.calls.append(("set_role", role, allow_rebind, force_new_page_id))
        self.binding = PageBinding("page-1", role)
        self.receipt = SendReceipt(
            prompt="hello",
            prompt_sha256="digest",
            binding=self.binding,
            baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
            attempts=1,
            accepted_via="stop_button",
            session_id_before=None,
        )
        return {"page_role": role, "page_id": "page-1"}

    async def set_text(self, text, overwrite=False, expected_existing=None):
        self.calls.append(("set_text", text, overwrite, expected_existing))
        self.current = FakeSnapshot(ChatGPTState.DRAFT)

    async def clear(self, force=False, expected_text=None):
        self.calls.append(("clear", force, expected_text))
        self.current = FakeSnapshot(ChatGPTState.NEW_CHAT)

    async def send(
        self,
        text,
        wait_for_stop=True,
        max_attempts=2,
        recovery_reload=True,
        expected_attachment_count=0,
        expected_attachment_names=None,
    ):
        self.calls.append(
            (
                "send",
                text,
                wait_for_stop,
                max_attempts,
                recovery_reload,
                expected_attachment_count,
            )
        )
        self.current = FakeSnapshot(ChatGPTState.RESPONDING)
        return self.receipt

    async def wait_for_response(self, receipt, **options):
        self.calls.append(("wait_for_response", receipt, options))
        return MessageSnapshot("assistant", "a3", "t3", "response", ())

    async def stop(self):
        self.calls.append(("stop",))
        return "dom"

    async def wait_for_state(self, state, stable_ms=0):
        self.calls.append(("wait_for_state", state, stable_ms))
        self.current = FakeSnapshot(state)
        return self.current

    async def snapshot(self):
        self.calls.append(("snapshot",))
        return self.current

    async def recent_responses(self, count, by_turn=True):
        self.calls.append(("recent_responses", count, by_turn))
        return (
            MessageSnapshot("assistant", "a1", "t1", "first", ()),
            MessageSnapshot("assistant", "a2", "t2", "second", ()),
        )[-count:]


def test_chatgpt_blocks_form_a_complete_workflow():
    client = FakeChatGPTPage()
    workflow = Workflow(
        "chat",
        [
            SetRoleBlock("DEV"),
            PrepareTaskBlock(lambda ctx: ctx.require("task_id")),
            NewChatBlock(),
            SetComposerBlock(lambda ctx: ctx.require("prompt")),
            CaptureSnapshotBlock("draft"),
            ClearComposerBlock(),
            SendPromptBlock("hello", wait_for_stop=True),
            WaitStateBlock(ChatGPTState.RESPONDING),
            StopResponseBlock(),
            SaveRecentResponsesBlock(1),
        ],
    )

    result = asyncio.run(
        workflow.run(client, {"prompt": "from variable", "task_id": "TASK-1"})
    )

    assert result.context.variables["draft"].state is ChatGPTState.DRAFT
    assert [message.message_id for message in result.context.variables["responses"]] == ["a2"]
    assert client.calls == [
        ("set_role", "DEV", False, False),
        ("prepare_task", "TASK-1", False),
        ("new_chat", False, False, False),
        ("set_text", "from variable", False, None),
        ("snapshot",),
        ("clear", False, None),
        ("send", "hello", True, 2, True, 0),
        ("wait_for_state", ChatGPTState.RESPONDING, 0),
        ("stop",),
        ("recent_responses", 1, True),
    ]


def test_wait_response_block_uses_send_receipt_provenance():
    client = FakeChatGPTPage()
    workflow = Workflow(
        "provenance",
        [
            SetRoleBlock("DEV"),
            SendPromptBlock("hello"),
            WaitResponseBlock(stable_ms=0),
        ],
    )

    result = asyncio.run(workflow.run(client))

    assert result.context.variables["response"].message_id == "a3"
    assert client.calls[-1][0:2] == ("wait_for_response", client.receipt)
    assert client.calls[-1][2]["stable_ms"] == 0
    assert client.calls[-1][2]["skeptical_after_reload"] is True
