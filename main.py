import json
import os
import re
import time
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db
from durak import Durak

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
}
MEDIA_RE = re.compile(r"^\[(img|vid|aud)\]/uploads/[\w.\-]+$")


@app.post("/upload")
async def upload(req: Request, token: str = ""):
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
    fname = uuid.uuid4().hex + "." + ext
    with open(os.path.join("uploads", fname), "wb") as f:
        f.write(body)
    return {"url": "/uploads/" + fname, "kind": kind}


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


async def send_game(room):
    g = games.get(room)
    if not g:
        return
    for ws, name in list(rooms.get(room, [])):
        if name in g.players:
            await _send(ws, g.state(name))


async def send_lobby(room):
    l = lobbies.get(room)
    if not l:
        await broadcast(room, {"type": "lobby", "gone": True})
    else:
        await broadcast(room, {"type": "lobby", "host": l["host"], "players": l["players"]})


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
            l = {"host": name, "players": []}
            lobbies[room] = l
            await say(room, name + " durak o'yiniga odam yig'moqda")
        if name in l["players"]:
            return
        if len(l["players"]) >= 4:
            await tell(room, name, "O'yinda 4 kishi to'lgan")
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
        if len(l["players"]) < 2:
            await tell(room, name, "Durak uchun kamida 2 kishi kerak")
            return
        players = l["players"]
        del lobbies[room]
        games[room] = Durak(players)
        await send_lobby(room)
        await say(room, "Durak boshlandi: " + ", ".join(players))
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
                return result
        except Exception as e:
            print("xato:", name, str(e)[:80])
            COOLDOWN[name] = time.time() + 600
    return None


@app.get("/translate")
def translate(text: str, to: str = "ru"):
    if to not in LANG_NAMES or len(text) > 450:
        return {"text": text}
    src = guess_lang(text)
    if src == to:
        return {"text": text}
    key = (text, to)
    if key in TR_CACHE:
        return {"text": TR_CACHE[key]}
    result = run_engines(text, src, to)
    if not result:
        return {"text": text}
    TR_CACHE[key] = result
    return {"text": result}


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
    token = None if name.lower() == "me" else db.register(name, pw, _ua(req))
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
    online = any(n == name for lst in rooms.values() for _, n in lst) or bool(usocks.get(name))
    other = viewer != name
    cut = other and viewer is not None and db.is_blocked(name, viewer)
    hidden = other and (bool(p["hide_seen"]) or cut)
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
    rows = db.conn.execute(
        "SELECT name, avatar, display, hide_photo FROM users WHERE (name LIKE ? OR display LIKE ?) AND name != ? LIMIT 20",
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
    for (room,) in db.conn.execute("SELECT DISTINCT room FROM messages WHERE room LIKE 'dm:%'").fetchall():
        pair = room[3:].split("|")
        if len(pair) == 2 and me in pair:
            other = pair[1] if pair[0] == me else pair[0]
            p = db.get_profile(other)
            if p:
                dms.append({"name": other, "display": p["display"],
                            "avatar": "🙂" if other != me and p["hide_photo"] else p["avatar"], "last": db.room_last(room),
                            "unread": db.unread(room, me)})
    groups = {}
    for r in wanted:
        if r.startswith("dm:"):
            continue
        groups[r] = {"last": db.room_last(r), "unread": db.unread(r, me)}
    return {"dms": dms, "rooms": groups}


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
    if is_dm:
        pair = room[3:].split("|")
        if len(pair) != 2 or name not in pair:
            await ws.close()
            return
    rooms.setdefault(room, []).append((ws, name))
    for m in db.last_messages(room):
        await ws.send_text(json.dumps(m))
    await ws.send_text(json.dumps({"type": "reads", "reads": db.get_reads(room)}))
    if not is_dm:
        await broadcast(room, {"type": "system", "text": name + " qo'shildi"})
    await send_lobby(room)
    await send_game(room)
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
                    rt = data.get("reply_to")
                    if isinstance(rt, int):
                        src = db.get_message(rt)
                        if not src or src["room"] != room:
                            rt = None
                    else:
                        rt = None
                    if is_dm:
                        other = pair[1] if pair[0] == name else pair[0]
                        if other != name:
                            op = db.get_profile(other)
                            if (db.is_blocked(other, name) or db.is_blocked(name, other)
                                    or (op and op["msg_priv"] == 1 and not db.prior_chat(room, other))):
                                await tell(room, name, "Xabar yuborib bo'lmadi")
                                continue
                    mid, now = db.save_message(room, name, text, rt)
                    prof = db.get_profile(name)
                    await broadcast(room, {"type": "msg", "id": mid, "name": name, "display": prof["display"],
                                           "color": prof["color"], "text": text,
                                           "avatar": "🙂" if prof["hide_photo"] else prof["avatar"], "time": now,
                                           "edited": 0, "deleted": 0, "reply": db.reply_info(rt)})
                    if is_dm and other != name:
                        for w in list(usocks.get(other, [])):
                            await _send(w, {"type": "notify", "room": room, "id": mid, "name": name,
                                            "display": prof["display"], "text": text, "time": now,
                                            "avatar": "🙂" if prof["hide_photo"] else prof["avatar"]})
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
                    if isinstance(mid, int) and db.delete_message(mid, room, name):
                        await broadcast(room, {"type": "del", "id": mid})
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
        if not is_dm:
            await broadcast(room, {"type": "system", "text": name + " chiqib ketdi"})
        if not any(n == name for _, n in rooms.get(room, [])):
            await player_left(room, name)
        if not rooms.get(room):
            rooms.pop(room, None)
