from datetime import UTC, datetime, timedelta

from brewchat.logs.session_log import SessionLog

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def test_first_message_sets_started_at_once(tmp_path):
    log = SessionLog(tmp_path / "logs" / "sessions.sqlite")
    assert log.get("s1") is None
    log.record_message("s1", now=T0)
    log.record_message("s1", now=T0 + timedelta(minutes=5))
    row = log.get("s1")
    assert row == {"session_id": "s1", "started_at": T0.isoformat(), "last_list_built_at": None, "lists_built": 0}


def test_each_build_updates_last_built_and_count(tmp_path):
    log = SessionLog(tmp_path / "sessions.sqlite")
    log.record_message("s1", now=T0)
    log.record_list_built("s1", now=T0 + timedelta(minutes=3))
    row = log.get("s1")
    assert row["lists_built"] == 1
    assert row["last_list_built_at"] == (T0 + timedelta(minutes=3)).isoformat()
    log.record_list_built("s1", now=T0 + timedelta(minutes=7))
    row = log.get("s1")
    assert row["lists_built"] == 2
    assert row["last_list_built_at"] == (T0 + timedelta(minutes=7)).isoformat()
    assert row["started_at"] == T0.isoformat()


def test_sessions_are_independent_and_persist(tmp_path):
    path = tmp_path / "sessions.sqlite"
    log = SessionLog(path)
    log.record_message("a", now=T0)
    log.record_message("b", now=T0 + timedelta(seconds=1))
    log.record_list_built("a", now=T0 + timedelta(minutes=1))
    reopened = SessionLog(path)
    assert reopened.get("a")["lists_built"] == 1
    assert reopened.get("b")["lists_built"] == 0


def test_build_without_prior_message_creates_row(tmp_path):
    log = SessionLog(tmp_path / "sessions.sqlite")
    log.record_list_built("x", now=T0)
    assert log.get("x") == {"session_id": "x", "started_at": T0.isoformat(),
                            "last_list_built_at": T0.isoformat(), "lists_built": 1}
