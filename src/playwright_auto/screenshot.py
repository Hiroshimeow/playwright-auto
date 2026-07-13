from pathlib import Path

def screenshot_options(full_page:bool,selector:str|None)->dict:
    if selector is None: return {"full_page":full_page}
    return {"selector":selector,"full_page":full_page}

async def capture(page,path:Path,full_page:bool=False,selector:str|None=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    if selector: await page.locator(selector).screenshot(path=str(path))
    else: await page.screenshot(path=str(path),full_page=full_page)
    return path
