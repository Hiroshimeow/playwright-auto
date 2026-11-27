from __future__ import annotations

import copy

from test_cdpa_restart_writer_release import enqueue_restart, successors
from test_stopped_writer_worker_maintenance import configured, evidence, owned, enqueue, execute


def test_verified_maintenance_releases_pending_restart_once_through_existing_stop_owner(configured):
    c = configured
    before_ledger = c.ledger.read_bytes()
    before_hops = copy.deepcopy(c.store.load(c.path)["hops"])
    before_attempts = {
        hop["send_attempt_id"]: c.owner.db.get_command(hop["send_attempt_id"])
        for hop in before_hops
    }
    restart = enqueue_restart(c.worker, c.store.load(c.path), key="restart-waiting-for-maintenance")
    waiting = execute(c)
    assert waiting["status"] == "running", waiting
    assert not successors(c.store, c.state)
    assert c.owner.get_binding("binding-old")["binding_status"] == "active"
    enqueue(c, "maintenance-release-for-restart")
    maintenance = execute(c)
    assert maintenance["status"] == "applied", maintenance
    completed = c.owner.db.get_command(restart["command_id"])
    assert completed["status"] == "applied", completed
    children = successors(c.store, c.state)
    assert len(children) == 1
    assert completed["result"]["task_id"] == children[0]["task_id"]
    parent = c.store.load(c.path)
    assert parent["status"] == "STOPPED"
    assert parent["cleanup"]["phase"] == "writer_released"
    assert parent["hops"] == before_hops and c.ledger.read_bytes() == before_ledger
    assert all(c.owner.db.get_command(key) == value for key, value in before_attempts.items())
    assert c.owner.get_binding("binding-old")["binding_status"] == "sealed"
    assert not c.target.closed and not c.other.closed
    # The admitted Restart's audit replay cannot create a second replacement.
    same = enqueue_restart(c.worker, c.state, key=restart["command_id"],
                           version=restart["expected_task_version"])
    assert same["status"] == "applied"
    assert len(successors(c.store, c.state)) == 1
