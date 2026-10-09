import importlib

import pytest
from fastapi.testclient import TestClient


ADMIN = "admin-pw"
GAME, PW = "t1", "pass-t1"


def load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    import app as app_module
    importlib.reload(app_module)
    return app_module


def admin_login(c):
    assert c.post("/admin/login", json={"password": ADMIN}).status_code == 200


def create(c, gid, pw, title=""):
    return c.post("/admin/api/games", json={"id": gid, "password": pw, "title": title})


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    return load_app(tmp_path, monkeypatch)


@pytest.fixture
def client(app_module):
    """管理者で試合 t1 を作り、t1 の入力画面にログインした状態。"""
    c = TestClient(app_module.app)
    admin_login(c)
    assert create(c, GAME, PW).status_code == 200
    assert c.post(f"/input/{GAME}/login", json={"password": PW}).status_code == 200
    return c


def post(c, gid=GAME, **body):
    return c.post(f"/input/{gid}/api/action", json=body)


def test_viewer_and_input_pages(client):
    assert client.get("/").status_code == 200
    assert client.get("/t1").status_code == 200
    assert client.get("/input/t1").status_code == 200
    assert 'id="through"' in client.get("/input/t1").text  # ログイン済みなら入力画面
    assert client.get("/nothing").status_code == 404
    assert client.get("/input/nothing").status_code == 404
    assert client.post("/input/nothing/api/action", json={"type": "through"}).status_code == 404


def test_action_updates_state_and_undo(client):
    v0 = client.get("/api/games/t1/state").json()["version"]
    r = post(client, type="correct", team=0, seat=2)
    assert r.status_code == 200
    s = client.get("/api/games/t1/state").json()
    assert s["teams"][0]["score"] == 2 and s["version"] != v0
    post(client, type="undo")
    assert client.get("/api/games/t1/state").json()["teams"][0]["score"] == 1
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
    s = client.get("/input/t1/api/state").json()
    assert [t["name"] for t in s["teams"]] == ["QUAPS", "チームB"]
    assert s["question"] == 0 and s["log"] == []


def test_reset_keeps_roster(client):
    players = [["永田", ""], ["", ""], ["", ""], ["", ""], ["", "佐藤"]]
    assert post(client, type="roster", team=0, players=players).status_code == 200
    post(client, type="correct", team=0, seat=4)
    post(client, type="reset")
    s = client.get("/api/games/t1/state").json()
    assert [seat["players"] for seat in s["teams"][0]["seats"]] == players
    assert s["teams"][0]["score"] == 1


def test_roster_too_long_rejected(client):
    r = post(client, type="roster", team=0, players=[["とても長すぎる名", ""]] + [["", ""]] * 4)
    assert r.status_code == 409


def test_display_default_off_and_toggle(client):
    post(client, type="correct", team=0, seat=0)
    post(client, type="through")
    # 既定はどちらも表示しない。閲覧用 API の応答からも外れる
    v = client.get("/api/games/t1/state").json()
    assert "question" not in v and "max_questions" not in v and "log" not in v
    full = client.get("/input/t1/api/state").json()
    assert full["question"] == 2 and len(full["log"]) == 2  # 入力画面には残る
    assert full["display"] == {"show_question": False, "show_log": False}

    post(client, type="display", show_question=True, show_log=True)
    v = client.get("/api/games/t1/state").json()
    assert v["question"] == 2 and v["log"][0]["q"] == 1

    r = post(client, type="display", show_question=False)
    assert r.status_code == 200 and r.json()["question"] == 2
    v = client.get("/api/games/t1/state").json()
    assert "question" not in v and "max_questions" not in v
    assert all("q" not in e for e in v["log"])  # 問題数を出さないときは記録の問題番号も外す

    post(client, type="display", show_log=False)
    v = client.get("/api/games/t1/state").json()
    assert "log" not in v and "question" not in v


def test_display_change_bumps_version_and_survives_undo_reset(client):
    v0 = client.get("/api/games/t1/state").json()["version"]
    post(client, type="display", show_log=True)
    v1 = client.get("/api/games/t1/state").json()["version"]
    assert v1 != v0  # 閲覧画面が再描画されるように版が変わる
    post(client, type="correct", team=1, seat=1)
    post(client, type="undo")
    post(client, type="reset")
    assert client.get("/input/t1/api/state").json()["display"]["show_log"] is True


