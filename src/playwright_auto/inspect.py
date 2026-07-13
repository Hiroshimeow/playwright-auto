import json

def locator_candidates(element:dict)->list[str]:
    result=[]
    if element.get("role") and element.get("name"): result.append(f'get_by_role({element["role"]!r}, name={element["name"]!r})'.replace("'","\""))
    if element.get("label"): result.append(f'get_by_label({element["label"]!r})'.replace("'","\""))
    if element.get("testid"): result.append(f'get_by_test_id({element["testid"]!r})'.replace("'","\""))
    if element.get("text"): result.append(f'get_by_text({element["text"]!r})'.replace("'","\""))
    return result

async def inspect_page(page)->list[dict]:
    return await page.locator("button,a,input,textarea,select,[role]").evaluate_all("""els => els.filter(e => e.offsetParent !== null).map(e => ({tag:e.tagName.toLowerCase(),role:e.getAttribute('role')||'',name:e.getAttribute('aria-label')||e.innerText?.trim().slice(0,120)||'',label:e.getAttribute('aria-label')||'',testid:e.getAttribute('data-testid')||'',text:e.innerText?.trim().slice(0,120)||''}))""")
