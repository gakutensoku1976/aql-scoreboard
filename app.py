"""AQL スコアボード: 試合ごとに、入力画面で正誤を記録し、閲覧画面で得点状況を表示する。

URL（本番では先頭に /score が付く）
  /             試合IDを入れて閲覧画面へ
  /<ID>         閲覧画面（認証なし）
  /input        出題者の入口（既存の試合の入力画面へ移る／作成用の合言葉で新しい試合を作る）
  /input/<ID>   入力画面（試合ごとのパスワードでログイン）
  /manual       マニュアル（出題者の入口・入力画面からリンク）
  /admin        管理者画面（試合の作成・一覧・削除。ADMIN_PASSWORD でログイン）

環境変数
  ADMIN_PASSWORD 管理者パスワード（未設定なら管理者画面は使えない）
  SECRET_KEY     ログイン状態の署名鍵（未設定なら初回に作って DB に保存）
  COOKIE_SECURE  ログインの Cookie に Secure を付けるか（既定 "1"。手元で http://localhost を使うときは "0"）
  TRUSTED_PROXIES 前に立つプロキシの段数（既定 0）。本番は CloudFront と nginx の 2。接続元の IP を
                 X-Forwarded-For の後ろからこの段数だけ戻って取る（前の方は利用者が偽れるので使わない）
  DB_PATH        SQLite ファイル（既定 "data/scoreboard.db"）
  MAX_QUESTIONS  規定問題数（既定 40）
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import game

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"
TRUSTED_PROXIES = int(os.environ.get("TRUSTED_PROXIES", "0"))
DB_PATH = os.environ.get("DB_PATH", "data/scoreboard.db")
MAX_QUESTIONS = int(os.environ.get("MAX_QUESTIONS", "40"))
STATIC = Path(__file__).parent / "static"

_lock = threading.Lock()
# 記録の時刻は日本時間（コンテナの TZ 設定や tzdata に頼らない）
JST = timezone(timedelta(hours=9), "JST")


def now_jst():
    return datetime.now(JST).replace(microsecond=0)


# ===== DB =====
# games: 試合ID・入力用パスワードのハッシュ。states: 試合ごとの状態の履歴（undo は最新行の削除）。
# settings: 試合ごとの閲覧画面の表示設定（履歴とは別に持ち、取り消し・リセットの対象にしない）。meta: 署名鍵
_ready = False


def _conn():
    global _ready
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    if not _ready:
        with conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(states)")]
            if cols and "game_id" not in cols:
                # 1試合版の DB（QLA2026 まで）は消さずに名前を変えて残す
                conn.execute("ALTER TABLE states RENAME TO legacy_states")
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'settings'").fetchone():
                    conn.execute("ALTER TABLE settings RENAME TO legacy_settings")
            conn.execute("CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS states (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL, body TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS states_game ON states (game_id, id)")
            conn.execute("CREATE TABLE IF NOT EXISTS settings (game_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, body TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            # 誰が作った試合か（admin=管理者画面、input=出題者の入口で合言葉を使って作成）
            if "created_by" not in [r[1] for r in conn.execute("PRAGMA table_info(games)")]:
                conn.execute("ALTER TABLE games ADD COLUMN created_by TEXT NOT NULL DEFAULT 'admin'")
            # 最後に得点を入力（○・×・スルー）した時刻。リセット・取り消しでは戻さない（削除の判断に使う）
            if "last_input_at" not in [r[1] for r in conn.execute("PRAGMA table_info(games)")]:
                conn.execute("ALTER TABLE games ADD COLUMN last_input_at TEXT")
                # 既存の試合は、今の判定の記録に残る最後の時刻から始める
                for (gid,) in conn.execute("SELECT id FROM games").fetchall():
                    row = conn.execute("SELECT body FROM states WHERE game_id = ? ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
                    ats = [e["at"] for e in json.loads(row[0]).get("log", []) if e.get("at")] if row else []
                    if ats:
                        conn.execute("UPDATE games SET last_input_at = ? WHERE id = ?", (max(ats), gid))
        _ready = True
    return conn


DEFAULT_DISPLAY = {"show_question": False, "show_log": False}  # 既定はどちらも表示しない


def load_display(gid):
    with _conn() as conn:
        row = conn.execute("SELECT rev, body FROM settings WHERE game_id = ?", (gid,)).fetchone()
    if row is None:
        return dict(DEFAULT_DISPLAY), 0
    return {**DEFAULT_DISPLAY, **json.loads(row[1])}, row[0]


def save_display(gid, display):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO settings (game_id, rev, body) VALUES (?, 1, ?)"
            " ON CONFLICT(game_id) DO UPDATE SET rev = rev + 1, body = excluded.body",
            (gid, json.dumps(display)),
        )


def load(gid):
    """その試合の現在の状態と版番号。状態は states テーブルの最新行。"""
    with _conn() as conn:
        row = conn.execute("SELECT id, body FROM states WHERE game_id = ? ORDER BY id DESC LIMIT 1", (gid,)).fetchone()
        if row is None:
            body = json.dumps(game.new_state(max_questions=MAX_QUESTIONS), ensure_ascii=False)
            cur = conn.execute("INSERT INTO states (game_id, body) VALUES (?, ?)", (gid, body))
            return json.loads(body), cur.lastrowid
        return json.loads(row[1]), row[0]


def push(gid, state):
    with _conn() as conn:
        conn.execute("INSERT INTO states (game_id, body) VALUES (?, ?)", (gid, json.dumps(state, ensure_ascii=False)))


def pop(gid):
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM states WHERE game_id = ?", (gid,)).fetchone()[0]
        if n <= 1:
            raise game.RuleError("これ以上取り消せません")
        conn.execute("DELETE FROM states WHERE id = (SELECT MAX(id) FROM states WHERE game_id = ?)", (gid,))


def reset(gid, state):
    """チーム名・エンブレム・参加者名・空席・試合名・勝利点の設定を残して最初の状態に戻す。"""
    names = [t["name"] for t in state["teams"]]
    rosters = [game.roster(t) for t in state["teams"]]
    vacants = [game.vacancies(t) for t in state["teams"]]
    with _conn() as conn:
        conn.execute("DELETE FROM states WHERE game_id = ?", (gid,))
        body = game.new_state(names=names, max_questions=MAX_QUESTIONS, rosters=rosters,
                              title=state.get("title", ""),
                              custom_target=bool(state.get("custom_target")),
                              targets=state.get("targets", [game.WIN_SCORE, game.WIN_SCORE]),
                              no_question_limit=bool(state.get("no_question_limit")), vacants=vacants,
                              emblems=game.emblems_of(state))
        conn.execute("INSERT INTO states (game_id, body) VALUES (?, ?)", (gid, json.dumps(body, ensure_ascii=False)))


def snapshot(gid, full):
    """full=True は入力画面用（すべて含む）。False は閲覧画面用で、OFF の項目を応答から外す。"""
    state, sid = load(gid)
    display, rev = load_display(gid)
    v = {**game.view(state), "game_id": gid, "display": display, "version": f"{sid}.{rev}"}
    if full:
        return v
    if not display["show_question"]:
        del v["question"], v["max_questions"]
        v["log"] = [{k: x for k, x in e.items() if k != "q"} for e in v["log"]]
    if not display["show_log"]:
        del v["log"]
    return v


# ===== 試合（作成・一覧・削除） =====
ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
RESERVED = {"admin", "input", "static", "api", "manual"}
PW_MIN, PW_MAX = 4, 64


def norm_id(gid):
    return (gid or "").strip().lower()


def pw_hash_of(gid):
    with _conn() as conn:
        row = conn.execute("SELECT pw_hash FROM games WHERE id = ?", (gid,)).fetchone()
    return row[0] if row else None


def hash_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw, stored):
    _, salt, h = stored.split("$")
    got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(got.hex(), h)


def create_game(gid, pw, title="", created_by="admin"):
    gid = norm_id(gid)
    if not ID_RE.match(gid):
        raise game.RuleError("試合IDは半角英数字と - _ で1〜32文字にしてください")
    if gid in RESERVED:
        raise game.RuleError(f"「{gid}」は試合IDに使えません")
    if not PW_MIN <= len(pw or "") <= PW_MAX:
        raise game.RuleError(f"パスワードは{PW_MIN}〜{PW_MAX}文字にしてください")
    body = game.new_state(max_questions=MAX_QUESTIONS, title=(title or "").strip()[:game.TITLE_MAX])
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM games WHERE id = ?", (gid,)).fetchone():
            raise game.RuleError(f"試合ID「{gid}」はすでにあります")
        conn.execute("DELETE FROM states WHERE game_id = ?", (gid,))
        conn.execute("DELETE FROM settings WHERE game_id = ?", (gid,))
        if created_by != "admin" and conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] >= MAX_GAMES:
            raise game.RuleError(f"試合の数が上限（{MAX_GAMES}件）に達しています。運営に連絡してください")
        conn.execute("INSERT INTO games (id, pw_hash, created_at, created_by) VALUES (?, ?, ?, ?)",
                     (gid, hash_password(pw), now_jst().isoformat(), created_by))
        conn.execute("INSERT INTO states (game_id, body) VALUES (?, ?)", (gid, json.dumps(body, ensure_ascii=False)))
    return gid


def delete_game(gid):
    with _conn() as conn:
        n = conn.execute("DELETE FROM games WHERE id = ?", (gid,)).rowcount
        conn.execute("DELETE FROM states WHERE game_id = ?", (gid,))
        conn.execute("DELETE FROM settings WHERE game_id = ?", (gid,))
    for k in [k for k in _failures if k.startswith(f"in:{gid}|")]:
        _failures.pop(k, None)
    return n > 0


def list_games():
    with _conn() as conn:
        rows = conn.execute("SELECT id, created_at, created_by, last_input_at FROM games ORDER BY created_at, id").fetchall()
    out = []
    for gid, created, created_by, last_input in rows:
        v = game.view(load(gid)[0])
        res = v["result"]
        out.append({
            "id": gid, "created_at": created, "created_by": created_by, "last_input_at": last_input, "title": v["title"],
            "teams": [{"name": t["name"], "score": t["score"]} for t in v["teams"]],
            "question": v["question"], "max_questions": v["max_questions"], "no_question_limit": v["no_question_limit"],
            "result": None if res is None else {**res, "winner_name": None if res["winner"] is None else v["teams"][res["winner"]]["name"]},
        })
    return out


# ===== 作成用の合言葉（出題者が /input で試合を作るため） =====
# 管理者画面で設定・変更・解除する。ハッシュだけを meta に置く。未設定なら出題者は作成できない
MAX_GAMES = 100  # 出題者が作れる上限（全試合の数で数える。管理者の作成には効かない）
CODE_KEY = "create_code"


def create_code():
    """{"hash": ..., "updated_at": ...} または None。"""
    with _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (CODE_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def set_create_code(code):
    if code is None:
        with _conn() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (CODE_KEY,))
        return
    if not PW_MIN <= len(code) <= PW_MAX:
        raise game.RuleError(f"合言葉は{PW_MIN}〜{PW_MAX}文字にしてください")
    value = json.dumps({"hash": hash_password(code), "updated_at": now_jst().isoformat()})
    with _conn() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (CODE_KEY, value))


def code_status():
    c = create_code()
    return {"set": c is not None, "updated_at": c["updated_at"] if c else None}


# ===== ログイン（署名付き Cookie） =====
SESSION_SECONDS = 24 * 60 * 60
MAX_FAILURES, LOCK_SECONDS = 5, 30
ADMIN_COOKIE = "aqlsb_admin"
_failures = {}  # "対象|接続元IP" -> [連続失敗回数, この時刻まで受け付けない]
_secret = None


def secret_key():
    global _secret
    if _secret is None:
        env = os.environ.get("SECRET_KEY")
        if env:
            _secret = env.encode()
        else:
            with _conn() as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = 'secret'").fetchone()
                if row is None:
                    value = secrets.token_hex(32)
                    conn.execute("INSERT INTO meta (key, value) VALUES ('secret', ?)", (value,))
                else:
                    value = row[0]
            _secret = value.encode()
    return _secret


def _sign(scope, issued, bound):
    """bound: パスワードのハッシュ。パスワード（試合）が変われば古いログインは通らない。"""
    return hmac.new(secret_key(), f"{scope}|{issued}|{bound}".encode(), hashlib.sha256).hexdigest()


def make_token(scope, bound):
    issued = int(time.time())
    return f"{issued}.{_sign(scope, issued, bound)}"


def check_token(token, scope, bound):
    try:
        issued, sig = (token or "").split(".")
        issued = int(issued)
    except ValueError:
        return False
    if time.time() - issued > SESSION_SECONDS:
        return False
    return hmac.compare_digest(sig, _sign(scope, issued, bound))


def input_cookie(gid):
    return f"aqlsb_in_{gid}"


def admin_bound():
    return hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()


def client_ip(request):
    """接続元の IP。プロキシ（CloudFront・nginx）は X-Forwarded-For の後ろに順に IP を足すので、
    後ろから TRUSTED_PROXIES 段だけ戻った位置が利用者の IP。それより前は利用者が自由に書けるので使わない。"""
    chain = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
    chain.append(request.client.host if request.client else "")
    return chain[max(0, len(chain) - 1 - TRUSTED_PROXIES)]


# 失敗の回数は「対象（試合・管理者・作成）× 接続元の IP」ごとに数える。
# 対象だけで数えると、第三者がわざと間違え続けて正しい利用者を締め出せてしまうため
def _key(key, request):
    return f"{key}|{client_ip(request)}"


def _throttle(key, request):
    count, until = _failures.get(_key(key, request), [0, 0.0])
    if time.monotonic() < until:
        raise HTTPException(status_code=429, detail="パスワードを続けて間違えたため、しばらく待ってから入力してください")


def _failed(key, request):
    if len(_failures) > 10000:  # 記録が増えすぎたら、止めている時間の過ぎたものを捨てる
        now = time.monotonic()
        for k in [k for k, (n, until) in _failures.items() if until < now]:
            del _failures[k]
    k = _key(key, request)
    entry = _failures.setdefault(k, [0, 0.0])
    entry[0] += 1
    if entry[0] >= MAX_FAILURES:
        _failures[k] = [0, time.monotonic() + LOCK_SECONDS]


def _succeeded(key, request):
    _failures.pop(_key(key, request), None)


def _set_cookie(response, name, token):
    response.set_cookie(name, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax", path="/",
                        secure=COOKIE_SECURE)


def is_admin(request):
    return bool(ADMIN_PASSWORD) and check_token(request.cookies.get(ADMIN_COOKIE), "admin", admin_bound())


def is_input(request, gid):
    h = pw_hash_of(gid)
    return h is not None and check_token(request.cookies.get(input_cookie(gid)), f"in:{gid}", h)


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="管理者としてログインしてください")


def require_input(game_id: str, request: Request):
    gid = norm_id(game_id)
    if pw_hash_of(gid) is None:
        raise HTTPException(status_code=404, detail="試合が見つかりません")
    if not is_input(request, gid):
        raise HTTPException(status_code=401, detail="この試合の入力画面にログインしてください")
    return gid


# ===== 画面 =====
def page(name, static_prefix, status_code=200):
    """静的ファイルへの相対パスは、ページの深さに合わせて差し替える（/score の下でも動くように）。"""
    html = (STATIC / name).read_text(encoding="utf-8").replace("{{STATIC}}", static_prefix)
    return HTMLResponse(html, status_code=status_code)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)


@app.middleware("http")
async def no_cache(request, call_next):
    # 画面の更新や得点の変化がすぐ届くよう、ブラウザには毎回サーバーへ確認させる
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    # 安全のためのヘッダ。HSTS は HTTPS で届いたときだけブラウザが従う（サブドメインには広げない）
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    # 他サイトへの埋め込みは、閲覧画面（配信の重ね表示などで使う）以外は禁止する
    if not getattr(request.state, "frame_ok", False):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Login(BaseModel):
    password: str = ""


@app.middleware("http")
async def trailing_slash(request, call_next):
    """末尾の / を外した URL へ、相対パスで転送する（/score の下でも外へ出ない）。
    FastAPI の自動の転送は前に付くパス（/score）を落とした絶対 URL にしてしまうので使わない。"""
    path = request.url.path
    if path != "/" and path.endswith("/") and request.method in ("GET", "HEAD"):
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if name:
            n = len(path) - len(path.rstrip("/"))
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse("../" * n + name + query, status_code=308)
    return await call_next(request)


@app.get("/")
def index_page():
    return page("index.html", "static")


@app.get("/manual")
def manual_page():
    return page("manual.html", "static")


# --- 管理者 ---
@app.get("/admin")
def admin_page(request: Request):
    if not ADMIN_PASSWORD:
        return HTMLResponse("管理者パスワード（ADMIN_PASSWORD）が設定されていません", status_code=503)
    return page("admin.html" if is_admin(request) else "login.html", "static")


@app.post("/admin/login")
def admin_login(body: Login, request: Request):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="管理者パスワードが設定されていません")
    _throttle("admin", request)
    if not hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode()):
        _failed("admin", request)
        raise HTTPException(status_code=401, detail="パスワードが違います")
    _succeeded("admin", request)
    r = JSONResponse({"ok": True})
    _set_cookie(r, ADMIN_COOKIE, make_token("admin", admin_bound()))
    return r


@app.post("/admin/logout")
def admin_logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(ADMIN_COOKIE, path="/")
    return r


class NewGame(BaseModel):
    id: str = ""
    password: str = ""
    title: str = ""


@app.get("/admin/api/games", dependencies=[Depends(require_admin)])
def admin_list():
    return {"games": list_games()}


@app.post("/admin/api/games", dependencies=[Depends(require_admin)])
def admin_create(body: NewGame):
    with _lock:
        try:
            gid = create_game(body.id, body.password, body.title)
        except game.RuleError as e:
            raise HTTPException(status_code=409, detail=str(e))
    return {"id": gid, "games": list_games()}


@app.delete("/admin/api/games/{game_id}", dependencies=[Depends(require_admin)])
def admin_delete(game_id: str):
    with _lock:
        if not delete_game(norm_id(game_id)):
            raise HTTPException(status_code=404, detail="試合が見つかりません")
    return {"games": list_games()}


class CreateCode(BaseModel):
    code: str = ""


@app.get("/admin/api/create-code", dependencies=[Depends(require_admin)])
def admin_code_status():
    return code_status()


@app.put("/admin/api/create-code", dependencies=[Depends(require_admin)])
def admin_set_code(body: CreateCode):
    try:
        set_create_code(body.code)
    except game.RuleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return code_status()


@app.delete("/admin/api/create-code", dependencies=[Depends(require_admin)])
def admin_clear_code():
    set_create_code(None)
    return code_status()


# --- 出題者の入口（既存の試合へ／合言葉で新しい試合を作る） ---
@app.get("/input")
def input_entry_page():
    return page("input_entry.html", "static")


class UserNewGame(NewGame):
    code: str = ""


@app.post("/input")
def input_create(body: UserNewGame, request: Request):
    """合言葉が合えば試合を作り、そのままその試合の入力画面にログインした状態にする。"""
    c = create_code()
    if c is None:
        raise HTTPException(status_code=403, detail="いまは新しい試合を作れません（運営が受け付けていません）")
    _throttle("create", request)
    if not verify_password(body.code, c["hash"]):
        _failed("create", request)
        raise HTTPException(status_code=401, detail="合言葉が違います")
    _succeeded("create", request)
    with _lock:
        try:
            gid = create_game(body.id, body.password, body.title, created_by="input")
        except game.RuleError as e:
            raise HTTPException(status_code=409, detail=str(e))
    r = JSONResponse({"id": gid})
    _set_cookie(r, input_cookie(gid), make_token(f"in:{gid}", pw_hash_of(gid)))
    return r


# --- 入力（試合ごとのパスワード） ---
@app.get("/input/{game_id}")
def input_page(game_id: str, request: Request):
    gid = norm_id(game_id)
    if pw_hash_of(gid) is None:
        return page("notfound.html", "../static", status_code=404)
    return page("input.html" if is_input(request, gid) else "login.html", "../static")


@app.post("/input/{game_id}/login")
def input_login(game_id: str, body: Login, request: Request):
    gid = norm_id(game_id)
    h = pw_hash_of(gid)
    if h is None:
        raise HTTPException(status_code=404, detail="試合が見つかりません")
    key = f"in:{gid}"
    _throttle(key, request)
    if not verify_password(body.password, h):
        _failed(key, request)
        raise HTTPException(status_code=401, detail="パスワードが違います")
    _succeeded(key, request)
    r = JSONResponse({"ok": True})
    _set_cookie(r, input_cookie(gid), make_token(key, h))
    return r


@app.post("/input/{game_id}/logout")
def input_logout(game_id: str):
    r = JSONResponse({"ok": True})
    r.delete_cookie(input_cookie(norm_id(game_id)), path="/")
    return r


class Action(BaseModel):
    type: str
    team: int | None = None
    seat: int | None = None
    names: list[str] | None = None
    emblems: list[str] | None = None
    title: str | None = None
    custom_target: bool | None = None
    targets: list[int] | None = None
    no_question_limit: bool | None = None
    players: list[list[str]] | None = None
    vacant: list[bool] | None = None
    show_question: bool | None = None
    show_log: bool | None = None


@app.get("/input/{game_id}/api/state")
def get_full_state(gid: str = Depends(require_input)):
    return snapshot(gid, full=True)


def touch_input(gid, state):
    """得点の入力（○・×・スルー）の時刻を試合に記録する。"""
    with _conn() as conn:
        conn.execute("UPDATE games SET last_input_at = ? WHERE id = ?", (state["log"][-1]["at"], gid))


def annotate_last(prev, new):
    """判定で記録が1件増えたとき、その記録に判定時刻と判定後の両チームの得点を付ける。"""
    if len(new["log"]) > len(prev["log"]):
        v = game.view(new)
        new["log"][-1]["at"] = now_jst().isoformat()
        new["log"][-1]["scores"] = [t["score"] for t in v["teams"]]
    return new


@app.post("/input/{game_id}/api/action")
def act(a: Action, gid: str = Depends(require_input)):
    with _lock:
        try:
            state, _ = load(gid)
            if a.type in ("correct", "wrong", "through"):
                if a.type == "through":
                    new = game.through(state)
                else:
                    new = getattr(game, a.type)(state, a.team, a.seat)
                new = annotate_last(state, new)
                push(gid, new)
                touch_input(gid, new)
            elif a.type == "emblems":
                push(gid, game.set_emblems(state, a.emblems))
            elif a.type == "rename":
                push(gid, game.rename(state, a.names or [], a.title))
            elif a.type == "targets":
                push(gid, game.set_targets(state, bool(a.custom_target), a.targets))
            elif a.type == "question_limit":
                push(gid, game.set_question_limit(state, a.no_question_limit))
            elif a.type == "roster":
                push(gid, game.set_roster(state, a.team, a.players, a.vacant))
            elif a.type == "display":
                display, _ = load_display(gid)
                for key in DEFAULT_DISPLAY:
                    if getattr(a, key) is not None:
                        display[key] = getattr(a, key)
                save_display(gid, display)
            elif a.type == "undo":
                pop(gid)
            elif a.type == "reset":
                reset(gid, state)
            else:
                raise game.RuleError("不明な操作です")
        except (game.RuleError, TypeError) as e:
            raise HTTPException(status_code=409, detail=str(e))
        return snapshot(gid, full=True)


def export_record(state, exported_at, gid=None):
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
        "game_id": gid,
        "exported_at": exported_at.isoformat(),
        "title": state.get("title", ""),
        "rules": {"max_questions": state["max_questions"], "no_question_limit": bool(state.get("no_question_limit")),
                  "custom_target": bool(state.get("custom_target")), "targets": game.targets(state)},
        "question": state["question"],
        "result": None if res is None else {**res, "winner_name": None if res["winner"] is None else teams[res["winner"]]},
        "teams": [
            {"name": t["name"], "emblem": t["emblem"], "score": t["score"], "product": game.product(raw), "target": t["target"],
             "seats": [{"seat": i + 1, "players": [n for n in s["players"] if n], "score": s["score"],
                        "cross": s["cross"], "locked": s["locked"], "reach": s["reach"], "vacant": s["vacant"]}
                       for i, s in enumerate(t["seats"])]}
            for t, raw in zip(v["teams"], state["teams"])
        ],
        "log": log,
    }


@app.get("/input/{game_id}/api/export")
def export(gid: str = Depends(require_input)):
    state, _ = load(gid)
    now = now_jst()
    name = f"aql_record_{gid}_{now:%Y%m%d_%H%M%S}.json"
    return JSONResponse(export_record(state, now, gid), headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --- 閲覧（認証なし） ---
@app.get("/api/games/{game_id}/state")
def get_state(game_id: str):
    gid = norm_id(game_id)
    if pw_hash_of(gid) is None:
        raise HTTPException(status_code=404, detail="試合が見つかりません")
    return snapshot(gid, full=False)


# ほかのルートと衝突しないよう最後に登録する
@app.get("/{game_id}")
def viewer_page(game_id: str, request: Request):
    gid = norm_id(game_id)
    if pw_hash_of(gid) is None:
        return page("notfound.html", "static", status_code=404)
    request.state.frame_ok = True  # 閲覧画面は他サイトへの埋め込みを許す
    return page("viewer.html", "static")
