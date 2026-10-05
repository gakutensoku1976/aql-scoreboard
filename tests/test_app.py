import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("INPUT_PATH", "secret-xyz")
    import app as app_module
    importlib.reload(app_module)
    return TestClient(app_module.app)


def post(c, **body):
    return c.post("/secret-xyz/api/action", json=body)


def test_viewer_and_input_pages(client):
    assert client.get("/").status_code == 200
    assert client.get("/secret-xyz").status_code == 200
    assert client.get("/input").status_code == 404
    assert client.post("/input/api/action", json={"type": "through"}).status_code == 404


def test_action_updates_state_and_undo(client):
    v0 = client.get("/api/state").json()["version"]
    r = post(client, type="correct", team=0, seat=2)
    assert r.status_code == 200
    s = client.get("/api/state").json()
    assert s["teams"][0]["score"] == 2 and s["version"] != v0
    post(client, type="undo")
    assert client.get("/api/state").json()["teams"][0]["score"] == 1
    assert post(client, type="undo").status_code == 409


def test_locked_seat_rejected(client):
    post(client, type="wrong", team=1, seat=0)
    post(client, type="wrong", team=1, seat=0)
    r = post(client, type="correct", team=1, seat=0)
    assert r.status_code == 409 and "封鎖" in r.json()["detail"]


def test_rename_and_reset_keeps_names(client):
    post(client, type="rename", names=["QUAPS", "  "])
    post(client, type="correct", team=0, seat=0)
    post(client, type="reset")
    s = client.get("/api/state").json()
    assert [t["name"] for t in s["teams"]] == ["QUAPS", "チームB"]
    assert s["question"] == 0 and s["log"] == []


def test_reset_keeps_roster(client):
    players = [["永田", ""], ["", ""], ["", ""], ["", ""], ["", "佐藤"]]
    assert post(client, type="roster", team=0, players=players).status_code == 200
    post(client, type="correct", team=0, seat=4)
    post(client, type="reset")
    s = client.get("/api/state").json()
    assert [seat["players"] for seat in s["teams"][0]["seats"]] == players
    assert s["teams"][0]["score"] == 1


def test_roster_too_long_rejected(client):
    r = post(client, type="roster", team=0, players=[["とても長すぎる名", ""]] + [["", ""]] * 4)
    assert r.status_code == 409


def test_display_off_removes_items_from_viewer_api(client):
    post(client, type="correct", team=0, seat=0)
    post(client, type="through")
    v = client.get("/api/state").json()
    assert v["question"] == 2 and v["log"][0]["q"] == 1

    r = post(client, type="display", show_question=False)
    assert r.status_code == 200 and r.json()["question"] == 2  # 入力画面には残る
    v = client.get("/api/state").json()
    assert "question" not in v and "max_questions" not in v
    assert all("q" not in e for e in v["log"])

    post(client, type="display", show_log=False)
    v = client.get("/api/state").json()
    assert "log" not in v and "question" not in v
    full = client.get("/secret-xyz/api/state").json()
    assert full["question"] == 2 and len(full["log"]) == 2
    assert full["display"] == {"show_question": False, "show_log": False}


def test_display_change_bumps_version_and_survives_undo_reset(client):
    v0 = client.get("/api/state").json()["version"]
    post(client, type="display", show_log=False)
    v1 = client.get("/api/state").json()["version"]
    assert v1 != v0  # 閲覧画面が再描画されるように版が変わる
    post(client, type="correct", team=1, seat=1)
    post(client, type="undo")
    post(client, type="reset")
    assert client.get("/secret-xyz/api/state").json()["display"]["show_log"] is False


def test_full_state_requires_input_path(client):
    assert client.get("/input/api/state").status_code == 404


def test_title_shown_to_viewer_and_kept_on_reset(client):
    post(client, type="rename", title="503会議室 第2試合", names=["", ""])
    post(client, type="correct", team=0, seat=0)
    post(client, type="reset")
    v = client.get("/api/state").json()
    assert v["title"] == "503会議室 第2試合"
    assert [t["name"] for t in v["teams"]] == ["チームA", "チームB"]


def test_responses_require_revalidation(client):
    for url in ("/", "/secret-xyz", "/static/board.js", "/api/state"):
        assert client.get(url).headers["cache-control"] == "no-cache"


def test_targets_action_undo_and_reset(client):
    r = post(client, type="targets", custom_target=True, targets=[300, 150])
    assert r.status_code == 200
    v = client.get("/api/state").json()
    assert v["custom_target"] is True and [t["target"] for t in v["teams"]] == [300, 150]
    post(client, type="correct", team=0, seat=0)
    post(client, type="reset")  # リセットしても勝利点の設定は残る
    v = client.get("/api/state").json()
    assert v["custom_target"] is True and [t["target"] for t in v["teams"]] == [300, 150]
    post(client, type="targets", custom_target=True, targets=[400, 150])
    post(client, type="undo")  # 取り消しで変更前に戻る
    assert [t["target"] for t in client.get("/api/state").json()["teams"]] == [300, 150]


