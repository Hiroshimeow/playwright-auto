from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto.cdpa_actions import AcquiredRole, CDPATabActions
from playwright_auto.cdpa_session_capabilities import SessionCapabilityObservation
from playwright_auto.chatgpt import ChatGPTPage
from test_cdpa_worker import send_snapshot


def requirement():
    return {
        'endpoint_id': 'endpoint-test', 'conversation_id': '/c/cdpa-send',
        'conversation_generation': 3, 'connector_id': 'md', 'target_alias': 'g6',
        'target_device_id': 'configured-device', 'principal_scope': 'configured-account',
        'project_binding': 'configured-project', 'required_operations': ['filesystem', 'report_write'],
        'policy_revision': 'policy-test',
    }


def client_for(live):
    client = object.__new__(ChatGPTPage)
    client.page = SimpleNamespace(evaluate=AsyncMock(return_value=live))
    client.timeout_ms = 1000
    client.assert_ownership = AsyncMock(return_value=send_snapshot())
    return client


def test_production_dom_observer_is_invoked_through_actions_without_invented_grants():
    client = client_for({'url': 'https://chatgpt.com/c/cdpa-send', 'time_origin': 12345,
                         'composer_present': True, 'controls': [{'label': 'md'}],
                         'gateway_online': True, 'target_device_id': 'configured-device'})
    actions = object.__new__(CDPATabActions)
    acquired = AcquiredRole(client, 'page-PLAN', 'https://chatgpt.com/c/cdpa-send', False, False)
    raw = asyncio.run(actions.observe_session_tool_capability(
        state={}, role='PLAN', acquired=acquired, requirement=requirement(), phase='pre_send'))
    assert client.page.evaluate.await_count == client.assert_ownership.await_count == 1
    assert raw['status'] == 'unknown'
    assert raw['target_device_id'] is raw['principal_scope'] is raw['project_binding'] is None
    assert raw['required_operations'] == []
    assert raw['evidence_ref'].startswith('live_page_dom:')
    assert 'no authenticated session-bound' in raw['reason_detail']
    assert SessionCapabilityObservation.from_mapping(raw).to_dict()['reason_detail'] == raw['reason_detail']


def test_production_dom_observer_does_not_relabel_another_conversation():
    client = client_for({'url': 'https://chatgpt.com/c/other', 'time_origin': 12345})
    raw = asyncio.run(client.observe_session_tool_capability(requirement=requirement(), phase='send_boundary'))
    assert raw['status'] == 'unknown'
    assert raw['conversation_id'] == '/c/other'
    assert 'navigated' in raw['reason_detail']


def test_production_dom_observer_probe_error_preserves_unknown_without_fake_session_revision():
    client = client_for(None)
    client.page.evaluate = AsyncMock(side_effect=TimeoutError('probe timeout'))
    raw = asyncio.run(client.observe_session_tool_capability(requirement=requirement(), phase='pre_send'))
    assert raw['status'] == 'unknown'
    assert raw['session_revision'] == ''
    assert raw['target_device_id'] is None


def test_production_dom_observer_proves_login_gate_but_not_target_authorization():
    from dataclasses import replace
    client = client_for({'url': 'https://chatgpt.com/c/cdpa-send', 'time_origin': 12345})
    client.assert_ownership = AsyncMock(return_value=replace(send_snapshot(), requires_login=True))
    raw = asyncio.run(client.observe_session_tool_capability(requirement=requirement(), phase='pre_send'))
    assert raw['status'] == 'unavailable'
    assert raw['reason_code'] == 'tool_authorization_failed'


@pytest.mark.parametrize(
    'insufficient_surface',
    [
        {'controls': [{'label': 'Allow md for this conversation'}]},
        {'project_binding': 'configured-project'},
        {'gateway_online': True, 'target_device_id': 'configured-device'},
        {
            'controls': [{'label': 'Allow md for this conversation'}],
            'project_binding': 'configured-project',
            'gateway_online': True,
            'target_device_id': 'configured-device',
        },
    ],
)
def test_production_observer_keeps_allow_project_and_gateway_surfaces_unknown(insufficient_surface):
    live = {
        'url': 'https://chatgpt.com/c/cdpa-send',
        'time_origin': 12345,
        'composer_present': True,
        **insufficient_surface,
    }
    raw = asyncio.run(
        client_for(live).observe_session_tool_capability(
            requirement=requirement(), phase='pre_send'
        )
    )

    assert raw['status'] == 'unknown'
    assert raw['reason_code'] == 'session_tool_visibility_unknown'
    assert raw['target_device_id'] is None
    assert raw['principal_scope'] is None
    assert raw['project_binding'] is None
    assert raw['required_operations'] == []