def test_full_state_requires_login(app_module, client):
    other = TestClient(app_module.app)
    assert other.get("/input/t1/api/state").status_code == 401
    assert other.get("/input/t1/api/export").status_code == 401
    assert other.post("/input/t1/api/action", json={"type": "through"}).status_code == 401
    assert 'id="through"' not in other.get("/input/t1").text  # ログイン画面
    assert other.get("/api/games/t1/state").status_code == 200  # 閲覧は認証なし


def test_title_shown_to_viewer_and_kept_on_reset(client):
    post(client, type="rename", title="503会議室 第2試合", names=["", ""])
    post(client, type="correct", team=0, seat=0)
    post(client, type="reset")
    v = client.get("/api/games/t1/state").json()
    assert v["title"] == "503会議室 第2試合"
    assert [t["name"] for t in v["teams"]] == ["チームA", "チームB"]


def test_responses_require_revalidation(client):
    for url in ("/", "/t1", "/input/t1", "/static/board.js", "/api/games/t1/state"):
        assert client.get(url).headers["cache-control"] == "no-cache"


def test_targets_action_undo_and_reset(client):
    r = post(client, type="targets", custom_target=True, targets=[300, 150])
    assert r.status_code == 200
    v = client.get("/api/games/t1/state").json()
    assert v["custom_target"] is True and [t["target"] for t in v["teams"]] == [300, 150]
    post(client, type="correct", team=0, seat=0)
    post(client, type="reset")  # リセットしても勝利点の設定は残る
    v = client.get("/api/games/t1/state").json()
    assert v["custom_target"] is True and [t["target"] for t in v["teams"]] == [300, 150]
    post(client, type="targets", custom_target=True, targets=[400, 150])
    post(client, type="undo")  # 取り消しで変更前に戻る
    assert [t["target"] for t in client.get("/api/games/t1/state").json()["teams"]] == [300, 150]


def test_targets_rejected_with_message(client):
    for _ in range(3):
        post(client, type="correct", team=1, seat=0)  # B = 4
    r = post(client, type="targets", custom_target=True, targets=[200, 4])
    assert r.status_code == 409 and "現在の得点（4点）" in r.json()["detail"]
    assert client.get("/api/games/t1/state").json()["custom_target"] is False


def _full_state(client):
    return client.get("/input/t1/api/state").json()


def test_judgements_get_time_and_scores_but_settings_do_not(client):
    post(client, type="rename", title="第1試合", names=["A", "B"])
    post(client, type="correct", team=0, seat=1)
    post(client, type="wrong", team=1, seat=0)
    post(client, type="through")
    post(client, type="targets", custom_target=True, targets=[300, 200])
    log = client.get("/input/t1/api/export").json()["log"]
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
    r = client.get("/input/t1/api/export")
    assert r.status_code == 200
    assert 'attachment; filename="aql_record_t1_' in r.headers["content-disposition"]
    d = r.json()
    assert d["format"] == "aql-scoreboard-record" and d["format_version"] == 1 and d["game_id"] == "t1"
    assert d["exported_at"].endswith("+09:00")
    assert d["title"] == "503会議室 第2試合"
    assert d["rules"] == {"max_questions": 40, "no_question_limit": False, "custom_target": True, "targets": [300, 150]}
    assert d["question"] == 16 and d["result"] is None
    a, b = d["teams"]
    assert (a["name"], a["score"], a["product"], a["target"]) == ("中日", 8, 8, 300)
    assert a["seats"][0] == {"seat": 1, "players": ["彦野", "仁村"], "score": 8, "cross": 0, "locked": False, "reach": False, "vacant": False}
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
    d = client.get("/input/t1/api/export").json()
    assert d["result"] == {"winner": 0, "winner_name": "チームA", "reason": "勝利点到達"}
    post(client, type="undo")
    assert len(client.get("/input/t1/api/export").json()["log"]) == 1  # 取り消した判定は残らない
    post(client, type="reset")
    d = client.get("/input/t1/api/export").json()
    assert d["log"] == [] and d["rules"]["targets"] == [3, 200]


def test_old_log_entries_export(app_module):
    st = app_module.game.correct(app_module.game.new_state(), 0, 0)  # 時刻・得点のない古い形式の記録
    entry = app_module.export_record(st, app_module.now_jst())["log"][0]
    assert entry["at"] is None and entry["scores"] is None


