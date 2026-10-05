import pytest

import game


def sc(seat):
    return {"score": seat["score"], "cross": seat["cross"]}


def test_initial_score_is_one():
    s = game.new_state()
    assert [game.team_score(t) for t in s["teams"]] == [1, 1]


def test_correct_multiplies():
    s = game.new_state()
    for seat in (0, 0, 1):
        s = game.correct(s, 0, seat)
    assert game.team_score(s["teams"][0]) == 3 * 2
    assert s["question"] == 3


def test_wrong_resets_seat_and_locks_on_second():
    s = game.new_state()
    s = game.correct(s, 0, 0)
    s = game.wrong(s, 0, 0)
    assert sc(s["teams"][0]["seats"][0]) == {"score": 1, "cross": 1}
    s = game.wrong(s, 0, 0)
    assert game.is_locked(s["teams"][0]["seats"][0])
    with pytest.raises(game.RuleError):
        game.correct(s, 0, 0)


def test_opponent_wrong_revives_locked_seats():
    s = game.new_state()
    s = game.wrong(s, 0, 0)
    s = game.wrong(s, 0, 0)
    s = game.wrong(s, 1, 3)
    assert sc(s["teams"][0]["seats"][0]) == {"score": 1, "cross": 1}
    assert sc(s["teams"][1]["seats"][3]) == {"score": 1, "cross": 1}


def test_reach_and_win_at_200():
    s = game.new_state()
    # 3*3*3*3*2 = 162 → 2の枠が正解すれば 243
    for seat in range(5):
        s = game.correct(s, 0, seat)
    for seat in range(4):
        s = game.correct(s, 0, seat)
    t = s["teams"][0]
    assert game.team_score(t) == 162
    assert game.is_reach(t, 4)
    assert game.is_reach(t, 0)  # 4*3*3*3*2 = 216
    s = game.correct(s, 0, 4)
    assert game.team_score(s["teams"][0]) == 200
    assert game.result(s) == {"winner": 0, "reason": "200点到達"}
    with pytest.raises(game.RuleError):
        game.through(s)


def test_not_reach_when_far():
    s = game.new_state()
    assert not game.is_reach(s["teams"][0], 0)


def test_locked_seat_is_not_reach():
    # 3*3*3*4 = 108。封鎖が無ければ 5番(1点) の正解で 216 に届く
    s = game.new_state()
    for seat in (0, 0, 1, 1, 2, 2, 3, 3, 3):
        s = game.correct(s, 0, seat)
    assert game.is_reach(s["teams"][0], 4)
    s = game.wrong(s, 0, 4)
    s = game.wrong(s, 0, 4)
    assert not game.is_reach(s["teams"][0], 4)


def test_all_locked_loses():
    s = game.new_state()
    for seat in range(5):
        s = game.wrong(s, 1, seat)
        s = game.wrong(s, 1, seat)
    assert game.result(s) == {"winner": 0, "reason": "全枠封鎖"}


def test_question_limit():
    s = game.new_state(max_questions=3)
    s = game.correct(s, 1, 0)
    s = game.through(s)
    s = game.through(s)
    assert game.result(s) == {"winner": 1, "reason": "3問終了"}


def test_question_limit_draw():
    s = game.new_state(max_questions=1)
    s = game.through(s)
    assert game.result(s)["winner"] is None


def test_roster_set_and_width_limit():
    s = game.new_state()
    players = [["永田", "ながた"], ["山田太郎", ""], ["abcdefghijkl", "ｱｲｳｴｵｶｷｸｹｺｻｼ"], ["寿限無寿限無", ""], ["鈴木", ""]]
    s = game.set_roster(s, 0, players)
    assert game.view(s)["teams"][0]["seats"][1]["players"] == ["山田太郎", ""]
    with pytest.raises(game.RuleError):
        game.set_roster(s, 0, [["寿限無寿限無五", ""]] + [["", ""]] * 4)
    with pytest.raises(game.RuleError):
        game.set_roster(s, 0, [["a", "b", "c"]] + [["", ""]] * 4)
    with pytest.raises(game.RuleError):
        game.set_roster(s, 0, [["", ""]] * 4)


def test_roster_survives_judgements():
    s = game.set_roster(game.new_state(), 1, [["a", "b"]] * 5)
    s = game.correct(s, 1, 0)
    s = game.wrong(s, 1, 0)
    assert s["teams"][1]["seats"][0]["players"] == ["a", "b"]


def test_view_handles_state_without_players():
    s = game.new_state()
    for t in s["teams"]:
        for seat in t["seats"]:
            del seat["players"]
    assert game.view(s)["teams"][0]["seats"][0]["players"] == ["", ""]


