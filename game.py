"""AQL 2チーム戦の得点ロジック（状態は dict、関数は新しい dict を返す）。

ルールは aql-principles-and-practices/rules/game-rules.md に従う。
- チーム得点 = 5枠の得点の積（勝利点で頭打ち。勝利点は通常200、オプションでチームごとに変更可）
- 正解: 枠の得点 +1
- 誤答: 枠の得点を1に戻し、×を1つ増やす。×2で封鎖。相手チームの封鎖枠は×1に戻る
- 勝敗: 勝利点到達 / 相手の全枠封鎖 / 規定問題数終了時の得点比較（オプションで規定問題数による終了を外せる）
"""

import copy

SEATS = 5
WIN_SCORE = 200
PLAYERS_PER_SEAT = 2
NAME_WIDTH = 12  # 全角6文字（半角は1、全角は2として数える）
TARGET_MIN, TARGET_MAX = 2, 99999


class RuleError(ValueError):
    pass


def empty_roster():
    return [[""] * PLAYERS_PER_SEAT for _ in range(SEATS)]


TITLE_MAX = 40


def new_state(names=("チームA", "チームB"), max_questions=40, rosters=None, title="",
              custom_target=False, targets=(WIN_SCORE, WIN_SCORE), no_question_limit=False):
    rosters = rosters or [empty_roster(), empty_roster()]
    return {
        "teams": [
            {"name": n, "seats": [{"score": 1, "cross": 0, "players": list(p)} for p in r]}
            for n, r in zip(names, rosters)
        ],
        "title": title,
        "custom_target": custom_target,
        "targets": list(targets),
        "no_question_limit": no_question_limit,
        "question": 0,
        "max_questions": max_questions,
        "log": [],
    }


def product(team):
    """5枠の得点の積（頭打ち前）。"""
    total = 1
    for s in team["seats"]:
        total *= s["score"]
    return total


def targets(state):
    """各チームの勝利点。オプションルールが OFF なら両チーム200。古い状態は既定値。"""
    if not state.get("custom_target"):
        return [WIN_SCORE, WIN_SCORE]
    return list(state.get("targets", [WIN_SCORE, WIN_SCORE]))


def team_score(team, target=WIN_SCORE):
    return min(product(team), target)


def is_locked(seat):
    return seat["cross"] >= 2


def is_reach(team, i, target=WIN_SCORE):
    """その枠が次に正解すると勝利点に届くか。"""
    seat = team["seats"][i]
    if is_locked(seat):
        return False
    return product(team) // seat["score"] * (seat["score"] + 1) >= target


def result(state):
    """試合が終わっていれば {"winner": 0|1|None, "reason": str}、続行中なら None。"""
    a, b = state["teams"]
    tg = targets(state)
    reason = "勝利点到達" if state.get("custom_target") else "200点到達"
    for idx, t in enumerate((a, b)):
        if product(t) >= tg[idx]:
            return {"winner": idx, "reason": reason}
    for idx, t in enumerate((a, b)):
        if all(is_locked(s) for s in t["seats"]):
            return {"winner": 1 - idx, "reason": "全枠封鎖"}
    # 「40問で終了しない」が ON のときは規定問題数で終えない（無効になった問題の分を続けて出題できる）
    if not state.get("no_question_limit") and state["question"] >= state["max_questions"]:
        sa, sb = team_score(a, tg[0]), team_score(b, tg[1])
        winner = None if sa == sb else (0 if sa > sb else 1)
        return {"winner": winner, "reason": f"{state['max_questions']}問終了"}
    return None


def _check_playable(state, team, seat):
    if result(state) is not None:
        raise RuleError("試合は終了しています")
    if team not in (0, 1) or not 0 <= seat < SEATS:
        raise RuleError("チームまたは枠の指定が不正です")
    if is_locked(state["teams"][team]["seats"][seat]):
        raise RuleError("封鎖中の枠です")


def correct(state, team, seat):
    _check_playable(state, team, seat)
    st = copy.deepcopy(state)
    st["teams"][team]["seats"][seat]["score"] += 1
    st["question"] += 1
    st["log"].append({"q": st["question"], "team": team, "seat": seat, "kind": "correct"})
    return st


def wrong(state, team, seat):
    _check_playable(state, team, seat)
    st = copy.deepcopy(state)
    s = st["teams"][team]["seats"][seat]
    s["score"] = 1
    s["cross"] = min(s["cross"] + 1, 2)
    for other in st["teams"][1 - team]["seats"]:
        if is_locked(other):
            other["cross"] = 1
    st["question"] += 1
    st["log"].append({"q": st["question"], "team": team, "seat": seat, "kind": "wrong"})
    return st


