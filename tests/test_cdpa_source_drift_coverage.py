"""Runtime self-repair must notice changes to the imported ChatGPT DOM and
RoleController owners; otherwise source drift stays falsely healthy."""
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_worker import CDPAWorker


def test_worker_source_identity_tracks_live_chatgpt_and_role_runtime_code():
    root=Path(__file__).resolve().parents[1]
    worker=SimpleNamespace(config=SimpleNamespace(repository_root=root))
    paths={str(path.resolve().relative_to(root.resolve())).replace("\\","/")
           for path in CDPAWorker._source_identity_paths(worker)
           if path.resolve().is_relative_to(root.resolve())}
    for name in (
        "src/playwright_auto/chatgpt.py",
        "src/playwright_auto/cdpa_conversations.py",
        "src/playwright_auto/cdpa_dependencies.py",
        "src/playwright_auto/dashboard.py",
        "src/playwright_auto/chatgpt_models.py",
        "src/playwright_auto/chatgpt_response_projection.py",
        "src/playwright_auto/role_runtime/controller.py",
        "src/playwright_auto/role_runtime/response.py",
        "src/playwright_auto/role_runtime/allow.py",
    ):
        assert name in paths, f"worker cannot detect source drift in {name}"
    assert len(paths)==len(set(paths)), "source identity should not duplicate paths"
