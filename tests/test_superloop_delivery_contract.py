from __future__ import annotations

import hashlib
import json
import time

import pytest

from orchestrator.superloop_receipts import SuperloopReceiptService
from orchestrator.superloop_runner import SuperloopRunner
from orchestrator.superloop_store import SuperloopStore
from orchestrator.superloop_taskboard import SuperloopTaskboardService
from orchestrator.superloop_validator import validate_loop


@pytest.fixture
def delivery_case(tmp_path):
    store = SuperloopStore(tmp_path / 'superloops')
    checks = [
        dict(id='adopt', kind='runtime_adoption', scope='instance-a/all-agents',
             scenario='Inspect every target Worker generation', expected='All use candidate',
             prerequisites=['approval:adoption']),
        dict(id='preview', kind='user_acceptance', scope='instance-a -> instance-b',
             scenario='Preview an existing Agent with a canonical identity file',
             expected='Preview succeeds and source remains unchanged', prerequisites=[]),
    ]
    for check in checks:
        check['subject_version'] = 'candidate-123'
    store.create_compiled_loop(
        loop_id='sl-delivery', loop_state={
            'status': 'running', 'continuous_supervision_required': True,
            'controller': {'agent': 'manager', 'instance': 'local'},
        }, taskboard=[dict(
            task_id='move', title='Move an Agent', owner_agent='manager', owner_instance='local',
            status='in_progress', depends_on=[], delivery_required=True,
            user_outcome='Existing Agents can safely move between the two instances',
            acceptance_checks=checks, acceptance_results={},
            runtime_adoption_verified=True, user_acceptance_verified=True,
            runtime_adoption_evidence_ref='anything.md', user_acceptance_evidence_ref='anything.md',
        )], issues=[], waits=[], operator_summary='Delivery remains open',
    )
    (store.loop_dir('sl-delivery') / 'anything.md').write_text('')
    return store, SuperloopTaskboardService(store)


def task_and_path(store):
    path = store.loop_dir('sl-delivery') / 'taskboard.json'
    return store.load_loop_json_list(path)[0], path


def save_task(store, task):
    store.save_loop_json_list(store.loop_dir('sl-delivery') / 'taskboard.json', [task])


def record_observation(store, task, check, *, result='passed', scope=None):
    root = store.loop_dir('sl-delivery')
    artifact = root / (check['id'] + '.log')
    artifact.write_text('Actual process or user operation observation: ' + result)
    observed_check = dict(check)
    if scope is not None:
        observed_check['scope'] = scope
    proof = dict(task_id=task['task_id'], check=observed_check, result=result,
                 observed_at='2026-09-08T07:00:00+00:00', observed='Observed operation result',
                 subject_version='candidate-123', observer='worker@remote',
                 artifacts=[dict(ref=artifact.name, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())])
    evidence = root / (check['id'] + '.json')
    evidence.write_text(json.dumps(proof))
    task['acceptance_results'][check['id']] = dict(
        evidence_ref=evidence.name, sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
        reviewed_by='manager@local',
    )
    save_task(store, task)


def test_status_transition_rejects_unaccepted_delivery(delivery_case):
    store, service = delivery_case
    with pytest.raises(ValueError, match='delivery'):
        service.update_task_status('sl-delivery', 'move', 'completed')
    assert task_and_path(store)[0]['status'] == 'in_progress'


def test_runner_cannot_close_bypassed_task_even_without_receipts(delivery_case):
    store, _ = delivery_case
    task, _ = task_and_path(store)
    task['status'] = 'completed'
    save_task(store, task)
    assert validate_loop(store, 'sl-delivery', closeout=True)['blocking']
    result = SuperloopRunner(store).next_action('sl-delivery')
    assert result['reason'] == 'closeout_blocked'
    assert store.load_loop_state('sl-delivery')['status'] != 'completed'


def test_partial_adoption_and_failed_preview_do_not_close_delivery(delivery_case):
    store, service = delivery_case
    task, _ = task_and_path(store)
    task['status'] = 'completed'
    record_observation(store, task, task['acceptance_checks'][0], scope='instance-a/one-agent')
    record_observation(store, task, task['acceptance_checks'][1], result='failed')
    gaps = SuperloopReceiptService(store, local_instance='local').delivery_gaps('sl-delivery')
    assert set(gaps) == {'move:adopt:scope_mismatch', 'move:preview:failed'}
    with pytest.raises(ValueError):
        service.update_task_status('sl-delivery', 'move', 'completed')


