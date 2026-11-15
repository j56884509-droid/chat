import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
import db
from durak import Durak

app = FastAPI()
import os, uuid
from fastapi.staticfiles import StaticFiles

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

MAX_FILE = 25 * 1024 * 1024
FILE_TYPES = {
    "image/jpeg": ("jpg", "img"), "image/png": ("png", "img"),
    "image/gif": ("gif", "img"), "image/webp": ("webp", "img"),
    "video/mp4": ("mp4", "vid"), "video/webm": ("webm", "vid"),
    "video/quicktime": ("mov", "vid"),
}

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
rooms = {}
games = {}
lobbies = {}

async def broadcast(room, data):
    for ws, _ in rooms.get(room, []):
        await ws.send_text(json.dumps(data))

async def say(room, text):
    await broadcast(room, {"type": "system", "text": text})

async def tell(room, name, text):
    for ws, n in rooms.get(room, []):
        if n == name:
            await ws.send_text(json.dumps({"type": "err", "text": text}))

async def send_game(room):
    g = games.get(room)
    if not g:
        return
    for ws, name in rooms.get(room, []):
        if name in g.players:
            await ws.send_text(json.dumps(g.state(name)))

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
import re
import time

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

def guess_lang(text):
    if re.search("[а-яА-ЯёЁ]", text):
        return "ru"
    words = re.findall("[a-z'’ʻʼ‘]+", text.lower())
    if any(w in EN_WORDS for w in words):
        return "en"
    return "uz"

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
    try:
        result = MyMemoryTranslator(source=LANG_NAMES[src], target=LANG_NAMES[to]).translate(text) or text
    except Exception:
        return {"text": text}
    TR_CACHE[key] = result
    return {"text": result}

def guess_lang(text):
    if re.search("[а-яА-ЯёЁ]", text):
        return "ru"
    words = re.findall("[a-z'’ʻʼ‘]+", text.lower())
    if any(w in EN_WORDS for w in words):
        return "en"
    return "uz"

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
    try:
        result = MyMemoryTranslator(source=LANG_NAMES[src], target=LANG_NAMES[to]).translate(text) or text
    except Exception:
        return {"text": text}
    TR_CACHE[key] = result
    return {"text": result}
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
    token = db.register(name, pw)
    if not token:
        return JSONResponse({"error": "Bu ism band"}, status_code=400)
    return {"token": token, "name": name}

@app.post("/login")
async def do_login(req: Request):
    d = await req.json()
    name = d.get("name", "").strip()
    token = db.login(name, d.get("password", ""))
    if not token:
        return JSONResponse({"error": "Ism yoki parol noto'g'ri"}, status_code=400)
    return {"token": token, "name": name}

@app.get("/profile/{name}")
async def get_profile(name: str):
    p = db.get_profile(name)
    if not p:
        return JSONResponse({"error": "Topilmadi"}, status_code=404)
    p["online"] = any(n == name for lst in rooms.values() for _, n in lst)
    return p

@app.post("/profile")
async def save_profile(req: Request):
    d = await req.json()
    name = db.name_by_token(d.get("token", ""))
    if not name:
        return JSONResponse({"error": "Kirish kerak"}, status_code=401)
    avatar = d.get("avatar", "🙂")
    if not re.fullmatch(r"/uploads/[\w\-]+\.(jpg|png|gif|webp)", avatar):
        avatar = avatar[:8]
    db.set_profile(name, avatar, d.get("bio", ""))
    return {"ok": True}
@app.get("/users")
def find_users(q: str = "", token: str = ""):
    me = db.name_by_token(token)
    q = q.strip()
    if not me or not q:
        return []
    rows = db.conn.execute(
        "SELECT name, avatar FROM users WHERE name LIKE ? AND name != ? LIMIT 20",
        ("%" + q + "%", me)).fetchall()
    return [{"name": r[0], "avatar": r[1]} for r in rows]

@app.get("/dms")
def my_dms(token: str = ""):
    me = db.name_by_token(token)
    if not me:
        return []
    rows = db.conn.execute("SELECT DISTINCT room FROM messages WHERE room LIKE 'dm:%'").fetchall()
    out = []
    for (room,) in rows:
        pair = room[3:].split("|")
        if len(pair) == 2 and me in pair:
            other = pair[1] if pair[0] == me else pair[0]
            p = db.get_profile(other)
            if p:
                out.append({"name": other, "avatar": p["avatar"]})
    return out

@app.websocket("/ws/{room}/{token}")
async def chat(ws: WebSocket, room: str, token: str):
    name = db.name_by_token(token)
    await ws.accept()
    if not name:
        await ws.close()
        return
    if room.startswith("dm:"):
        pair = room[3:].split("|")
        if len(pair) != 2 or name not in pair:
            await ws.close()
            return
    rooms.setdefault(room, []).append((ws, name))
    for m in db.last_messages(room):
        await ws.send_text(json.dumps(m))
    await broadcast(room, {"type": "system", "text": name + " qo'shildi"})
    await send_lobby(room)
    await send_game(room)
    try:
        while True:
            data = json.loads(await ws.receive_text())
            if data["type"] == "msg":
                text = data["text"].strip()[:1000]
                if not text:
                    continue
                db.save_message(room, name, text)
                avatar = db.get_profile(name)["avatar"]
                await broadcast(room, {"type": "msg", "name": name, "text": text, "avatar": avatar})
            elif data["type"] == "game":
                await handle_game(room, name, data)
    except WebSocketDisconnect:
        rooms[room].remove((ws, name))
        await broadcast(room, {"type": "system", "text": name + " chiqib ketdi"})
        if not any(n == name for _, n in rooms[room]):
            await player_left(room, name)