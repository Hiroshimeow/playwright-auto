"""CDP detach must not trap cancellation while a maintenance lock is held."""
import asyncio
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_worker import CDPAWorker


@pytest.mark.parametrize("phase", ["browser", "page"])
def test_stopped_writer_target_cancellation_does_not_wait_forever_for_detach(phase):
    async def run():
        entered_send=asyncio.Event()
        entered_detach=asyncio.Event()
        never=asyncio.Event()
        info={"targetId":"target-1","type":"page","url":"https://chatgpt.com/c/saved-chat"}

        class Session:
            def __init__(self,kind):
                self.kind=kind

            async def send(self,method,params=None):
                assert method=="Target.getTargetInfo"
                if self.kind==phase:
                    entered_send.set()
                    await never.wait()
                return {"targetInfo":dict(info)}

            async def detach(self):
                if self.kind==phase:
                    entered_detach.set()
                    await never.wait()

        class Page:
            url=info["url"]
            def is_closed(self):
                return False

        page=Page()
        class Context:
            pages=[page]
            browser=None
            async def new_browser_cdp_session(self):
                return Session("browser")
            async def new_cdp_session(self,actual):
                assert actual is page
                return Session("page")

        context=Context()
        context.browser=context
        worker=object.__new__(CDPAWorker)
        task=asyncio.create_task(worker._stopped_writer_target(
            context,{"target_id":"target-1"},{"canonical_conversation_id":"saved-chat"},
        ))
        completed=False
        try:
            await asyncio.wait_for(entered_send.wait(),timeout=0.2)
            task.cancel()
            await asyncio.wait_for(entered_detach.wait(),timeout=0.2)
            done,_=await asyncio.wait({task},timeout=0.75)
            completed=task in done
            if completed:
                assert task.cancelled(),"the original cancellation must propagate"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        assert completed,"cancellation remained trapped in unbounded CDP detach"

    asyncio.run(run())
