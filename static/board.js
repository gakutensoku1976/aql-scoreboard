// 閲覧・入力で共用する描画処理
const Board = (() => {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  let lastLogLen = null;
  // 席の物理配置に合わせた並び（端末・画面ごとの設定）。swap=チームの左右入れ替え、rev=チームごとに枠を5→1
  let layout = { swap: false, rev: [false, false] };
  let lastRender = null;

  function seatHtml(seat, i, t, controls, showNames) {
    // 空席は枠だけ（名前・得点・×・○×ボタンを出さず、リーチ・封鎖の見た目も付けない）
    if (seat.vacant) return `<div class="slot"><div class="no">${i + 1}</div><div class="seat vacant" data-t="${t}" data-s="${i}"></div></div>`;
    const cls = ["seat", seat.locked ? "locked" : "", seat.reach ? "reach" : ""].join(" ");
    const x = "×".repeat(seat.cross);
    const btns = controls ? controls(t, i, seat) : "";
    const names = showNames
      // 名前欄は常に2行分の高さ。1人だけの枠はその上下中央に出す
      ? `<div class="pnames">${(seat.players || []).filter((n) => n).map((n) => `<span><i>${esc(n)}</i></span>`).join("")}</div>` : "";
    return `<div class="slot"><div class="no">${i + 1}</div><div class="${cls}" data-t="${t}" data-s="${i}">${names}` +
      `<div class="pt">${seat.score}</div><div class="x">${x}</div>${btns}</div></div>`;
  }

  // 並びを変えても枠番・入力先は本来の枠のまま（data-s は本来の番号）
  const seatOrder = (t) => (layout.rev[t] ? [4, 3, 2, 1, 0] : [0, 1, 2, 3, 4]);

  function teamHtml(team, t, res, controls, custom) {
    // オプションルール（勝利点の変更）が ON のときは「現在点/勝利点」で表す
    const unit = custom ? `<small class="target">/${team.target}</small>` : "<small>点</small>";
    const win = res && res.winner === t ? " win" : "";
    // どの枠にも名前が無いチームは名前欄ごと省く
    const showNames = team.seats.some((s) => (s.players || []).some((n) => n));
    return `<section class="team${win}" data-i="${t}"><div class="thead"><div class="tname">${esc(team.name)}</div>` +
      `<div class="tscore">${team.score}${unit}</div></div>` +
      `<div class="seats">${seatOrder(t).map((i) => seatHtml(team.seats[i], i, t, controls, showNames)).join("")}</div></section>`;
  }

  function logHtml(s) {
    const items = s.log.slice().reverse().map((e) => {
      const q = e.q === undefined ? "" : `Q${e.q} `;
      if (e.kind === "through") return `<li>${q}スルー</li>`;
      const name = esc(s.teams[e.team].name);
      const mark = e.kind === "correct" ? '<span class="ok">○</span>' : '<span class="ng">×</span>';
      return `<li>${q}${name} ${e.seat + 1}枠 ${mark}</li>`;
    });
    return items.length ? items.join("") : "<li>まだ記録がありません</li>";
  }

  // 枠の幅に入りきらない名前は、文字の高さを保ったまま横方向に縮めて収める
  function fitNames(root) {
    for (const el of root.querySelectorAll(".pnames i")) {
      el.style.transform = "";
      const avail = el.parentElement.clientWidth, w = el.offsetWidth;
      if (w > avail && avail > 0) el.style.transform = `scaleX(${(avail / w).toFixed(3)})`;
    }
  }
  window.addEventListener("resize", () => { if (lastRender) fitNames(lastRender[0]); });

  function render(root, s, controls) {
    lastRender = [root, s, controls];
    const res = s.result;
    // 並びの設定欄に出すチーム名
    for (const el of document.querySelectorAll(".lname[data-t]")) el.textContent = s.teams[+el.dataset.t].name;
    // 閲覧用APIは、表示OFFの項目（問題数・判定記録）を含まない
    const qc = root.querySelector(".qcount");
    qc.hidden = s.question === undefined;
    // 「40問で終了しない」のときは分母を出さず「第n問」
    if (!qc.hidden) qc.innerHTML = s.no_question_limit ? `第<b>${s.question}</b>問` : `<b>${s.question}</b> / ${s.max_questions}問`;
    const banner = root.querySelector(".banner");
    if (res) {
      banner.textContent = res.winner === null ? `引き分け（${res.reason}）` : `${s.teams[res.winner].name} の勝利（${res.reason}）`;
      banner.classList.add("show");
    } else banner.classList.remove("show");
    root.querySelector(".teams").innerHTML = (layout.swap ? [1, 0] : [0, 1])
      .map((i) => teamHtml(s.teams[i], i, res, controls, s.custom_target)).join("");
    fitNames(root);
    const log = root.querySelector(".log");
    log.hidden = s.log === undefined;
    if (log.hidden) { lastLogLen = null; return; }
    log.querySelector("ol").innerHTML = logHtml(s);
    // 新しい判定が入った枠を一瞬強調する
    const last = s.log[s.log.length - 1];
    if (lastLogLen !== null && s.log.length !== lastLogLen && last && last.kind !== "through") {
      const el = root.querySelector(`.seat[data-t="${last.team}"][data-s="${last.seat}"]`);
      if (el) el.classList.add("flash");
    }
    lastLogLen = s.log.length;
  }

  // 一定間隔で状態を取得し、版が変わったときだけ描画する
  function poll(url, onState, intervalMs = 1500) {
    let version = null;
    const foot = document.querySelector(".foot");
    async function tick() {
      try {
        const r = await fetch(url, { cache: "no-store" });
        const s = await r.json();
        if (s.version !== version) { version = s.version; onState(s); }
        foot.textContent = `更新: ${new Date().toLocaleTimeString("ja-JP")}`;
        foot.classList.remove("stale");
      } catch {
        foot.textContent = "接続できません。再試行しています…";
        foot.classList.add("stale");
      }
      setTimeout(tick, intervalMs);
    }
    tick();
    return { force: (s) => { version = s.version; onState(s); } };
  }

  // 表示モード（ライト／ダーク／SportsManiax）。端末ごとに保存し、未設定ならOSの設定に従う
  const THEME_KEY = "aql-scoreboard-theme";
  function bindTheme(select) {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch {}
    select.value = saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    select.onchange = () => {
      document.documentElement.dataset.theme = select.value;
      try { localStorage.setItem(THEME_KEY, select.value); } catch {}
    };
  }

  // 並びの設定欄（data-layout="swap" / "rev0" / "rev1" のチェック）を key で端末に保存する
  function bindLayout(box, key) {
    try { layout = { ...layout, ...JSON.parse(localStorage.getItem(key) || "{}") }; } catch {}
    const inputs = box.querySelectorAll("input[data-layout]");
    for (const el of inputs) {
      const k = el.dataset.layout;
      el.checked = k === "swap" ? layout.swap : layout.rev[+k.slice(3)];
      el.onchange = () => {
        if (k === "swap") layout.swap = el.checked;
        else layout.rev = layout.rev.map((v, i) => (i === +k.slice(3) ? el.checked : v));
        try { localStorage.setItem(key, JSON.stringify(layout)); } catch {}
        if (lastRender) render(...lastRender);
      };
    }
  }

  // 表示中の得点状況を PNG にしてクリップボードへ（できない環境では保存）。画像の下に撮影日時を入れる。
  // makeNode() は画像にする要素と後片付けの関数 { node, cleanup } を返す
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = (d) => `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  async function renderShot(node, now) {
    const body = getComputedStyle(document.body);
    const muted = getComputedStyle(document.querySelector(".foot")).color;
    const ratio = 2, margin = 16, band = 30;
    const canvas = await htmlToImage.toCanvas(node, {
      backgroundColor: body.backgroundColor, pixelRatio: ratio,
      width: node.offsetWidth + margin * 2, height: node.offsetHeight + margin * 2,
      style: { padding: `${margin}px`, margin: "0", position: "static", left: "auto", top: "auto" },
    });
    const out = document.createElement("canvas");
    out.width = canvas.width;
    out.height = canvas.height + band * ratio;
    const ctx = out.getContext("2d");
    ctx.fillStyle = body.backgroundColor;
    ctx.fillRect(0, 0, out.width, out.height);
    ctx.drawImage(canvas, 0, 0);
    ctx.fillStyle = muted;
    ctx.font = `${13 * ratio}px ${body.fontFamily}`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(`撮影 ${stamp(now)}`, out.width - margin * ratio, canvas.height + (band / 2 - 6) * ratio);
    return new Promise((resolve, reject) => out.toBlob((b) => (b ? resolve(b) : reject(new Error("画像を作れませんでした"))), "image/png"));
  }
  function download(blob, now) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `aql_score_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }
  // 画像にする部分（見出し・勝敗・両チーム）だけを複製して画面外で組み立てる。
  // 複製の中の drop（○×ボタン・メニューなど）は取り除き、その分の空きも残さない
  function shotStage(root, drop) {
    const node = document.createElement("div");
    node.className = "shot-stage";
    node.style.width = `${root.querySelector(".teams").offsetWidth}px`;
    for (const sel of ["header.bar", ".banner", ".teams"]) node.appendChild(root.querySelector(sel).cloneNode(true));
    node.querySelectorAll(drop).forEach((el) => el.remove());
    node.querySelectorAll(".flash").forEach((el) => el.classList.remove("flash"));
    root.appendChild(node);
    return { node, cleanup: () => node.remove() };
  }

  function bindShot(button, msgEl, makeNode) {
    let timer;
    const say = (text, isError) => {
      msgEl.textContent = text;
      msgEl.classList.toggle("error", !!isError);
      clearTimeout(timer);
      timer = setTimeout(() => { msgEl.textContent = ""; }, 5000);
    };
    button.onclick = async () => {
      button.disabled = true;
      say("画像を作っています…");
      const now = new Date();
      const { node, cleanup } = makeNode();
      const blob = renderShot(node, now).finally(cleanup);
      blob.catch(() => {});
      try {
        if (!navigator.clipboard || !window.ClipboardItem) throw new Error("unsupported");
        // Safari ではクリック中に write を呼ぶ必要があるため、画像は Promise のまま渡す
        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
        say(`画像をコピーしました（${stamp(now)}）`);
      } catch {
        try {
          download(await blob, now);
          say("この環境ではコピーできないため、画像を保存しました");
        } catch (err) {
          say(`画像を作れませんでした：${err.message}`, true);
        }
      } finally {
        button.disabled = false;
      }
    };
  }

  return { render, poll, esc, bindTheme, bindLayout, bindShot, shotStage };
})();