def test_question_limit_option_action_undo_reset_export(client):
    r = post(client, type="question_limit", no_question_limit=True)
    assert r.status_code == 200 and r.json()["no_question_limit"] is True
    assert client.get("/api/games/t1/state").json()["no_question_limit"] is True
    post(client, type="through")
    post(client, type="reset")  # リセットしても残る
    assert _full_state(client)["no_question_limit"] is True
    assert client.get("/input/t1/api/export").json()["rules"]["no_question_limit"] is True
    post(client, type="question_limit", no_question_limit=False)
    post(client, type="undo")  # 取り消しで ON に戻る
    assert _full_state(client)["no_question_limit"] is True


def test_roster_accepts_six_fullwidth_chars(client):
    r = post(client, type="roster", team=0, players=[["寿限無寿限無", "abcdefghijkl"]] + [["", ""]] * 4)
    assert r.status_code == 200
    assert r.json()["teams"][0]["seats"][0]["players"] == ["寿限無寿限無", "abcdefghijkl"]


def test_vacant_roster_action_reset_undo_export(client):
    names = [["", ""]] * 5
    vac = [False, False, False, True, True]
    assert post(client, type="roster", team=1, players=names, vacant=vac).status_code == 200
    s = client.get("/api/games/t1/state").json()
    assert [x["vacant"] for x in s["teams"][1]["seats"]] == vac
    assert not any(x["vacant"] for x in s["teams"][0]["seats"])
    assert post(client, type="correct", team=1, seat=3).status_code == 409
    post(client, type="correct", team=1, seat=0)
    r = post(client, type="roster", team=1, players=names, vacant=[False] * 5)
    assert r.status_code == 409 and "試合開始前" in r.json()["detail"]
    d = client.get("/input/t1/api/export").json()
    assert [x["vacant"] for x in d["teams"][1]["seats"]] == vac
    post(client, type="reset")
    s = client.get("/input/t1/api/state").json()
    assert [x["vacant"] for x in s["teams"][1]["seats"]] == vac and s["question"] == 0
    post(client, type="roster", team=1, players=names, vacant=[False] * 5)
    post(client, type="undo")
    assert [x["vacant"] for x in client.get("/api/games/t1/state").json()["teams"][1]["seats"]] == vac


# ===== 複数試合・ログイン =====

def test_input_login_wrong_password_and_throttle(app_module, client):
    other = TestClient(app_module.app)
    for _ in range(app_module.MAX_FAILURES):
        assert other.post("/input/t1/login", json={"password": "wrong"}).status_code == 401
    # 続けて間違えたら、正しいパスワードでもしばらく受け付けない
    assert other.post("/input/t1/login", json={"password": PW}).status_code == 429
    app_module._failures.clear()
    assert other.post("/input/t1/login", json={"password": PW}).status_code == 200
    assert other.get("/input/t1/api/state").status_code == 200
    assert other.post("/input/t1/logout").status_code == 200
    assert other.get("/input/t1/api/state").status_code == 401


def test_admin_required(app_module):
    c = TestClient(app_module.app)
    assert "管理者パスワード" in c.get("/admin").text  # ログイン画面
    assert c.get("/admin/api/games").status_code == 401
    assert create(c, "x1", "pass").status_code == 401
    assert c.delete("/admin/api/games/x1").status_code == 401
    assert c.post("/admin/login", json={"password": "nope"}).status_code == 401
    admin_login(c)
    assert "試合の作成" in c.get("/admin").text
    assert c.get("/admin/api/games").json() == {"games": []}
    c.post("/admin/logout")
    assert c.get("/admin/api/games").status_code == 401


def test_admin_disabled_without_password(tmp_path, monkeypatch):
    m = load_app(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "ADMIN_PASSWORD", "")
    c = TestClient(m.app)
    assert c.get("/admin").status_code == 503
    assert c.post("/admin/login", json={"password": ""}).status_code == 503


