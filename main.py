import json
import os
import re
import time
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import ai
import asyncio
import db
from durak import Durak
from games import XO, RPS

app = FastAPI()

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

MAX_FILE = 25 * 1024 * 1024
FILE_TYPES = {
    "image/jpeg": ("jpg", "img"), "image/png": ("png", "img"),
    "image/gif": ("gif", "img"), "image/webp": ("webp", "img"),
    "video/mp4": ("mp4", "vid"), "video/webm": ("webm", "vid"),
    "video/quicktime": ("mov", "vid"),
    "audio/webm": ("webm", "aud"), "audio/mp4": ("m4a", "aud"),
    "audio/ogg": ("ogg", "aud"), "audio/mpeg": ("mp3", "aud"),
    "application/pdf": ("pdf", "doc"), "text/plain": ("txt", "doc"), "text/csv": ("csv", "doc"),
    "application/zip": ("zip", "doc"), "application/x-zip-compressed": ("zip", "doc"),
    "application/msword": ("doc", "doc"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("docx", "doc"),
    "application/vnd.ms-excel": ("xls", "doc"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("xlsx", "doc"),
    "application/vnd.ms-powerpoint": ("ppt", "doc"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ("pptx", "doc"),
}
MEDIA_RE = re.compile(r"^\[(img|vid|aud)\]/uploads/[\w.\-]+$|^\[doc\]/uploads/[\w.\-]+\|[^|]{1,120}\|\d{1,10}$")
REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "👎"]


@app.post("/upload")
async def upload(req: Request, token: str = "", fname: str = ""):
    if not db.name_by_token(token):
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    ctype = req.headers.get("content-type", "").split(";")[0].strip()
    if ctype not in FILE_TYPES:
        return JSONResponse({"error": "Bu fayl turi mumkin emas"}, status_code=400)
    if int(req.headers.get("content-length", "0") or 0) > MAX_FILE:
        return JSONResponse({"error": "Fayl katta"}, status_code=400)
    body = await req.body()
    if not body or len(body) > MAX_FILE:
        return JSONResponse({"error": "Fayl katta"}, status_code=400)
    ext, kind = FILE_TYPES[ctype]
    stored = uuid.uuid4().hex + "." + ext
    with open(os.path.join("uploads", stored), "wb") as f:
        f.write(body)
    shown = re.sub(r"[|\\/\r\n]", "_", os.path.basename(fname or ""))[:120] or ("fayl." + ext)
    return {"url": "/uploads/" + stored, "kind": kind, "name": shown, "size": len(body)}


# ---------- xonalar, o'yin ----------

rooms = {}
games = {}
lobbies = {}
usocks = {}  # name -> [WebSocket]: har bir kirgan odamning umumiy kanali (bildirishnomalar uchun)


async def _send(ws, data):
    try:
        await ws.send_text(json.dumps(data))
    except Exception:
        pass


async def broadcast(room, data):
    for ws, _ in list(rooms.get(room, [])):
        await _send(ws, data)


async def say(room, text):
    await broadcast(room, {"type": "system", "text": text})


async def tell(room, name, text):
    for ws, n in list(rooms.get(room, [])):
        if n == name:
            await _send(ws, {"type": "err", "text": text})


def can_access(room, name):
    if room.startswith("dm:"):
        pair = room[3:].split("|")
        return len(pair) == 2 and name in pair
    gid = db.room_gid(room)
    if gid is not None:
        return db.member_role(gid, name) is not None
    return True


def can_pin(room, name):
    gid = db.room_gid(room)
    if gid is None:
        return True
    return db.member_role(gid, name) in ("owner", "admin")


def is_room_admin(room, name):
    gid = db.room_gid(room)
    return gid is not None and db.member_role(gid, name) in ("owner", "admin")


async def ping_chats(names):
    """Ro'yxatlarni yangilash uchun onlayn odamlarga signal."""
    for n in set(names):
        for w in list(usocks.get(n, [])):
            await _send(w, {"type": "chats"})


async def deliver(room, name, text, reply_to=None, fwd=None):
    """Xabarni tekshiradi, saqlaydi va hammaga yuboradi. Xato bo'lsa matn qaytaradi, aks holda None."""
    other = None
    gid = db.room_gid(room)
    if room.startswith("dm:"):
        pair = room[3:].split("|")
        if len(pair) != 2 or name not in pair:
            return "Xabar yuborib bo'lmadi"
        other = pair[1] if pair[0] == name else pair[0]
        if other != name:
            op = db.get_profile(other)
            if (db.is_blocked(other, name) or db.is_blocked(name, other)
                    or (op and op["msg_priv"] == 1 and not db.prior_chat(room, other))):
                return "Xabar yuborib bo'lmadi"
    elif gid is not None:
        if db.member_role(gid, name) is None:
            return "Xabar yuborib bo'lmadi"
    if isinstance(reply_to, int):
        src = db.get_message(reply_to)
        if not src or src["room"] != room:
            reply_to = None
    else:
        reply_to = None
    mid, now = db.save_message(room, name, text, reply_to, fwd)
    payload = db.message_payload(mid)
    await broadcast(room, payload)
    note = {"type": "notify", "room": room, "id": mid, "name": name, "display": payload["display"],
            "text": text, "time": now, "avatar": payload["avatar"], "title": ""}
    targets = []
    if other is not None and other != name:
        targets = [other]
    elif gid is not None:
        g = db.get_group(gid)
        note["title"] = g["title"] if g else ""
        targets = [n for n in db.member_names(gid) if n != name]
    for n in targets:
        for w in list(usocks.get(n, [])):
            await _send(w, note)
    return None


BOT = db.BOT_NAME
GREETING = ("Salom! Men «AI do'st»man 🤖 Savol bering, gaplashamiz, maslahat so'rang. "
            "Eslatma: xabarlaringiz AI xizmatiga (Google, Groq va boshqalar) yuboriladi, "
            "shuning uchun parol yoki shaxsiy ma'lumotlarni yozmang.")
AI_HITS = {}
AI_LOCKS = {}


def ai_allowed(name):
    """Bepul limitlarni asrash uchun: daqiqada 10 va kuniga 150 ta so'rov."""
    now = time.time()
    hits = [t for t in AI_HITS.get(name, []) if now - t < 86400]
    if sum(1 for t in hits if now - t < 60) >= 10:
        return "min"
    if len(hits) >= 150:
        return "day"
    hits.append(now)
    AI_HITS[name] = hits
    return ""


async def bot_reply(room, user, text):
    lock = AI_LOCKS.setdefault(room, asyncio.Lock())
    async with lock:
        done = asyncio.Event()

        async def typing():
            while not done.is_set():
                await broadcast(room, {"type": "typing", "name": BOT, "display": db.display_of(BOT)})
                try:
                    await asyncio.wait_for(done.wait(), 2.5)
                except asyncio.TimeoutError:
                    pass

        tt = asyncio.create_task(typing())
        reply = ""
        try:
            if MEDIA_RE.match(text):
                reply = "Hozircha faqat matn xabarlarini tushunaman 🙂"
            else:
                why = ai_allowed(user)
                if why == "min":
                    reply = "Juda tez yozyapsiz, bir daqiqa kuting 🙂"
                elif why == "day":
                    reply = "Bugungi limit tugadi, ertaga davom etamiz 🙂"
                else:
                    history = []
                    for n, t in db.recent_texts(room, 14):
                        if n == BOT and (t or "").startswith("⚠"):
                            continue
                        history.append(("assistant" if n == BOT else "user",
                                        "[fayl]" if MEDIA_RE.match(t or "") else t))
                    try:
                        reply, _ = await asyncio.get_running_loop().run_in_executor(None, ai.ask, history)
                    except ai.AIError as e:
                        reply = "⚠ " + e.user_message
        except Exception as e:
            print("AI xato:", repr(e)[:120])
            reply = "⚠ AI'da kutilmagan xato chiqdi."
        finally:
            done.set()
            await tt
        for part in ai.split_text(reply):
            await deliver(room, BOT, part)


kinds = {}
GAME_DEFS = {
    "durak": {"cls": Durak, "title": "Durak", "min": 2, "max": 4},
    "xo": {"cls": XO, "title": "XO", "min": 2, "max": 2},
    "rps": {"cls": RPS, "title": "Tosh-qaychi-qog'oz", "min": 2, "max": 2},
}


async def send_game(room):
    g = games.get(room)
    if not g:
        return
    for ws, name in list(rooms.get(room, [])):
        if name in g.players:
            st = g.state(name)
            st["kind"] = kinds.get(room, "durak")
            await _send(ws, st)


async def send_lobby(room):
    l = lobbies.get(room)
    if not l:
        await broadcast(room, {"type": "lobby", "gone": True})
    else:
        d = GAME_DEFS[l["game"]]
        await broadcast(room, {"type": "lobby", "host": l["host"], "players": l["players"],
                               "game": l["game"], "min": d["min"], "max": d["max"]})


async def player_left(room, name):
    l = lobbies.get(room)
    if l and name in l["players"]:
        l["players"].remove(name)
        if l["players"]:
            l["host"] = l["players"][0]
        else:
            del lobbies[room]
        await send_lobby(room)
    g = games.get(room)
    if g and name in g.players:
        del games[room]
        await broadcast(room, {"type": "game", "gone": True})
        if not g.over:
            await say(room, "O'yin to'xtadi")


async def handle_game(room, name, data):
    action = data.get("action")
    g = games.get(room)
    l = lobbies.get(room)
    if action == "join":
        if g and not g.over:
            await tell(room, name, "O'yin allaqachon boshlangan")
            return
        if g and g.over:
            del games[room]
            await broadcast(room, {"type": "game", "gone": True})
        if not l:
            kind = data.get("game")
            if kind not in GAME_DEFS:
                kind = kinds.get(room, "durak")
            l = {"host": name, "players": [], "game": kind}
            lobbies[room] = l
            kinds[room] = kind
            title = "durak" if kind == "durak" else GAME_DEFS[kind]["title"]
            await say(room, name + " " + title + " o'yiniga odam yig'moqda")
        if name in l["players"]:
            return
        d = GAME_DEFS[l["game"]]
        if len(l["players"]) >= d["max"]:
            await tell(room, name, "O'yinda %d kishi to'lgan" % d["max"])
            return
        l["players"].append(name)
        await send_lobby(room)
    elif action == "leave":
        if l and name in l["players"]:
            l["players"].remove(name)
            if l["players"]:
                l["host"] = l["players"][0]
            else:
                del lobbies[room]
            await send_lobby(room)
    elif action == "start":
        if not l or name != l["host"]:
            await tell(room, name, "Faqat boshlovchi o'yinni boshlay oladi")
            return
        d = GAME_DEFS[l["game"]]
        if len(l["players"]) < d["min"]:
            await tell(room, name, "%s uchun kamida %d kishi kerak" % (d["title"], d["min"]))
            return
        players = l["players"]
        del lobbies[room]
        games[room] = d["cls"](players)
        kinds[room] = l["game"]
        await send_lobby(room)
        await say(room, d["title"] + " boshlandi: " + ", ".join(players))
        await send_game(room)
    else:
        if not g or name not in g.players:
            return
        err = g.act(name, action, data.get("card"))
        if err:
            await tell(room, name, err)
            return
        await send_game(room)
        if g.over:
            await say(room, g.msg)


# ---------- tarjima ----------

TR_CACHE = {}
LANG_NAMES = {"uz": "uzbek", "ru": "russian", "en": "english"}


def tr_google(text, src, to):
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source=src, target=to).translate(text)


def tr_bing(text, src, to):
    import translators as ts
    return ts.translate_text(text, translator="bing", from_language=src, to_language=to)


def tr_yandex(text, src, to):
    import translators as ts
    return ts.translate_text(text, translator="yandex", from_language=src, to_language=to)


def tr_mymemory(text, src, to):
    from deep_translator import MyMemoryTranslator
    return MyMemoryTranslator(source=LANG_NAMES[src], target=LANG_NAMES[to]).translate(text)


ENGINES = [("google", tr_google), ("bing", tr_bing), ("yandex", tr_yandex), ("mymemory", tr_mymemory)]
COOLDOWN = {}
EN_WORDS = {"the", "is", "are", "you", "i", "and", "to", "a", "of", "in", "it",
            "what", "how", "hello", "hi", "hey", "thanks", "thank", "please",
            "yes", "ok", "good", "this", "that", "my", "we", "can", "not",
            "with", "for", "have", "your", "where", "when", "why", "who"}


def guess_lang(text):
    if re.search("[а-яА-ЯёЁ]", text):
        return "ru"
    words = re.findall("[a-z'’ʻʼ‘]+", text.lower())
    if any(w in EN_WORDS for w in words):
        return "en"
    return "uz"


def run_engines(text, src, to):
    for name, fn in ENGINES:
        if COOLDOWN.get(name, 0) > time.time():
            continue
        try:
            result = fn(text, src, to)
            if result:
                print("tarjima:", name)
                return result, name
        except Exception as e:
            print("xato:", name, str(e)[:80])
            COOLDOWN[name] = time.time() + 600
    return None, ""


@app.get("/translate")
def translate(text: str, to: str = "ru", src: str = ""):
    """ok=False bo'lsa, hech bir tarjimon javob bermadi (sahifa ogohlantirish ko'rsatadi)."""
    if to not in LANG_NAMES or len(text) > 450:
        return {"text": text, "ok": True, "same": True}
    if src not in LANG_NAMES:
        src = guess_lang(text)
    if src == to:
        return {"text": text, "ok": True, "same": True}
    key = (text, to)
    if key in TR_CACHE:
        return {"text": TR_CACHE[key], "ok": True}
    result, engine = run_engines(text, src, to)
    if not result:
        return {"text": text, "ok": False}
    TR_CACHE[key] = result
    if len(TR_CACHE) > 5000:
        TR_CACHE.clear()
    return {"text": result, "ok": True, "engine": engine}


@app.get("/translate/status")
def translate_status():
    now = time.time()
    return [{"name": n, "ok": COOLDOWN.get(n, 0) <= now, "wait": max(0, int(COOLDOWN.get(n, 0) - now))}
            for n, _ in ENGINES]


# ---------- sahifa, kirish, profil ----------

def _ua(req):
    return (req.headers.get("user-agent") or "")[:200]


@app.get("/")
async def home():
    return HTMLResponse(open("index.html", encoding="utf-8").read())


@app.post("/register")
async def do_register(req: Request):
    d = await req.json()
    name = d.get("name", "").strip()
    pw = d.get("password", "")
    if not (2 <= len(name) <= 20) or len(pw) < 4:
        return JSONResponse({"error": "Ism 2-20 harf, parol kamida 4 belgi bo'lsin"}, status_code=400)
    token = None if name.lower() in ("me", db.BOT_NAME) else db.register(name, pw, _ua(req))
    if not token:
        return JSONResponse({"error": "Bu ism band"}, status_code=400)
    return {"token": token, "name": name}


@app.post("/login")
async def do_login(req: Request):
    d = await req.json()
    name = d.get("name", "").strip()
    token = db.login(name, d.get("password", ""), _ua(req))
    if not token:
        return JSONResponse({"error": "Ism yoki parol noto'g'ri"}, status_code=400)
    return {"token": token, "name": name}


@app.get("/profile/{name}")
async def get_profile(name: str, token: str = ""):
    viewer = db.name_by_token(token)
    if name == "me" and viewer:
        name = viewer
    p = db.get_profile(name)
    if not p:
        return JSONResponse({"error": "Topilmadi"}, status_code=404)
    online = any(n == name for lst in rooms.values() for _, n in lst) or bool(usocks.get(name)) or bool(p.get("bot"))
    other = viewer != name
    cut = other and viewer is not None and db.is_blocked(name, viewer)
    hidden = other and not p.get("bot") and (bool(p["hide_seen"]) or cut)
    out = {"name": name, "display": p["display"], "color": p["color"],
           "avatar": "🙂" if other and (p["hide_photo"] or cut) else p["avatar"],
           "bio": "" if other and (p["hide_bio"] or cut) else p["bio"],
           "online": False if hidden else online,
           "last_seen": 0 if hidden else p["last_seen"], "hidden": hidden}
    if viewer:
        out["blocked"] = db.is_blocked(viewer, name)
        if viewer == name:
            for k in ("hide_seen", "hide_photo", "hide_bio", "msg_priv", "sess_ttl"):
                out[k] = p[k]
            out["blocked_n"] = len(db.list_blocked(viewer))
            out["sess_n"] = len(db.list_sessions(viewer))
    return out


@app.post("/profile")
async def save_profile(req: Request):
    d = await req.json()
    name = db.name_by_token(d.get("token", ""))
    if not name:
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    avatar = d.get("avatar", "🙂")
    if not re.fullmatch(r"/uploads/[\w\-]+\.(jpg|png|gif|webp)", avatar):
        avatar = avatar[:8]
    display = d.get("display")
    if isinstance(display, str):
        display = display.strip()[:30] or name
    else:
        display = None
    hide = d.get("hide_seen")
    hide = bool(hide) if hide is not None else None
    color = d.get("color")
    if isinstance(color, str):
        color = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else ""
    else:
        color = None
    db.set_profile(name, avatar, str(d.get("bio", "")), display, hide, color)
    return {"ok": True}


@app.post("/settings")
async def save_settings(req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    if not me:
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    db.set_privacy(me, d)
    return {"ok": True}


@app.get("/sessions")
def sessions(token: str = ""):
    me = db.name_by_token(token)
    if not me:
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    return [{"id": x["id"], "ua": x["ua"], "created": x["created"], "last_active": x["last_active"],
             "current": x["token"] == token} for x in db.list_sessions(me)]


@app.post("/sessions/end")
async def end_sessions(req: Request):
    d = await req.json()
    tok = d.get("token", "")
    me = db.name_by_token(tok)
    if not me:
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    if d.get("all_others"):
        db.end_other_sessions(me, tok)
    elif isinstance(d.get("id"), int):
        db.end_session(me, d["id"], tok)
    return {"ok": True}


@app.post("/block")
async def do_block(req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    other = d.get("name", "")
    if not me or not db.get_profile(other) or other == me:
        return JSONResponse({"error": "Xato"}, status_code=400)
    db.set_block(me, other, bool(d.get("on")))
    return {"ok": True}


@app.get("/blocked")
def blocked_list(token: str = ""):
    me = db.name_by_token(token)
    if not me:
        return []
    return db.list_blocked(me)


@app.post("/report")
async def do_report(req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    mid = d.get("id")
    if not me or not isinstance(mid, int) or not db.add_report(me, mid):
        return JSONResponse({"error": "Xato"}, status_code=400)
    return {"ok": True}


@app.get("/users")
def find_users(q: str = "", token: str = ""):
    me = db.name_by_token(token)
    q = q.strip()
    if not me or not q:
        return []
    hide_bot = "" if ai.enabled() else " AND bot=0"
    rows = db.conn.execute(
        "SELECT name, avatar, display, hide_photo FROM users WHERE (name LIKE ? OR display LIKE ?) AND name != ?"
        + hide_bot + " LIMIT 20",
        ("%" + q + "%", "%" + q + "%", me)).fetchall()
    return [{"name": r[0], "avatar": "🙂" if r[3] else r[1], "display": r[2] or r[0]} for r in rows]


@app.get("/chats")
def chats(token: str = "", rooms: str = ""):
    """Suhbatlar ro'yxati: oxirgi xabar, vaqt, o'qilmaganlar soni."""
    me = db.name_by_token(token)
    if not me:
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    try:
        wanted = [r for r in json.loads(rooms or "[]") if isinstance(r, str)][:100]
    except Exception:
        wanted = []
    dms = []
    ai_on = ai.enabled()
    for (room,) in db.conn.execute("SELECT DISTINCT room FROM messages WHERE room LIKE 'dm:%'").fetchall():
        pair = room[3:].split("|")
        if len(pair) == 2 and me in pair:
            other = pair[1] if pair[0] == me else pair[0]
            if other == db.BOT_NAME and not ai_on:
                continue
            p = db.get_profile(other)
            if p:
                dms.append({"name": other, "display": p["display"],
                            "avatar": "🙂" if other != me and p["hide_photo"] else p["avatar"], "last": db.room_last(room),
                            "unread": db.unread(room, me)})
    groups = {}
    for r in wanted:
        if r.startswith("dm:"):
            continue
        if r.startswith("g:"):
            continue
        groups[r] = {"last": db.room_last(r), "unread": db.unread(r, me)}
    grps = []
    for g in db.user_groups(me):
        room = db.group_room(g["id"])
        grps.append({"gid": g["id"], "room": room, "title": g["title"], "avatar": g["avatar"], "role": g["role"],
                     "n": g["n"], "last": db.room_last(room), "unread": db.unread(room, me)})
    return {"dms": dms, "rooms": groups, "groups": grps, "ai": ai_on}


# ---------- yo'naltirish va guruhlar ----------

def _avatar_value(v):
    v = str(v or "")
    if re.fullmatch(r"/uploads/[\w\-]+\.(jpg|png|gif|webp)", v):
        return v
    return v[:8] or "👥"


@app.post("/forward")
async def forward(req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    if not me:
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    mid = d.get("id")
    src = db.get_message(mid) if isinstance(mid, int) else None
    if not src or src["deleted"] or not can_access(src["room"], me):
        return JSONResponse({"error": "Xato"}, status_code=400)
    targets = [r for r in (d.get("rooms") or []) if isinstance(r, str)][:10]
    fwd = src["fwd"] or src["name"]
    failed = 0
    for r in targets:
        if not can_access(r, me) or await deliver(r, me, src["text"], None, fwd):
            failed += 1
    return {"ok": True, "failed": failed}


@app.post("/group")
async def group_create(req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    title = str(d.get("title", "")).strip()[:40]
    if not me or not title:
        return JSONResponse({"error": "Xato"}, status_code=400)
    gid = db.create_group(title, me)
    return {"gid": gid, "room": db.group_room(gid)}


@app.post("/group/join")
async def group_join(req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    gid = db.group_by_invite(str(d.get("code", "")))
    if not me or gid is None:
        return JSONResponse({"error": "Havola yaroqsiz"}, status_code=400)
    if db.member_role(gid, me) is None:
        db.add_member(gid, me)
        room = db.group_room(gid)
        await say(room, db.display_of(me) + " guruhga qo'shildi")
        await ping_chats(db.member_names(gid))
    g = db.get_group(gid)
    return {"gid": gid, "room": db.group_room(gid), "title": g["title"]}


@app.get("/group/{gid}")
def group_info(gid: int, token: str = ""):
    me = db.name_by_token(token)
    role = db.member_role(gid, me) if me else None
    g = db.get_group(gid)
    if not me or role is None or not g:
        return JSONResponse({"error": "Topilmadi"}, status_code=404)
    online = set(usocks.keys())
    members = []
    for x in db.members_of(gid):
        x["online"] = x["name"] in online
        members.append(x)
    return {"gid": gid, "title": g["title"], "avatar": g["avatar"], "owner": g["owner"], "role": role,
            "invite": g["invite"] if role in ("owner", "admin") else "", "members": members}


@app.post("/group/{gid}/edit")
async def group_edit(gid: int, req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    if not me or db.member_role(gid, me) not in ("owner", "admin"):
        return JSONResponse({"error": "Ruxsat yo'q"}, status_code=403)
    title = d.get("title")
    title = str(title).strip()[:40] if isinstance(title, str) and title.strip() else None
    avatar = _avatar_value(d["avatar"]) if d.get("avatar") else None
    db.edit_group(gid, title, avatar)
    await ping_chats(db.member_names(gid))
    return {"ok": True}


@app.post("/group/{gid}/invite")
async def group_invite(gid: int, req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    if not me or db.member_role(gid, me) not in ("owner", "admin"):
        return JSONResponse({"error": "Ruxsat yo'q"}, status_code=403)
    return {"invite": db.reset_invite(gid)}


@app.post("/group/{gid}/member")
async def group_member(gid: int, req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    my = db.member_role(gid, me) if me else None
    other = str(d.get("name", ""))
    action = d.get("action")
    if my not in ("owner", "admin") or not db.get_profile(other):
        return JSONResponse({"error": "Ruxsat yo'q"}, status_code=403)
    room = db.group_room(gid)
    their = db.member_role(gid, other)
    if action == "add":
        if their is None:
            db.add_member(gid, other)
            await say(room, db.display_of(other) + " guruhga qo'shildi")
    elif action == "remove":
        if their is None or their == "owner" or (their == "admin" and my != "owner") or other == me:
            return JSONResponse({"error": "Ruxsat yo'q"}, status_code=403)
        db.remove_member(gid, other)
        await say(room, db.display_of(other) + " guruhdan chiqarildi")
    elif action in ("admin", "member"):
        if my != "owner" or their in (None, "owner"):
            return JSONResponse({"error": "Ruxsat yo'q"}, status_code=403)
        db.set_member_role(gid, other, action)
    else:
        return JSONResponse({"error": "Xato"}, status_code=400)
    await ping_chats(db.member_names(gid) + [other])
    return {"ok": True}


@app.post("/group/{gid}/leave")
async def group_leave(gid: int, req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    if not me or db.member_role(gid, me) is None:
        return JSONResponse({"error": "Xato"}, status_code=400)
    room = db.group_room(gid)
    before = db.member_names(gid)
    res = db.leave_group(gid, me)
    if res == "left":
        await say(room, db.display_of(me) + " guruhni tark etdi")
    await ping_chats(before)
    return {"ok": True}


@app.get("/ai/status")
def ai_status(token: str = ""):
    if not db.name_by_token(token):
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    return ai.status()


@app.post("/ai/test")
async def ai_test(req: Request):
    d = await req.json()
    me = db.name_by_token(d.get("token", ""))
    if not me:
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    if ai_allowed(me):
        return {"ok": False, "error": "Juda ko'p so'rov, birozdan keyin urinib ko'ring"}
    try:
        text, prov = await asyncio.get_running_loop().run_in_executor(
            None, ai.ask, [("user", "Salom! Bitta qisqa gap bilan javob ber.")])
        return {"ok": True, "provider": prov, "text": text[:200]}
    except ai.AIError as e:
        return {"ok": False, "error": e.user_message}


# ---------- WebSocket ----------

@app.websocket("/wsu/{token}")
async def user_ws(ws: WebSocket, token: str):
    name = db.name_by_token(token)
    await ws.accept()
    if not name:
        await ws.close()
        return
    usocks.setdefault(name, []).append(ws)
    try:
        while True:
            await ws.receive_text()
    except Exception:
        pass
    finally:
        try:
            usocks[name].remove(ws)
        except (KeyError, ValueError):
            pass
        if not usocks.get(name):
            usocks.pop(name, None)
            db.touch_seen(name)


@app.websocket("/ws/{room}/{token}")
async def chat(ws: WebSocket, room: str, token: str):
    name = db.name_by_token(token)
    await ws.accept()
    if not name:
        await ws.close()
        return
    is_dm = room.startswith("dm:")
    gid = db.room_gid(room)
    if not can_access(room, name):
        await ws.close()
        return
    plain = (not is_dm) and gid is None
    rooms.setdefault(room, []).append((ws, name))
    hist = db.last_messages(room)
    for m in hist:
        await ws.send_text(json.dumps(m))
    await ws.send_text(json.dumps({"type": "reads", "reads": db.get_reads(room)}))
    await ws.send_text(json.dumps({"type": "pin", "msg": db.get_pin(room)}))
    if plain:
        await broadcast(room, {"type": "system", "text": name + " qo'shildi"})
    await send_lobby(room)
    await send_game(room)
    if not hist and name != BOT and room == db.dm_room(name, BOT):
        await deliver(room, BOT, GREETING)
    try:
        while True:
            try:
                data = json.loads(await ws.receive_text())
                kind = data.get("type")
            except WebSocketDisconnect:
                raise
            except Exception:
                continue
            try:
                if kind == "msg":
                    text = str(data.get("text", "")).strip()[:1000]
                    if not text:
                        continue
                    err = await deliver(room, name, text, data.get("reply_to"))
                    if err:
                        await tell(room, name, err)
                    elif name != BOT and room == db.dm_room(name, BOT):
                        asyncio.create_task(bot_reply(room, name, text))
                elif kind == "edit":
                    mid = data.get("id")
                    text = str(data.get("text", "")).strip()[:1000]
                    src = db.get_message(mid) if isinstance(mid, int) else None
                    if (text and src and src["room"] == room and src["name"] == name
                            and not src["deleted"] and not MEDIA_RE.match(src["text"] or "")
                            and not MEDIA_RE.match(text)):
                        if db.edit_message(mid, room, name, text):
                            await broadcast(room, {"type": "edit", "id": mid, "text": text})
                elif kind == "del":
                    mid = data.get("id")
                    src = db.get_message(mid) if isinstance(mid, int) else None
                    if (src and src["room"] == room and not src["deleted"]
                            and (src["name"] == name or is_room_admin(room, name))):
                        if db.delete_any(mid):
                            await broadcast(room, {"type": "del", "id": mid})
                            if db.pin_id(room) == mid:
                                db.clear_pin(room)
                                await broadcast(room, {"type": "pin", "msg": None})
                elif kind == "react":
                    mid = data.get("id")
                    emoji = data.get("emoji")
                    src = db.get_message(mid) if isinstance(mid, int) else None
                    if src and src["room"] == room and not src["deleted"] and emoji in REACTIONS:
                        await broadcast(room, {"type": "react", "id": mid,
                                               "reactions": db.toggle_reaction(mid, name, emoji)})
                elif kind == "pin":
                    mid = data.get("id")
                    if can_pin(room, name):
                        if data.get("on", True):
                            src = db.get_message(mid) if isinstance(mid, int) else None
                            if src and src["room"] == room and not src["deleted"]:
                                db.set_pin(room, mid)
                                await broadcast(room, {"type": "pin", "msg": db.message_payload(mid)})
                        else:
                            db.clear_pin(room)
                            await broadcast(room, {"type": "pin", "msg": None})
                elif kind == "typing":
                    for w, n in list(rooms.get(room, [])):
                        if n != name:
                            await _send(w, {"type": "typing", "name": name, "display": db.display_of(name)})
                elif kind == "read":
                    upto = data.get("upto")
                    if isinstance(upto, int) and upto > 0:
                        db.mark_read(room, name, upto)
                        await broadcast(room, {"type": "read", "name": name, "upto": upto})
                elif kind == "game":
                    await handle_game(room, name, data)
            except WebSocketDisconnect:
                raise
            except Exception as e:
                print("ws xato:", repr(e)[:120])
    except WebSocketDisconnect:
        pass
    finally:
        db.touch_seen(name)
        try:
            rooms[room].remove((ws, name))
        except (KeyError, ValueError):
            pass
        if plain:
            await broadcast(room, {"type": "system", "text": name + " chiqib ketdi"})
        if not any(n == name for _, n in rooms.get(room, [])):
            await player_left(room, name)
        if not rooms.get(room):
            rooms.pop(room, None)
