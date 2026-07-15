"""Single import surface for user-authored workflow files."""

from .chatgpt import (
    AuthenticationRequiredError,
    ChatGPTPage,
    ChatGPTState,
    RateLimitBlockedError,
    TaskBindingError,
)
from .chatgpt_blocks import (
    AssertBlock,
    CaptureSnapshotBlock,
    ClearComposerBlock,
    NewChatBlock,
    PrepareTaskBlock,
    RecoverPageBlock,
    RefreshBlock,
    ResolveChoicePromptBlock,
    SaveRecentResponsesBlock,
    SendPromptBlock,
    SetComposerBlock,
    SetRoleBlock,
    StopResponseBlock,
    WaitCleanReadyBlock,
    WaitResponseBlock,
    WaitStateBlock,
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
from .loop import LoopOptions, WorkflowLoop
from .roles import RoleSlot, expand_role_team
from .team import TeamRoundSpec, TeamTranscript, resolve_role_selectors
from .team_blocks import (
    DurableTeamRoleExecutor,
    TeamCheckpointMismatchError,
    TeamConversationBlock,
    TeamRoundError,
)
from .upload import (
    FileIdentity,
    UploadError,
    UploadReadinessError,
    UploadReceipt,
    UploadTransportError,
)
from .upload_blocks import UploadFilesBlock, WaitUploadReadyBlock
from .workspace import ChatGPTWorkspace, RouteValidationError, WorkspaceBindingError, parse_route_map
from .workspace_blocks import (
    DispatchRouteBlock,
    ParseRouteBlock,
    RoleDispatchError,
    RoleSequenceBlock,
    WaitRouteResponsesBlock,
)
from .workflow import (
    ActionBlock,
    DelayBlock,
    RepeatBlock,
    RetryBlock,
    SequenceBlock,
    SetVariableBlock,
    TimeoutBlock,
    TryBlock,
    WaitUntilBlock,
    WhenBlock,
    Workflow,
    WorkflowContext,
)


def variable(name: str):
    return lambda context: context.require(name)


def previous_result(block_id: str):
    return lambda context: context.result(block_id)


__all__ = [
    "ActionBlock",
    "AuthenticationRequiredError",
    "AssertBlock",
    "CaptureSnapshotBlock",
    "ChatGPTPage",
    "ChatGPTState",
    "ChatGPTWorkspace",
    "ClearComposerBlock",
    "DelayBlock",
    "DurableRecoveryState",
    "DurableRequestBusyError",
    "DurableRequestError",
    "DurableRequestRecord",
    "DurableSendBlock",
    "DispatchRouteBlock",
    "FileIdentity",
    "LoopOptions",
    "NewChatBlock",
    "ParseRouteBlock",
    "PrepareTaskBlock",
    "RateLimitBlockedError",
    "RecoverPageBlock",
    "RefreshBlock",
    "ResolveChoicePromptBlock",
    "RepeatBlock",
    "RequestLedger",
    "RequestStatus",
    "RetryBlock",
    "RoleDispatchError",
    "RoleSequenceBlock",
    "RoleSlot",
    "RouteValidationError",
    "SaveRecentResponsesBlock",
    "SendPromptBlock",
    "SequenceBlock",
    "SetComposerBlock",
    "SetRoleBlock",
    "SetVariableBlock",
    "StopResponseBlock",
    "TeamCheckpointMismatchError",
    "TeamConversationBlock",
    "TeamRoundError",
    "TeamRoundSpec",
    "TeamTranscript",
    "TaskBindingError",
    "DurableTeamRoleExecutor",
    "WaitCleanReadyBlock",
    "WaitResponseBlock",
    "TimeoutBlock",
    "UploadError",
    "UploadFilesBlock",
    "UploadReadinessError",
    "UploadReceipt",
    "UploadTransportError",
    "TryBlock",
    "WaitRouteResponsesBlock",
    "WaitStateBlock",
    "WaitUploadReadyBlock",
    "WaitUntilBlock",
    "WhenBlock",
    "Workflow",
    "WorkflowContext",
    "WorkflowLoop",
    "WorkspaceBindingError",
    "expand_role_team",
    "parse_route_map",
    "previous_result",
    "resolve_role_selectors",
    "variable",
]