def test_game_id_validation(client):
    for bad in ("", "a b", "日本語", "x" * 33, "a/b"):
        r = create(client, bad, "pass")
        assert r.status_code in (409, 404, 405), bad
    for word in ("admin", "INPUT", "static", "api"):
        assert "使えません" in create(client, word, "pass").json()["detail"]
    assert "すでに" in create(client, "T1", "pass").json()["detail"]  # 大文字は小文字にそろえる
    assert "パスワード" in create(client, "t2", "abc").json()["detail"]
    r = create(client, "Room_A-2", "pass", "第2試合")
    assert r.status_code == 200 and r.json()["id"] == "room_a-2"
    assert client.get("/api/games/room_a-2/state").json()["title"] == "第2試合"
    assert client.get("/ROOM_A-2").status_code == 200


def test_games_are_independent(app_module, client):
    create(client, "t2", "pass-t2")
    assert post(client, gid="t2", type="through").status_code == 401  # t1 のログインでは t2 に書けない
    assert client.post("/input/t2/login", json={"password": PW}).status_code == 401
    assert client.post("/input/t2/login", json={"password": "pass-t2"}).status_code == 200
    post(client, type="correct", team=0, seat=0)
    post(client, gid="t2", type="wrong", team=1, seat=1)
    post(client, gid="t2", type="display", show_log=True)
    a = client.get("/api/games/t1/state").json()
    b = client.get("/api/games/t2/state").json()
    assert a["teams"][0]["score"] == 2 and b["teams"][0]["score"] == 1
    assert b["teams"][1]["seats"][1]["cross"] == 1 and a["teams"][1]["seats"][1]["cross"] == 0
    assert "log" not in a and "log" in b
    post(client, type="undo")
    post(client, gid="t2", type="through")
    post(client, type="reset")
    assert client.get("/input/t2/api/state").json()["question"] == 2
    games = {g["id"]: g for g in client.get("/admin/api/games").json()["games"]}
    assert games["t2"]["question"] == 2 and games["t1"]["question"] == 0


def test_delete_game_and_recreate_invalidates_login(app_module, client):
    post(client, type="correct", team=0, seat=0)
    assert client.delete("/admin/api/games/t1").status_code == 200
    assert client.delete("/admin/api/games/t1").status_code == 404
    assert client.get("/api/games/t1/state").status_code == 404
    assert client.get("/t1").status_code == 404
    assert post(client, type="through").status_code == 404
    create(client, "t1", PW)  # 同じIDとパスワードで作り直しても、古いログインは通らない
    assert post(client, type="through").status_code == 401
    s = client.get("/api/games/t1/state").json()
    assert s["teams"][0]["score"] == 1  # 前の試合の状態は残らない
    assert {g["id"]: g for g in client.get("/admin/api/games").json()["games"]}["t1"]["question"] == 0


def test_login_survives_restart_and_expires(tmp_path, monkeypatch, app_module, client):
    m = load_app(tmp_path, monkeypatch)  # 再起動（署名鍵は DB に保存されている）
    c2 = TestClient(m.app, cookies=dict(client.cookies))
    assert c2.get("/input/t1/api/state").status_code == 200
    real = m.time.time
    monkeypatch.setattr(m.time, "time", lambda: real() + m.SESSION_SECONDS + 1)
    assert c2.get("/input/t1/api/state").status_code == 401


def test_legacy_single_game_db_is_kept_aside(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE states (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)")
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id = 1), rev INTEGER NOT NULL, body TEXT NOT NULL)")
    conn.execute("INSERT INTO states (body) VALUES ('{}')")
    conn.commit()
    conn.close()
    m = load_app(tmp_path, monkeypatch)
    c = TestClient(m.app)
    admin_login(c)
    assert create(c, "a", "pass").status_code == 200
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"legacy_states", "legacy_settings", "games", "states", "settings"} <= names
    assert conn.execute("SELECT COUNT(*) FROM legacy_states").fetchone()[0] == 1


def test_viewer_has_qr_menu_and_library(client):
    html = client.get("/t1").text
    assert 'id="qr"' in html and "QRコードを表示" in html
    assert 'src="static/vendor/qrcode-generator-2.0.4.js"' in html
    assert client.get("/static/vendor/qrcode-generator-2.0.4.js").status_code == 200


def test_input_has_viewer_qr_button(client):
    html = client.get("/input/t1").text
    assert html.index('id="qr"') < html.index('id="export"')  # 「試合記録をダウンロード」の左
    assert "表示画面QRコード" in html
    assert 'src="../static/vendor/qrcode-generator-2.0.4.js"' in html