def test_title_set_clear_and_limit():
    s = game.rename(game.new_state(), [], "  503会議室 第2試合 ")
    assert game.view(s)["title"] == "503会議室 第2試合"
    s2 = game.rename(s, ["X", ""])  # title 省略時は変えない
    assert s2["title"] == "503会議室 第2試合" and s2["teams"][0]["name"] == "X"
    assert game.rename(s, [], "")["title"] == ""
    assert len(game.rename(s, [], "あ" * 50)["title"]) == game.TITLE_MAX


def test_view_handles_state_without_title():
    s = game.new_state()
    del s["title"]
    assert game.view(s)["title"] == ""


def test_view_hides_reach_after_game_ends():
    s = game.new_state()
    for seat in (0, 1, 2, 3, 4, 0, 1, 2, 3):
        s = game.correct(s, 0, seat)
    v = game.view(s)
    assert v["result"] is None and any(x["reach"] for x in v["teams"][0]["seats"])
    s = game.correct(s, 0, 4)  # 243 → 200点で勝利
    v = game.view(s)
    assert v["result"]["winner"] == 0
    assert not any(x["reach"] for t in v["teams"] for x in t["seats"])
    assert not any(t["reach"] for t in v["teams"])


def _play(s, team, seats):
    for seat in seats:
        s = game.correct(s, team, seat)
    return s


def test_custom_target_300_reach_and_no_win_at_243():
    s = game.set_targets(game.new_state(), True, [300, 200])
    s = _play(s, 0, (0, 1, 2, 3, 4, 0, 1, 2, 3))  # 3*3*3*3*2 = 162
    v = game.view(s)
    # 162 → 2点の枠が正解すると 243 < 300 なのでリーチではない。3点の枠は 216 < 300
    assert not any(x["reach"] for x in v["teams"][0]["seats"])
    s = _play(s, 0, (4,))  # 243
    v = game.view(s)
    assert v["result"] is None and v["teams"][0]["score"] == 243 and v["teams"][0]["target"] == 300
    # 243 → 3点の枠が正解すると 324 >= 300 でリーチ
    assert all(x["reach"] for x in v["teams"][0]["seats"])
    s = _play(s, 0, (0,))  # 324
    v = game.view(s)
    assert v["result"] == {"winner": 0, "reason": "勝利点到達"}
    assert v["teams"][0]["score"] == 300  # 勝利点で頭打ち


def test_custom_target_lower_and_per_team():
    s = game.set_targets(game.new_state(), True, [100, 50])
    s = _play(s, 1, (0, 1, 2, 3, 4, 0))  # B: 3*2*2*2*2 = 48
    v = game.view(s)
    assert v["result"] is None
    # B は 48 → 2点の枠で 72 >= 50、3点の枠で 64 >= 50。全枠リーチ
    assert all(x["reach"] for x in v["teams"][1]["seats"])
    assert not any(x["reach"] for x in v["teams"][0]["seats"])
    s = _play(s, 1, (1,))  # 72
    assert game.result(s) == {"winner": 1, "reason": "勝利点到達"}


def test_set_targets_rejects_at_or_below_current_score():
    s = _play(game.new_state(), 0, (0, 1, 2, 3, 4, 0, 1))  # 3*3*2*2*2 = 72
    with pytest.raises(game.RuleError, match="72点"):
        game.set_targets(s, True, [72, 200])  # 等しい値も拒否
    with pytest.raises(game.RuleError):
        game.set_targets(s, True, [50, 200])
    assert game.set_targets(s, True, [73, 200])["targets"] == [73, 200]
    with pytest.raises(game.RuleError):
        game.set_targets(s, True, [1, 200])
    with pytest.raises(game.RuleError):
        game.set_targets(s, True, [100000, 200])


def test_turning_off_requires_scores_below_200():
    s = game.set_targets(game.new_state(), True, [300, 300])
    s = _play(s, 0, (0, 1, 2, 3, 4, 0, 1, 2, 3, 4))  # 243
    with pytest.raises(game.RuleError, match="解除できません"):
        game.set_targets(s, False)
    s2 = game.set_targets(game.new_state(), True, [300, 300])
    assert game.view(game.set_targets(s2, False))["custom_target"] is False


def test_raise_target_after_win_resumes_game():
    s = _play(game.new_state(), 0, (0, 1, 2, 3, 4, 0, 1, 2, 3, 4))  # 243 → 200点で勝利
    assert game.result(s)["winner"] == 0
    with pytest.raises(game.RuleError, match="243点"):
        game.set_targets(s, True, [220, 200])  # 表示は200だが実際の積は243
    s = game.set_targets(s, True, [250, 200])
    assert game.result(s) is None and game.view(s)["teams"][0]["score"] == 243


