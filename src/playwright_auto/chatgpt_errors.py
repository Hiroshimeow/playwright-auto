"""ChatGPT runtime error hierarchy."""

from __future__ import annotations


class ChatGPTAutomationError(RuntimeError):
    pass


class PageOwnershipError(ChatGPTAutomationError):
    pass


class AuthenticationRequiredError(ChatGPTAutomationError):
    pass


class ComposerConflictError(ChatGPTAutomationError):
    pass


class UnsafePageStateError(ChatGPTAutomationError):
    pass


class ConversationTranscriptNotReadyError(UnsafePageStateError):
    pass


class RateLimitBlockedError(UnsafePageStateError):
    pass


class SendRecoveryError(ChatGPTAutomationError):
    pass


class IncompleteResponseTimeoutError(TimeoutError, ChatGPTAutomationError):
    """Exact-provenance response exists but remained structurally incomplete."""


class StableMalformedResponseError(ChatGPTAutomationError):
    """A clean, inactive assistant candidate stayed malformed through its grace period."""

    def __init__(
        self,
        candidate: "MessageSnapshot",
        validation_error: BaseException,
    ) -> None:
        self.candidate = candidate
        self.validation_error = validation_error
        super().__init__(f"stable malformed response: {validation_error}")


class ManualInputPendingError(ChatGPTAutomationError):
    pass


class ChoicePromptBlockedError(ChatGPTAutomationError):
    pass


class TaskBindingError(ChatGPTAutomationError):
    pass
