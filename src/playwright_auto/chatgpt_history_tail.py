"""Read-only native transcript-tail evidence for exact stopped-writer proof."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

HISTORY_TAIL_JS = r"""
async () => {
  const root=(__ACTIVE_CHAT_ROOT__);
  const rendered=e=>{
    if(!e||!e.isConnected)return false;
    const r=e.getBoundingClientRect();
    if(!(r.width>0&&r.height>0))return false;
    for(let n=e;n;n=n.parentElement){
      const s=getComputedStyle(n);
      if(n.hasAttribute('inert')||n.getAttribute('aria-hidden')==='true'||
         s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||
         s.contentVisibility==='hidden'||Number(s.opacity)===0)return false;
    }
    return true;
  };
  const rect=e=>{
    if(!e)return null;
    const r=e.getBoundingClientRect();
    return {top:r.top,bottom:r.bottom,height:r.height,width:r.width};
  };
  const px=(e,k)=>{
    if(!e)return null;
    const value=parseFloat(getComputedStyle(e)[k]);
    return Number.isFinite(value)?value:null;
  };
  const hash=async text=>[...new Uint8Array(await crypto.subtle.digest(
    'SHA-256',new TextEncoder().encode(text)))].map(x=>x.toString(16).padStart(2,'0')).join('');
  const mains=[...document.querySelectorAll('main')].filter(rendered);
  const transcripts=[...root.querySelectorAll('[data-rail-chat-transcript]')].filter(rendered);
  const transcript=transcripts.length===1?transcripts[0]:null;
  const scrollers=[...root.querySelectorAll('.thread-scroll-container[role="region"]')]
    .filter(e=>rendered(e)&&transcript&&e.contains(transcript));
  const scroller=scrollers.length===1?scrollers[0]:null;
  const groups=transcript?[...transcript.querySelectorAll('[data-turn-key]')]
    .filter(e=>!e.parentElement.closest('[data-turn-key]')):[];
  const group=groups.at(-1)||null;
  const wrapper=group?.closest('[data-virtualized-turn]')||null;
  const list=wrapper?.parentElement||null;
  const canvas=list?.parentElement||null;
  const contents=canvas?.parentElement||null;
  const after=contents&&transcript&&contents.parentElement===transcript
    ? [...transcript.children].slice([...transcript.children].indexOf(contents)+1):[];
  const userUnits=group?[...group.querySelectorAll('[data-chatgpt-search-unit-key$=":user"]')]
    .filter(rendered):[];
  const userIds=[...new Set(userUnits.flatMap(e=>String(
    e.getAttribute('data-chatgpt-search-message-ids')||'').trim().split(/\s+/).filter(Boolean)))];
  const head=transcript?.firstElementChild||null;
  const headText=String(head?.textContent||'').replace(/\s+/g,' ').trim();
  const exactOlderLabel=headText==='Loading older messages…';
  const loaders=[];
  const loaderSelector=[
    '[data-loading-indicator]','[aria-busy="true"]','[data-loading]',
    '[data-is-loading]','[data-state="loading"]','[role="progressbar"]',
    '[data-testid*="loading"]','[data-testid*="skeleton"]',
    '.animate-spin','[class~="motion-safe:animate-spin"]'
  ].join(',');
  for(const marker of [...root.querySelectorAll(loaderSelector)].filter(rendered)){
    const insideHead=Boolean(head&&head!==contents&&head.contains(marker));
    const beforeCanvas=Boolean(head&&canvas&&
      (head.compareDocumentPosition(canvas)&Node.DOCUMENT_POSITION_FOLLOWING)&&
      head.getBoundingClientRect().bottom<=canvas.getBoundingClientRect().top+1);
    const exactHeadStructure=Boolean(
      head&&head.parentElement===transcript&&head===transcript.firstElementChild&&
      head.querySelectorAll('[data-loading-indicator]').length===1&&
      !head.querySelector('button,input,textarea,a,[contenteditable="true"]'));
    const older=insideHead&&beforeCanvas&&exactOlderLabel&&exactHeadStructure&&
      Boolean(marker.matches('[data-loading-indicator],[class~="motion-safe:animate-spin"]'));
    loaders.push({
      kind:older?'older_head':'unknown',inside_first_transcript_child:insideHead,
      dom_before_canvas:beforeCanvas,exact_older_label:exactOlderLabel,
      exact_head_structure:exactHeadStructure,inside_canvas:Boolean(canvas&&canvas.contains(marker)),
      head_label_sha256:await hash(headText),
      head_bottom:head?head.getBoundingClientRect().bottom:null,
      canvas_top:canvas?canvas.getBoundingClientRect().top:null,
    });
  }
  const metadata={};
  let storageReadable=true;
  try{
    for(const [field,key] of [
      ['page_id','playwright-auto:page-id'],['page_task_id','playwright-auto:task-id'],
      ['page_team','playwright-auto:team'],['page_role','playwright-auto:role']])
      metadata[field]=sessionStorage.getItem(key);
  }catch(_){storageReadable=false;}
  const busy=[root,scroller,transcript,canvas,list,wrapper].filter(Boolean)
    .some(e=>e.getAttribute('aria-busy')==='true');
  const cs=canvas?getComputedStyle(canvas):null,ls=list?getComputedStyle(list):null;
  return {
    schema:'chatgpt_virtual_tail_v1',observed_at:Date.now()/1000,
    document_ready_state:document.readyState,document_url:location.href,
    storage_readable:storageReadable,owner:metadata,
    counts:{live_main:mains.length,transcript:transcripts.length,
      scroller:scrollers.length,native_groups:groups.length,user_units:userUnits.length},
    reading:scroller?.getAttribute('data-thread-reading')||null,
    topology:{
      exact_active_main:mains.length===1&&mains[0]===root,
      transcript_inside_scroller:Boolean(scroller&&transcript&&scroller.contains(transcript)),
      canvas_inside_transcript:Boolean(canvas&&transcript&&transcript.contains(canvas)),
      contents_is_direct_transcript_child:Boolean(contents&&contents.parentElement===transcript&&getComputedStyle(contents).display==='contents'),
      canvas_only_mounted_list:Boolean(canvas&&list&&canvas.children.length===1&&canvas.firstElementChild===list),
      mounted_list_only_turn_wrappers:Boolean(list&&list.children.length>0&&[...list.children].every(e=>e.matches('[data-virtualized-turn]')&&e.querySelectorAll('[data-turn-key]').length===1)),
      tail_wrapper_is_last_list_child:Boolean(wrapper&&list&&list.lastElementChild===wrapper),
      tail_group_is_unique_in_wrapper:Boolean(wrapper&&group&&wrapper.querySelectorAll('[data-turn-key]').length===1&&wrapper.querySelector('[data-turn-key]')===group),
      trailing_spacer_zero:after.length===1&&after[0].getAttribute('aria-hidden')==='true'&&
        after[0].children.length===0&&!String(after[0].textContent||'').trim()&&
        after[0].getBoundingClientRect().height===0,
      canvas_known_style:Boolean(cs&&cs.position==='relative'&&cs.transform==='none'&&
        cs.paddingTop==='0px'&&cs.paddingBottom==='0px'&&canvas.style.height.endsWith('px')),
      list_known_style:Boolean(ls&&ls.display==='flex'&&ls.flexDirection==='column'&&
        ls.transform==='none'&&ls.paddingTop==='0px'&&ls.paddingBottom==='0px'&&ls.marginBottom==='0px'),
      last_group_rendered:rendered(group),last_user_rendered:userUnits.length===1&&rendered(userUnits[0]),
    },
    geometry:{canvas:rect(canvas),list:rect(list),wrapper:rect(wrapper),scroller:rect(scroller),
      mounted_margin_top:px(list,'marginTop')},
    last_group_key:group?.getAttribute('data-turn-key')||null,
    last_user_message_ids:userIds,loaders,busy_container:busy,
  };
}
"""


def validate_native_history_tail(
    evidence: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    latest_accepted_user_id: str,
) -> dict[str, Any]:
    """Recognize the observed virtualizer tail; require separate idle snapshot.

    This cannot establish remote task completion. The caller must bind both
    reads to the exact target/physical browser and validate the accepted ledger.
    No caller-provided history_tail_verified flag is accepted here.
    """
    failures: list[str] = []

    def mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    evidence, snapshot = mapping(evidence), mapping(snapshot)

    def require(value: bool, reason: str) -> None:
        if not value:
            failures.append(reason)

    def finite(value: Any) -> bool:
        return type(value) in (int, float) and math.isfinite(value)

    def close(a: Any, b: Any) -> bool:
        return finite(a) and finite(b) and abs(a - b) <= 1.0

    require(evidence.get("schema") == "chatgpt_virtual_tail_v1", "unknown_tail_schema")
    require(evidence.get("document_ready_state") == "complete" and
            evidence.get("storage_readable") is True, "document_not_ready")
    owner_keys = ("page_id", "page_task_id", "page_team", "page_role")
    owner = mapping(evidence.get("owner"))
    require(evidence.get("document_url") == snapshot.get("url") and
            all(owner.get(k) == snapshot.get(k) for k in owner_keys),
            "tail_snapshot_identity_changed")
    counts = mapping(evidence.get("counts"))
    require(all(type(counts.get(k)) is int and counts[k] == 1
                for k in ("live_main", "transcript", "scroller", "user_units")),
            "ambiguous_tail_owner")
    require(type(counts.get("native_groups")) is int and counts["native_groups"] > 0,
            "no_native_tail")
    require(evidence.get("reading") == "measured", "layout_not_measured")
    topology = mapping(evidence.get("topology"))
    fields = (
        "exact_active_main", "transcript_inside_scroller", "canvas_inside_transcript",
        "contents_is_direct_transcript_child", "canvas_only_mounted_list",
        "mounted_list_only_turn_wrappers", "tail_wrapper_is_last_list_child",
        "tail_group_is_unique_in_wrapper", "trailing_spacer_zero",
        "canvas_known_style", "list_known_style", "last_group_rendered", "last_user_rendered",
    )
    require(all(topology.get(k) is True for k in fields), "unknown_tail_topology")
    geometry = mapping(evidence.get("geometry"))
    canvas, mounted, wrapper, scroller = (
        mapping(geometry.get(k)) for k in ("canvas", "list", "wrapper", "scroller")
    )
    require(all(finite(node.get(k)) for node in (canvas, mounted, wrapper, scroller)
                for k in ("top", "bottom", "height", "width")),
            "invalid_tail_geometry")
    require(all(finite(node.get("height")) and node["height"] > 0 and
                finite(node.get("width")) and node["width"] > 0
                for node in (canvas, mounted, wrapper, scroller)),
            "tail_not_rendered")
    margin = geometry.get("mounted_margin_top")
    require(finite(margin) and margin >= 0, "unknown_virtualized_head_extent")
    require(close(wrapper.get("bottom"), mounted.get("bottom")) and
            close(mounted.get("bottom"), canvas.get("bottom")),
            "unmounted_or_trailing_tail")
    require(finite(margin) and finite(mounted.get("height")) and
            close(canvas.get("height"), margin + mounted["height"]),
            "virtualizer_extent_incomplete")
    require(finite(wrapper.get("bottom")) and finite(scroller.get("top")) and
            finite(scroller.get("bottom")) and
            scroller["top"] <= wrapper["bottom"] <= scroller["bottom"] + 1,
            "tail_outside_current_view")
    messages = snapshot.get("messages")
    known_messages = isinstance(messages, (list, tuple)) and all(isinstance(m, Mapping) for m in messages)
    require(known_messages, "native_messages_unknown")
    users = [m for m in messages if m.get("role") == "user"] if known_messages else []
    require(isinstance(latest_accepted_user_id, str) and bool(latest_accepted_user_id) and
            bool(users) and users[-1].get("message_id") == latest_accepted_user_id and
            evidence.get("last_user_message_ids") == [latest_accepted_user_id] and
            evidence.get("last_group_key") == latest_accepted_user_id,
            "latest_accepted_user_not_at_native_tail")
    loaders = evidence.get("loaders")
    require(isinstance(loaders, list), "loading_state_unknown")
    for loader in loaders if isinstance(loaders, list) else ():
        require(
            isinstance(loader, Mapping) and loader.get("kind") == "older_head" and
            loader.get("inside_first_transcript_child") is True and
            loader.get("dom_before_canvas") is True and
            loader.get("exact_older_label") is True and
            loader.get("exact_head_structure") is True and
            loader.get("inside_canvas") is False and
            loader.get("head_label_sha256") ==
              "913a378e70f6ee66434d8358526e077ad4e0d86e386f881f08eefa58f0257210" and
            finite(loader.get("head_bottom")) and finite(loader.get("canvas_top")) and
            loader["head_bottom"] <= loader["canvas_top"] + 1,
            "unclassified_or_tail_loading_marker",
        )
    require(evidence.get("busy_container") is False, "transcript_busy")
    require(snapshot.get("state") == "waiting_prompt" and
            snapshot.get("stop_visible") is False and
            "response_activity_turn_id" in snapshot and
            snapshot["response_activity_turn_id"] in (None, "") and
            snapshot.get("response_projection_active") is False,
            "response_not_positively_idle")
    # Other composer/challenge/permission/ledger gates remain in the Stop proof owner.
    result = {
        "verified": not failures,
        "reason_codes": sorted(set(failures)),
        "latest_accepted_user_id": latest_accepted_user_id,
        "evidence_sha256": hashlib.sha256(json.dumps(
            evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest() if not failures else None,
    }
    return result


async def collect_stopped_writer_observation(
    page: Any,
    *,
    endpoint_id: str,
    endpoint_cdp_url: str,
    physical_browser_id: str,
    target_id: str,
) -> dict[str, Any]:
    """Read only an already resolved and pinned Page.

    The worker owns endpoint and CDP target pinning before and after this call.
    This collector does not acquire a page, change metadata, navigate, scroll,
    click, close, or infer a complete tail from repeated snapshots. Raw snapshot
    text is transient input to the proof owner; persist only its safe digest.
    """
    import time

    from .chatgpt import inspect_chatgpt_page
    from .chatgpt_response_projection import ACTIVE_CHAT_ROOT_JS
    from .workspace import CLOUDFLARE_CHALLENGE_DOM, has_cloudflare_challenge_url

    identity = {
        "endpoint_id": endpoint_id, "endpoint_cdp_url": endpoint_cdp_url,
        "physical_browser_id": physical_browser_id, "target_id": target_id,
    }
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise ValueError("An exact pinned page context is required")
    before_url = str(page.url)
    challenge_before = await page.evaluate(CLOUDFLARE_CHALLENGE_DOM)
    if type(challenge_before) is not bool:
        raise ValueError("Provider challenge state is unknown")
    tail = await page.evaluate(HISTORY_TAIL_JS.replace(
        "__ACTIVE_CHAT_ROOT__", ACTIVE_CHAT_ROOT_JS,
    ))
    if not isinstance(tail, Mapping):
        raise ValueError("Native history-tail evidence is unavailable")
    snapshot = (await inspect_chatgpt_page(page)).to_dict()
    challenge_after = await page.evaluate(CLOUDFLARE_CHALLENGE_DOM)
    if type(challenge_after) is not bool:
        raise ValueError("Provider challenge state is unknown")
    return {
        **identity,
        "observed_at": time.time(),
        "document_ready_state": tail.get("document_ready_state"),
        "storage_readable": tail.get("storage_readable"),
        "provider_challenge": (
            challenge_before or challenge_after
            or has_cloudflare_challenge_url(before_url)
            or has_cloudflare_challenge_url(str(page.url))
            or has_cloudflare_challenge_url(str(snapshot.get("url") or ""))
        ),
        "snapshot": snapshot,
        "history_tail": dict(tail),
    }