def test_view_defaults_without_target_fields():
    s = game.new_state()
    del s["custom_target"], s["targets"]
    v = game.view(s)
    assert v["custom_target"] is False and [t["target"] for t in v["teams"]] == [200, 200]


def test_no_question_limit_continues_past_limit():
    s = game.set_question_limit(game.new_state(max_questions=3), True)
    for _ in range(5):
        s = game.through(s)  # 3問を超えても終わらない
    assert game.result(s) is None and s["question"] == 5
    s = game.correct(s, 0, 0)  # 判定もできる
    assert game.view(s)["no_question_limit"] is True
    # 勝利点への到達と全枠封鎖では従来どおり終わる
    t = game.set_targets(s, True, [3, 200])
    t = game.correct(t, 0, 0)
    assert game.result(t) == {"winner": 0, "reason": "勝利点到達"}
    # OFF に戻すと規定問題数を超えているので終了する
    assert game.result(game.set_question_limit(s, False))["reason"] == "3問終了"


def test_view_default_question_limit_for_old_state():
    s = game.new_state()
    del s["no_question_limit"]
    assert game.view(s)["no_question_limit"] is False


EMPTY = [["", ""]] * 5


def vacant_state(team=0, seats=(4,)):
    v = [i in seats for i in range(5)]
    return game.set_roster(game.new_state(), team, [list(p) for p in EMPTY], v)


def test_vacant_seat_stays_one_and_rejects_judgements():
    st = vacant_state(seats=(1, 4))
    seat = st["teams"][0]["seats"][1]
    assert seat["vacant"] and seat["score"] == 1 and seat["cross"] == 0
    for f in (game.correct, game.wrong):
        with pytest.raises(game.RuleError, match="空席"):
            f(st, 0, 1)
    st = game.correct(st, 0, 0)
    st = game.correct(st, 0, 0)
    assert game.product(st["teams"][0]) == 3
    st = game.wrong(st, 1, 0)  # 相手の誤答でも空席は変わらない
    assert sc(st["teams"][0]["seats"][1]) == {"score": 1, "cross": 0}
    assert game.view(st)["teams"][0]["seats"][1]["vacant"] is True


def test_vacant_seat_is_never_reach():
    st = vacant_state(seats=(4,))
    for s, n in zip(st["teams"][0]["seats"], (5, 5, 4, 1)):
        s["score"] = n  # 積100。得点1の枠が +1 すると200に届く
    v = game.view(st)
    assert v["teams"][0]["seats"][3]["reach"] is True
    assert v["teams"][0]["seats"][4]["reach"] is False
    assert v["teams"][0]["seats"][4]["vacant"] is True


def test_all_locked_ignores_vacant_seats():
    st = vacant_state(team=1, seats=(2, 3, 4))
    for seat in (0, 1):
        st = game.wrong(st, 1, seat)
        st = game.through(st)
        st = game.wrong(st, 1, seat)
    assert game.result(st) == {"winner": 0, "reason": "全枠封鎖"}


def test_vacant_change_only_before_game_starts():
    st = game.through(vacant_state(seats=(4,)))
    with pytest.raises(game.RuleError, match="試合開始前"):
        game.set_roster(st, 0, [list(p) for p in EMPTY], [False] * 5)
    # 空席が同じなら名前は試合中でも変えられる（None も同じ扱い）
    names = [["a", ""], ["", ""], ["", ""], ["", ""], ["x", ""]]
    st2 = game.set_roster(st, 0, names, [False, False, False, False, True])
    assert game.roster(st2["teams"][0])[0] == ["a", ""]
    assert game.roster(st2["teams"][0])[4] == ["", ""]  # 空席の名前は消す
    assert game.vacancies(game.set_roster(st, 0, names)["teams"][0])[4] is True


def test_all_vacant_rejected():
    with pytest.raises(game.RuleError, match="すべての枠"):
        game.set_roster(game.new_state(), 0, [list(p) for p in EMPTY], [True] * 5)
    with pytest.raises(game.RuleError, match="空席の指定"):
        game.set_roster(game.new_state(), 0, [list(p) for p in EMPTY], [True] * 3)


def test_view_handles_state_without_vacant():
    st = game.new_state()
    for t in st["teams"]:
        for s in t["seats"]:
            del s["vacant"]
    assert all(not s["vacant"] for t in game.view(st)["teams"] for s in t["seats"])
    st = game.correct(st, 0, 0)
    assert game.product(st["teams"][0]) == 2
