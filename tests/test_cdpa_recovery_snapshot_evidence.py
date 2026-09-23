from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_routes import expected_report_relative
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.role_runtime.response import observe
from test_cdpa_worker import _prepare_sent_waiting_task


def test_snapshot_uses_actual_report_and_checkpoint_before_model_claims(tmp_path):
    store, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="snapshot-artifacts")
    expected = expected_report_relative(repository_root=tmp_path, plans_root=store.config.plans_root,
        team=state['team'], physical_role=hop['physical_role'], turn=hop['turn'], task_id=state['task_id'])
    report = tmp_path / expected
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("Completed step one; step two remains unfinished.\n", encoding='utf-8', newline='')
    (tmp_path / 'checkpoint.md').write_text('step_one=verified\n', encoding='utf-8', newline='')
    hop['wait']['last_assistant_progress'] = {'text': 'Everything is finished', 'observed_at': '2026-09-12T00:00:00Z'}
    log = tmp_path / '.runtime/live-events.jsonl'
    log.parent.mkdir(exist_ok=True)
    log.write_text(json.dumps({'task_id':state['task_id'], 'kind':'validation', 'values':{'passed':1,'remaining':1}})+'\n'+
                   json.dumps({'task_id':'unrelated','kind':'foreign','values':{'message':'must not enter snapshot'}})+'\n', encoding='utf-8')
    snapshot = worker._build_execution_recovery_snapshot(state, hop, reason='endpoint lost')
    assert snapshot['current_report']['availability'] == 'verified'
    assert snapshot['current_report']['content'] == report.read_text(encoding='utf-8')
    assert snapshot['current_report']['sha256']
    assert snapshot['checkpoints'][0]['content'] == 'step_one=verified\n'
    assert snapshot['last_progress']['assistant']['text'] == 'Everything is finished'
    assert snapshot['last_progress']['advisory_only'] is True
    assert snapshot['source_of_truth'][0] == 'workspace_reports_checkpoints_git'
    assert snapshot['send_provenance']['boundary'] == 'accepted'
    assert snapshot['durable_paths']['ledger'] == hop['ledger_path']
    assert 'must not enter snapshot' not in json.dumps(snapshot)
    assert snapshot['recent_logs'][0]['kind'] == 'validation'


def test_snapshot_does_not_recursively_embed_full_prior_recovery_prompts(tmp_path):
    _, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id='bounded-recovery')
    hop['kind'] = 'execution_recovery'
    hop['handoff'] = 'previous recovery ' * 20000
    hop['receipt']['prompt'] = 'old snapshot ' * 20000
    snapshot = worker._build_execution_recovery_snapshot(state, hop, reason='lost again')
    assert len(snapshot['execution']['handoff']) <= 4000
    assert 'prompt' not in snapshot['send_provenance']['receipt']
    assert snapshot['task']['goal'] == state['task_text']
    assert len(json.dumps(snapshot)) < 30000


def test_snapshot_records_latest_effective_goal_and_validation_errors(tmp_path):
    _, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id='changed-goal-snapshot')
    hop['hop_id'] = 2
    state['goal_revisions'] = [{'applies_from_hop_id':2, 'goal':'Finish only the remaining verification'}]
    hop['validation_error'] = 'last report failed test evidence check'
    snapshot = worker._build_execution_recovery_snapshot(state, hop, reason='offline')
    assert snapshot['task']['goal'] == 'Finish only the remaining verification'
    assert 'original_task' not in snapshot['task']
    assert snapshot['task']['task_text_ref'].endswith('#task_text')
    assert snapshot['validation']['error'] == hop['validation_error']


def test_large_recovery_snapshot_has_hard_total_bound_and_references_task_text(tmp_path):
    _, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id='large-recovery-snapshot')
    state['task_text'] = 'ORIGINAL_TASK_DETAIL ' * 5000
    hop['response'] = 'ASSISTANT_PROGRESS ' * 5000
    hop['wait']['last_assistant_progress'] = {'text': 'PROGRESS_DETAIL ' * 5000}

    snapshot = worker._build_execution_recovery_snapshot(state, hop, reason='endpoint unavailable')
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    assert len(encoded) <= 12000
    assert encoded.count('ORIGINAL_TASK_DETAIL') < 60
    assert 'original_task' not in snapshot['task']
    assert snapshot['task']['task_text_ref'].endswith('#task_text')
    assert len(snapshot['task']['goal']) <= 1000
    assert snapshot['send_provenance']['classification'] == 'accepted_or_ambiguous'
    receipt = snapshot['send_provenance']['receipt']
    assert receipt is not None
    assert {'prompt_sha256', 'binding', 'user_message_id', 'user_turn_id', 'attempts'} <= set(receipt)


def test_last_useful_progress_is_bounded_and_survives_a_lost_document():
    wait = {}
    ctx = SimpleNamespace(snapshot=SimpleNamespace(messages=[SimpleNamespace(role='assistant',text='x'*12000,message_id='a1',turn_id='t1')]),
        wait=wait, now=datetime.now(timezone.utc), policy=SimpleNamespace(response_stable_seconds=1,minimum_samples=2),
        phase='wait', validation_error=None, candidate_stable=False)
    observe(ctx, None, None)
    assert len(wait['last_assistant_progress']['text']) == 8000
    assert wait['last_assistant_progress']['message_id'] == 'a1'
    ctx.snapshot = SimpleNamespace(messages=[])
    observe(ctx, None, None)
    assert wait['last_assistant_progress']['message_id'] == 'a1'
