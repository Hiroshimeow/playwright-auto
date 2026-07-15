"""Persistent Playwright CDP automation."""

from .chatgpt import (
    AuthenticationRequiredError,
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    RateLimitBlockedError,
    SendReceipt,
    TaskBindingError,
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
    "expand_role_team",
]