def through(state):
    if result(state) is not None:
        raise RuleError("試合は終了しています")
    st = copy.deepcopy(state)
    st["question"] += 1
    st["log"].append({"q": st["question"], "kind": "through"})
    return st


def rename(state, names, title=None):
    """チーム名は空欄なら変えない。試合名は title が None でなければ置き換える（空欄で消える）。"""
    st = copy.deepcopy(state)
    if title is not None:
        st["title"] = title.strip()[:TITLE_MAX]
    for t, n in zip(st["teams"], names):
        n = (n or "").strip()[:30]
        if n:
            t["name"] = n
    return st


def set_targets(state, custom, values=None):
    """オプションルール（勝利点）の ON/OFF と各チームの勝利点を設定する。
    どのチームも、新しい勝利点は現在の得点（頭打ち前の積）より大きくなければならない。
    OFF にするのは両チームを200にするのと同じなので、同じ確認をする。"""
    if custom:
        if not isinstance(values, list) or len(values) != 2:
            raise RuleError("勝利点の指定が不正です")
        try:
            new = [int(v) for v in values]
        except (TypeError, ValueError):
            raise RuleError("勝利点は整数で入力してください")
        for v in new:
            if not TARGET_MIN <= v <= TARGET_MAX:
                raise RuleError(f"勝利点は{TARGET_MIN}〜{TARGET_MAX}の範囲で入力してください")
    else:
        new = [WIN_SCORE, WIN_SCORE]
    for t, v in zip(state["teams"], new):
        if v <= product(t):
            if custom:
                raise RuleError(f"{t['name']}の勝利点は、現在の得点（{product(t)}点）より大きくしてください")
            raise RuleError(f"{t['name']}の得点が{product(t)}点で200点以上のため、オプションルールを解除できません")
    st = copy.deepcopy(state)
    st["custom_target"] = bool(custom)
    if custom:
        st["targets"] = new
    return st


def set_question_limit(state, no_limit):
    """オプションルール「規定問題数で終了しない」の ON/OFF。"""
    st = copy.deepcopy(state)
    st["no_question_limit"] = bool(no_limit)
    return st


def name_width(name):
    """半角（Latin-1 と半角カナ）を1、それ以外を2として数える。画面側の判定と同じ規則。"""
    return sum(1 if ord(c) <= 0xFF or 0xFF61 <= ord(c) <= 0xFF9F else 2 for c in name)


def roster(team):
    return [list(s.get("players", [""] * PLAYERS_PER_SEAT)) for s in team["seats"]]


def set_roster(state, team, players):
    """players: 5枠 × 最大2名の名前。空欄は空文字。"""
    if team not in (0, 1) or not isinstance(players, list) or len(players) != SEATS:
        raise RuleError("参加者名の指定が不正です")
    cleaned = []
    for i, seat in enumerate(players):
        if not isinstance(seat, list) or len(seat) > PLAYERS_PER_SEAT:
            raise RuleError(f"{i + 1}枠の参加者は{PLAYERS_PER_SEAT}名までです")
        names = [str(n or "").strip() for n in seat] + [""] * (PLAYERS_PER_SEAT - len(seat))
        for n in names:
            if name_width(n) > NAME_WIDTH:
                raise RuleError(f"「{n}」が長すぎます（全角6文字・半角12文字まで）")
        cleaned.append(names)
    st = copy.deepcopy(state)
    for seat, names in zip(st["teams"][team]["seats"], cleaned):
        seat["players"] = names
    return st


def view(state):
    """表示用に得点・リーチ・封鎖・勝敗を付け加えた dict。"""
    res = result(state)
    tg = targets(state)
    teams = []
    for t, target in zip(state["teams"], tg):
        seats = [
            # 試合が終わったらリーチは意味を持たないので出さない
            {**s, "players": roster(t)[i], "locked": is_locked(s),
             "reach": res is None and is_reach(t, i, target)}
            for i, s in enumerate(t["seats"])
        ]
        teams.append({"name": t["name"], "score": team_score(t, target), "target": target, "seats": seats,
                      "reach": any(s["reach"] for s in seats)})
    return {"title": state.get("title", ""), "custom_target": bool(state.get("custom_target")),
            "no_question_limit": bool(state.get("no_question_limit")),
            "teams": teams, "question": state["question"],
            "max_questions": state["max_questions"], "log": state["log"][-12:],
            "result": res}