def test_targets_rejected_with_message(client):
    for _ in range(3):
        post(client, type="correct", team=1, seat=0)  # B = 4
    r = post(client, type="targets", custom_target=True, targets=[200, 4])
    assert r.status_code == 409 and "現在の得点（4点）" in r.json()["detail"]
    assert client.get("/api/state").json()["custom_target"] is False


def _full_state(client):
    return client.get("/secret-xyz/api/state").json()


def test_judgements_get_time_and_scores_but_settings_do_not(client):
    post(client, type="rename", title="第1試合", names=["A", "B"])
    post(client, type="correct", team=0, seat=1)
    post(client, type="wrong", team=1, seat=0)
    post(client, type="through")
    post(client, type="targets", custom_target=True, targets=[300, 200])
    log = client.get("/secret-xyz/api/export").json()["log"]
    assert [e["kind"] for e in log] == ["correct", "wrong", "through"]
    assert all(e["at"].endswith("+09:00") for e in log)
    assert [e["scores"] for e in log] == [[2, 1], [2, 1], [2, 1]]


def test_export_contents(client):
    post(client, type="rename", title="503会議室 第2試合", names=["中日", "西武"])
    post(client, type="roster", team=0, players=[["彦野", "仁村"], ["立浪", ""], ["", ""], ["", ""], ["", ""]])
    post(client, type="targets", custom_target=True, targets=[300, 150])
    for _ in range(7):
        post(client, type="correct", team=0, seat=0)
    for _ in range(7):
        post(client, type="through")
    post(client, type="wrong", team=1, seat=2)
    post(client, type="wrong", team=1, seat=2)
    r = client.get("/secret-xyz/api/export")
    assert r.status_code == 200
    assert 'attachment; filename="aql_record_' in r.headers["content-disposition"]
    d = r.json()
    assert d["format"] == "aql-scoreboard-record" and d["format_version"] == 1
    assert d["exported_at"].endswith("+09:00")
    assert d["title"] == "503会議室 第2試合"
    assert d["rules"] == {"max_questions": 40, "no_question_limit": False, "custom_target": True, "targets": [300, 150]}
    assert d["question"] == 16 and d["result"] is None
    a, b = d["teams"]
    assert (a["name"], a["score"], a["product"], a["target"]) == ("中日", 8, 8, 300)
    assert a["seats"][0] == {"seat": 1, "players": ["彦野", "仁村"], "score": 8, "cross": 0, "locked": False, "reach": False}
    assert a["seats"][1]["players"] == ["立浪"] and a["seats"][2]["players"] == []
    assert b["seats"][2]["locked"] is True and b["seats"][2]["cross"] == 2
    assert len(d["log"]) == 16  # 画面の12件制限を受けず全件
    first = d["log"][0]
    assert (first["q"], first["kind"], first["team_name"], first["seat"], first["players"]) == (1, "correct", "中日", 1, ["彦野", "仁村"])
    assert "team" not in d["log"][7]  # スルーにはチーム・枠がない


def test_export_result_and_undo_reset(client):
    post(client, type="targets", custom_target=True, targets=[3, 200])
    post(client, type="correct", team=0, seat=0)
    post(client, type="correct", team=0, seat=0)  # 3点で勝利
    d = client.get("/secret-xyz/api/export").json()
    assert d["result"] == {"winner": 0, "winner_name": "チームA", "reason": "勝利点到達"}
    post(client, type="undo")
    assert len(client.get("/secret-xyz/api/export").json()["log"]) == 1  # 取り消した判定は残らない
    post(client, type="reset")
    d = client.get("/secret-xyz/api/export").json()
    assert d["log"] == [] and d["rules"]["targets"] == [3, 200]


def test_export_only_under_input_path_and_old_log_entries(client):
    assert client.get("/api/export").status_code == 404
    import app as app_module
    st = app_module.game.correct(app_module.game.new_state(), 0, 0)  # 時刻・得点のない古い形式の記録
    entry = app_module.export_record(st, app_module.now_jst())["log"][0]
    assert entry["at"] is None and entry["scores"] is None


def test_question_limit_option_action_undo_reset_export(client):
    r = post(client, type="question_limit", no_question_limit=True)
    assert r.status_code == 200 and r.json()["no_question_limit"] is True
    assert client.get("/api/state").json()["no_question_limit"] is True
    post(client, type="through")
    post(client, type="reset")  # リセットしても残る
    assert _full_state(client)["no_question_limit"] is True
    assert client.get("/secret-xyz/api/export").json()["rules"]["no_question_limit"] is True
    post(client, type="question_limit", no_question_limit=False)
    post(client, type="undo")  # 取り消しで ON に戻る
    assert _full_state(client)["no_question_limit"] is True


def test_roster_accepts_six_fullwidth_chars(client):
    r = post(client, type="roster", team=0, players=[["寿限無寿限無", "abcdefghijkl"]] + [["", ""]] * 4)
    assert r.status_code == 200
    assert r.json()["teams"][0]["seats"][0]["players"] == ["寿限無寿限無", "abcdefghijkl"]