def test_adoption_wait_cannot_dispose_independent_preview(delivery_case):
    store, service = delivery_case
    (store.loop_dir('sl-delivery') / 'review.md').write_text('Reviewed missing evidence')
    wait = dict(kind='blocked', reason='Adoption needs permission', owner='manager',
                trigger='approval response', review_after=time.time() + 600,
                blocked_on=['approval:adoption'])
    task, _ = task_and_path(store)
    task['next_disposition'] = dict(check_dispositions=[dict(check_id=c['id'], **wait) for c in task['acceptance_checks']])
    save_task(store, task)
    row = dict(review_verified=True, review_evidence_ref='review.md',
               dispositions=[dict(task_id='move', **task['next_disposition'])],
               outcome_report=service.outcome_report('sl-delivery'))
    assert SuperloopReceiptService(store, local_instance='local').review_gaps('sl-delivery', row) == ['move:preview:next_step']


def test_reviewed_observations_survive_restart_and_detect_changed_evidence(delivery_case):
    store, service = delivery_case
    task, _ = task_and_path(store)
    for check in task['acceptance_checks']:
        record_observation(store, task, check)
    assert service.update_task_status('sl-delivery', 'move', 'completed')
    restarted = SuperloopTaskboardService(SuperloopStore(store.root_dir))
    assert restarted.outcome_report('sl-delivery')[0]['accepted'] is True
    assert validate_loop(store, 'sl-delivery', closeout=True)['blocking'] is False
    (store.loop_dir('sl-delivery') / 'preview.log').write_text('new failure')
    assert restarted.outcome_report('sl-delivery')[0]['accepted'] is False
    assert validate_loop(store, 'sl-delivery', closeout=True)['blocking'] is True


def test_complete_step_dispositions_and_report_still_need_terminal_proof(delivery_case):
    store, service = delivery_case
    task, _ = task_and_path(store)
    task['terminal_delivery_required'] = True
    task['acceptance_checks'].append(dict(id='notify', kind='terminal_delivery', scope='manager/user-channel',
        scenario='Inspect exact request final Connector receipt', expected='Final report sent', prerequisites=[],
        subject_version='candidate-123'))
    for check in task['acceptance_checks'][:2]:
        record_observation(store, task, check)
    (store.loop_dir('sl-delivery') / 'review.md').write_text('Exact request has not finished delivery')
    wait = dict(kind='deferred', reason='Await final Connector event after this run', owner='manager',
                trigger='existing receipt review', review_after=time.time() + 600)
    task['next_disposition'] = dict(check_dispositions=[dict(check_id='notify', **wait)])
    save_task(store, task)
    row = dict(review_verified=True, review_evidence_ref='review.md',
               dispositions=[dict(task_id='move', check_dispositions=[dict(check_id='notify', **wait)])],
               outcome_report=service.outcome_report('sl-delivery'))
    receipts = SuperloopReceiptService(store, local_instance='local')
    assert receipts.review_gaps('sl-delivery', row) == []
    assert row['outcome_report'][0]['accepted'] is False
    with pytest.raises(ValueError):
        service.update_task_status('sl-delivery', 'move', 'completed')
    # A new observed failure invalidates the saved report and needs a next action.
    record_observation(store, task, task['acceptance_checks'][1], result='failed')
    assert receipts.review_gaps('sl-delivery', row)


def test_report_and_valid_next_steps_cannot_disagree(delivery_case):
    store, service = delivery_case
    task, _ = task_and_path(store)
    for check in task['acceptance_checks']:
        record_observation(store, task, check)
    task['next_disposition'] = dict(kind='blocked', reason='obsolete full-task approval wait')
    save_task(store, task)
    (store.loop_dir('sl-delivery') / 'review.md').write_text('Both checks independently passed')
    row = dict(review_verified=True, review_evidence_ref='review.md',
        dispositions=[dict(task_id='move', check_dispositions=[])],
        outcome_report=service.outcome_report('sl-delivery'))
    assert 'move:next_step_mismatch' in SuperloopReceiptService(store, local_instance='local').review_gaps('sl-delivery', row)
    task['next_disposition'] = dict(check_dispositions=[])
    save_task(store, task)
    row['outcome_report'] = service.outcome_report('sl-delivery')
    assert 'move:closeout' in SuperloopReceiptService(store, local_instance='local').review_gaps('sl-delivery', row)
    service.update_task_status('sl-delivery', 'move', 'completed')
    row['outcome_report'] = service.outcome_report('sl-delivery')
    assert SuperloopReceiptService(store, local_instance='local').review_gaps('sl-delivery', row) == []
    # A new candidate cannot retain acceptance from the previous version.
    task['acceptance_checks'][0]['subject_version'] = 'candidate-456'
    save_task(store, task)
    assert service.outcome_report('sl-delivery')[0]['accepted'] is False
