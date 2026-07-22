"""Persistent Playwright CDP automation."""

from .chatgpt import (
    AuthenticationRequiredError,
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    IncompleteResponseTimeoutError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    RateLimitBlockedError,
    SendReceipt,
    StableMalformedResponseError,
    TaskBindingError,
    configure_random_delay,
    confirm_delete_chat,
    delete_current_chat,
    open_delete_chat_dialog,
    random_delay,
)
from .durable import (
    DurableRecoveryState,
    DurableRequestBusyError,
    DurableRequestError,
    DurableRequestRecord,
    RequestLedger,
    RequestStatus,
)
from .durable_blocks import DurableSendBlock
from .upload import FileIdentity, UploadReceipt
from .roles import RoleSlot, expand_role_team
from .team import TeamRoundSpec, TeamTranscript
from .team_blocks import (
    DurableTeamRoleExecutor,
    TeamCheckpointMismatchError,
    TeamConversationBlock,
)
from .workflow import Workflow, WorkflowBlock, WorkflowContext, WorkflowRun
from .workspace import ChatGPTWorkspace, RoleBinding

__all__ = [
    "AuthenticationRequiredError",
    "ChatGPTPage",
    "ChatGPTSnapshot",
    "ChatGPTState",
    "ChatGPTWorkspace",
    "IncompleteResponseTimeoutError",
    "DurableRecoveryState",
    "DurableRequestBusyError",
    "DurableRequestError",
    "DurableRequestRecord",
    "DurableSendBlock",
    "FileIdentity",
    "MessageBaseline",
    "MessageSnapshot",
    "PageBinding",
    "RateLimitBlockedError",
    "RequestLedger",
    "RequestStatus",
    "RoleBinding",
    "RoleSlot",
    "SendReceipt",
    "StableMalformedResponseError",
    "TeamCheckpointMismatchError",
    "TeamConversationBlock",
    "TeamRoundSpec",
    "TeamTranscript",
    "TaskBindingError",
    "DurableTeamRoleExecutor",
    "UploadReceipt",
    "Workflow",
    "WorkflowBlock",
    "WorkflowContext",
    "WorkflowRun",
    "configure_random_delay",
    "confirm_delete_chat",
    "delete_current_chat",
    "open_delete_chat_dialog",
    "random_delay",
    "expand_role_team",
]
