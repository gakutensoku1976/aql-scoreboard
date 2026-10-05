"""AQL スコアボード: 入力URLで正誤を記録し、閲覧URLで得点状況を表示する。

環境変数
  INPUT_PATH     入力画面のパス（既定 "input"）。公開時は推測しにくい文字列にする
  DB_PATH        SQLite ファイル（既定 "data/scoreboard.db"）
  MAX_QUESTIONS  規定問題数（既定 40）
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import game

INPUT_PATH = os.environ.get("INPUT_PATH", "input").strip("/")
DB_PATH = os.environ.get("DB_PATH", "data/scoreboard.db")
MAX_QUESTIONS = int(os.environ.get("MAX_QUESTIONS", "40"))
STATIC = Path(__file__).parent / "static"

_lock = threading.Lock()
# 記録の時刻は日本時間（コンテナの TZ 設定や tzdata に頼らない）
JST = timezone(timedelta(hours=9), "JST")


def now_jst():
    return datetime.now(JST).replace(microsecond=0)


def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS states (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1), rev INTEGER NOT NULL, body TEXT NOT NULL)")
    return conn


# 閲覧画面の表示設定。試合の履歴とは別に持ち、取り消し・リセットの対象にしない
DEFAULT_DISPLAY = {"show_question": True, "show_log": True}


def load_display():
    with _conn() as conn:
        row = conn.execute("SELECT rev, body FROM settings WHERE id = 1").fetchone()
    if row is None:
        return dict(DEFAULT_DISPLAY), 0
    return {**DEFAULT_DISPLAY, **json.loads(row[1])}, row[0]


def save_display(display):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO settings (id, rev, body) VALUES (1, 1, ?)"
            " ON CONFLICT(id) DO UPDATE SET rev = rev + 1, body = excluded.body",
            (json.dumps(display),),
        )


def load():
    """現在の状態と版番号。状態は states テーブルの最新行で、undo は最新行の削除。"""
    with _conn() as conn:
        row = conn.execute("SELECT id, body FROM states ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            body = json.dumps(game.new_state(max_questions=MAX_QUESTIONS), ensure_ascii=False)
            cur = conn.execute("INSERT INTO states (body) VALUES (?)", (body,))
            return json.loads(body), cur.lastrowid
        return json.loads(row[1]), row[0]


def push(state):
    with _conn() as conn:
        conn.execute("INSERT INTO states (body) VALUES (?)", (json.dumps(state, ensure_ascii=False),))


def pop():
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0]
        if n <= 1:
            raise game.RuleError("これ以上取り消せません")
        conn.execute("DELETE FROM states WHERE id = (SELECT MAX(id) FROM states)")


def reset(state):
    """チーム名・参加者名・空席・試合名・勝利点の設定を残して最初の状態に戻す。"""
    names = [t["name"] for t in state["teams"]]
    rosters = [game.roster(t) for t in state["teams"]]
    vacants = [game.vacancies(t) for t in state["teams"]]
    with _conn() as conn:
        conn.execute("DELETE FROM states")
        body = game.new_state(names=names, max_questions=MAX_QUESTIONS, rosters=rosters,
                              title=state.get("title", ""),
                              custom_target=bool(state.get("custom_target")),
                              targets=state.get("targets", [game.WIN_SCORE, game.WIN_SCORE]),
                              no_question_limit=bool(state.get("no_question_limit")), vacants=vacants)
        conn.execute("INSERT INTO states (body) VALUES (?)", (json.dumps(body, ensure_ascii=False),))


def snapshot(full):
    """full=True は入力画面用（すべて含む）。False は閲覧画面用で、OFF の項目を応答から外す。"""
    state, sid = load()
    display, rev = load_display()
    v = {**game.view(state), "display": display, "version": f"{sid}.{rev}"}
    if full:
        return v
    if not display["show_question"]:
        del v["question"], v["max_questions"]
        v["log"] = [{k: x for k, x in e.items() if k != "q"} for e in v["log"]]
    if not display["show_log"]:
        del v["log"]
    return v


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def no_cache(request, call_next):
    # 画面の更新や得点の変化がすぐ届くよう、ブラウザには毎回サーバーへ確認させる
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def viewer_page():
    return FileResponse(STATIC / "viewer.html")


@app.get("/api/state")
def get_state():
    return snapshot(full=False)


@app.get(f"/{INPUT_PATH}")
def input_page():
    return FileResponse(STATIC / "input.html")


class Action(BaseModel):
    type: str
    team: int | None = None
    seat: int | None = None
    names: list[str] | None = None
    title: str | None = None
    custom_target: bool | None = None
    targets: list[int] | None = None
    no_question_limit: bool | None = None
    players: list[list[str]] | None = None
    vacant: list[bool] | None = None
    show_question: bool | None = None
    show_log: bool | None = None


@app.get(f"/{INPUT_PATH}/api/state")
def get_full_state():
    return snapshot(full=True)


def annotate_last(prev, new):
    """判定で記録が1件増えたとき、その記録に判定時刻と判定後の両チームの得点を付ける。"""
    if len(new["log"]) > len(prev["log"]):
        v = game.view(new)
        new["log"][-1]["at"] = now_jst().isoformat()
        new["log"][-1]["scores"] = [t["score"] for t in v["teams"]]
    return new


@app.post(f"/{INPUT_PATH}/api/action")
def act(a: Action):
    with _lock:
        try:
            state, _ = load()
            if a.type == "correct":
                push(annotate_last(state, game.correct(state, a.team, a.seat)))
            elif a.type == "wrong":
                push(annotate_last(state, game.wrong(state, a.team, a.seat)))
            elif a.type == "through":
                push(annotate_last(state, game.through(state)))
            elif a.type == "rename":
                push(game.rename(state, a.names or [], a.title))
            elif a.type == "targets":
                push(game.set_targets(state, bool(a.custom_target), a.targets))
            elif a.type == "question_limit":
                push(game.set_question_limit(state, a.no_question_limit))
            elif a.type == "roster":
                push(game.set_roster(state, a.team, a.players, a.vacant))
            elif a.type == "display":
                display, _ = load_display()
                for key in DEFAULT_DISPLAY:
                    if getattr(a, key) is not None:
                        display[key] = getattr(a, key)
                save_display(display)
            elif a.type == "undo":
                pop()
            elif a.type == "reset":
                reset(state)
            else:
                raise game.RuleError("不明な操作です")
        except (game.RuleError, TypeError) as e:
            raise HTTPException(status_code=409, detail=str(e))
        return snapshot(full=True)


def export_record(state, exported_at):
    """試合記録（JSON）。盤面・勝敗は画面と同じ計算（game.view）から作り、判定の記録は全件を入れる。"""
    v = game.view(state)
    res = v["result"]
    teams = [t["name"] for t in state["teams"]]
    log = []
    for e in state["log"]:
        item = {"q": e.get("q"), "at": e.get("at"), "kind": e["kind"]}
        if e["kind"] != "through":
            item.update(team=e["team"], team_name=teams[e["team"]], seat=e["seat"] + 1,
                        players=[n for n in game.roster(state["teams"][e["team"]])[e["seat"]] if n])
        item["scores"] = e.get("scores")
        log.append(item)
    return {
        "format": "aql-scoreboard-record",
        "format_version": 1,
        "exported_at": exported_at.isoformat(),
        "title": state.get("title", ""),
        "rules": {"max_questions": state["max_questions"], "no_question_limit": bool(state.get("no_question_limit")),
                  "custom_target": bool(state.get("custom_target")), "targets": game.targets(state)},
        "question": state["question"],
        "result": None if res is None else {**res, "winner_name": None if res["winner"] is None else teams[res["winner"]]},
        "teams": [
            {"name": t["name"], "score": t["score"], "product": game.product(raw), "target": t["target"],
             "seats": [{"seat": i + 1, "players": [n for n in s["players"] if n], "score": s["score"],
                        "cross": s["cross"], "locked": s["locked"], "reach": s["reach"], "vacant": s["vacant"]}
                       for i, s in enumerate(t["seats"])]}
            for t, raw in zip(v["teams"], state["teams"])
        ],
        "log": log,
    }


@app.get(f"/{INPUT_PATH}/api/export")
def export():
    state, _ = load()
    now = now_jst()
    name = f"aql_record_{now:%Y%m%d_%H%M%S}.json"
    return JSONResponse(export_record(state, now), headers={"Content-Disposition": f'attachment; filename="{name}"'})
