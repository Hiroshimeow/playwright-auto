import asyncio

import pytest

from playwright_auto import chatgpt
from playwright_auto import chatgpt_composer


class FakePage:
    def __init__(self, results):
        self.results = list(results)

    async def evaluate(self, script, text):
        assert script is chatgpt_composer.COMPOSER_REPLACE_TEXT_JS
        return self.results.pop(0)


def test_composer_module_owns_atomic_replace_and_facade_error_type():
    page = FakePage([
        {
            "status": "ambiguous",
            "text": "",
            "owner_source": "owned",
            "candidate_count": 2,
        }
    ])

    with pytest.raises(chatgpt.ComposerConflictError, match="multiple eligible"):
        asyncio.run(
            chatgpt_composer.replace_owned_composer_text(
                page, "prompt", timeout_ms=100
            )
        )


def test_chatgpt_facade_uses_composer_owner_primitives():
    assert chatgpt._COMPOSER_RESOLVER_JS is chatgpt_composer.COMPOSER_RESOLVER_JS
    assert chatgpt._COMPOSER_REPLACE_TEXT_JS is chatgpt_composer.COMPOSER_REPLACE_TEXT_JS
    assert chatgpt._replace_owned_composer_text is chatgpt_composer.replace_owned_composer_text
