"""特殊等待队列的持久化和确认边界。"""

from __future__ import annotations

import importlib

import pytest

pending = importlib.import_module("stock_robot_plugin_test.pending_queue")


def test_group_shares_three_latest_targets_and_reports_eviction(tmp_path):
    queue = pending.PendingQueue(tmp_path / "waiting.sqlite3")
    for symbol in ("600519", "000001", "00700.HK"):
        queue.enqueue("qq:group:1", "stock", symbol)
    replaced = queue.enqueue("qq:group:1", "stock", "09988.HK")
    assert replaced == {"kind": "stock", "symbol": "600519"}
    assert [x["symbol"] for x in queue.get("qq:group:1")["targets"]] == [
        "000001", "00700.HK", "09988.HK",
    ]
    assert queue.get("qq:group:2") is None


def test_duplicate_hk_spelling_refreshes_without_using_extra_slot(tmp_path):
    queue = pending.PendingQueue(tmp_path / "waiting.sqlite3")
    queue.enqueue("group", "stock", "hk00700")
    queue.enqueue("group", "stock", "600519")
    queue.enqueue("group", "stock", "00700")
    assert queue.get("group")["targets"] == [
        {"kind": "stock", "symbol": "600519"},
        {"kind": "stock", "symbol": "00700.HK"},
    ]


def test_confirmation_requires_sent_question_and_correct_revision(tmp_path):
    queue = pending.PendingQueue(tmp_path / "waiting.sqlite3")
    queue.enqueue("group", "stock", "00700")
    record = queue.get("group")
    assert queue.confirm("group", record["id"], now=10) is None
    assert queue.asked("group", record["id"], "需要分析腾讯吗？", now=10)
    assert queue.confirm("other", record["id"], now=11) is None
    assert queue.confirm("group", "stale-id", now=11) is None
    claimed = queue.confirm("group", record["id"], now=11)
    assert claimed["targets"] == [{"kind": "stock", "symbol": "00700.HK"}]
    assert queue.confirm("group", record["id"], now=12) is None


def test_reload_preserves_question_deadline(tmp_path):
    path = tmp_path / "waiting.sqlite3"
    queue = pending.PendingQueue(path)
    queue.enqueue("group", "stock", "00700")
    record = queue.get("group")
    queue.asked("group", record["id"], "现在继续吗？", now=100)
    reloaded = pending.PendingQueue(path)
    assert reloaded.get("group")["deadline"] == 1900
    assert reloaded.expire(now=1899) == []
    expired = reloaded.expire(now=1900)
    assert len(expired) == 1
    assert reloaded.get("group") is None
    assert reloaded.confirm("group", record["id"], now=1900) is None
    assert reloaded.expire(now=1901) == []


def test_new_analysis_clears_whole_waiting_batch(tmp_path):
    queue = pending.PendingQueue(tmp_path / "waiting.sqlite3")
    for symbol in ("00700", "600519", "000001"):
        queue.enqueue("group", "stock", symbol)
    previous = queue.get("group")
    assert queue.discard("group")
    assert queue.get("group") is None
    assert not queue.asked("group", previous["id"], "旧询问", now=1)


def test_two_store_instances_cannot_claim_same_request(tmp_path):
    path = tmp_path / "waiting.sqlite3"
    first, second = pending.PendingQueue(path), pending.PendingQueue(path)
    first.enqueue("group", "stock", "00700")
    record = first.get("group")
    first.asked("group", record["id"], "继续吗？", now=1)
    assert second.confirm("group", record["id"], now=2)
    assert first.confirm("group", record["id"], now=2) is None


def test_partial_confirmation_rejects_targets_outside_queue(tmp_path):
    queue = pending.PendingQueue(tmp_path / "waiting.sqlite3")
    queue.enqueue("group", "stock", "00700")
    queue.enqueue("group", "stock", "600519")
    record = queue.get("group")
    queue.asked("group", record["id"], "继续吗？", now=1)
    with pytest.raises(ValueError, match="队列"):
        queue.confirm("group", record["id"], symbols=["000001"], now=2)
    claimed = queue.confirm("group", record["id"], symbols=["00700.HK"], now=2)
    assert claimed["targets"] == [{"kind": "stock", "symbol": "00700.HK"}]


def test_confirm_at_deadline_never_executes(tmp_path):
    queue = pending.PendingQueue(tmp_path / "waiting.sqlite3")
    queue.enqueue("group", "stock", "00700")
    record = queue.get("group")
    queue.asked("group", record["id"], "继续吗？", now=10)
    assert queue.confirm("group", record["id"], now=1810) is None
    assert len(queue.expire(now=1810)) == 1


def test_gold_queue_deduplicates_and_survives_reload_with_old_targets(tmp_path):
    path = tmp_path / "waiting.sqlite3"
    original = pending.PendingQueue(path)
    original.enqueue("group", "stock", "00700")
    original.enqueue("group", "index", "000300")
    original.enqueue("group", "gold", "gold")
    assert original.enqueue("group", "gold", " GOLD ") is None
    restored = pending.PendingQueue(path)
    record = restored.get("group")
    assert record["targets"] == [
        {"kind": "stock", "symbol": "00700.HK"},
        {"kind": "index", "symbol": "000300"},
        {"kind": "gold", "symbol": "GOLD"},
    ]
    assert restored.asked("group", record["id"], "继续分析吗？", now=10)
    accepted = restored.confirm("group", record["id"], symbols=["GOLD"], now=11)
    assert accepted["targets"] == [{"kind": "gold", "symbol": "GOLD"}]
    assert pending.PendingQueue(path).accepted()[0]["targets"] == accepted["targets"]


@pytest.mark.parametrize("symbol", ["", "600519", "XAU", "GOLD2"])
def test_gold_queue_rejects_other_symbols(tmp_path, symbol):
    queue = pending.PendingQueue(tmp_path / "waiting.sqlite3")
    with pytest.raises(ValueError):
        queue.enqueue("group", "gold", symbol)
    assert queue.get("group") is None
