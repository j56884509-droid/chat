import sqlite3
import hashlib
import secrets
import time

conn = sqlite3.connect("chat.db", check_same_thread=False)
conn.execute("CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, salt TEXT, hash TEXT, avatar TEXT, bio TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, name TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, name TEXT, text TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS reads (room TEXT, name TEXT, last_id INTEGER, PRIMARY KEY (room, name))")
conn.execute("CREATE TABLE IF NOT EXISTS blocks (blocker TEXT, blocked TEXT, PRIMARY KEY (blocker, blocked))")
conn.execute("CREATE TABLE IF NOT EXISTS reactions (msg_id INTEGER, name TEXT, emoji TEXT, PRIMARY KEY (msg_id, name))")
conn.execute("CREATE TABLE IF NOT EXISTS pins (room TEXT PRIMARY KEY, msg_id INTEGER)")
conn.execute("CREATE TABLE IF NOT EXISTS chat_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, avatar TEXT, owner TEXT, created INTEGER, invite TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS group_members (gid INTEGER, name TEXT, role TEXT, joined INTEGER, PRIMARY KEY (gid, name))")
conn.execute("CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, reporter TEXT, reported TEXT, room TEXT, msg_id INTEGER, msg_text TEXT, created INTEGER)")


def _add_column(table, col, decl):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(" + table + ")").fetchall()]
    if col not in cols:
        conn.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + decl)


# Eski chat.db ni o'chirmasdan yangi ustunlarni qo'shamiz
_add_column("users", "display", "TEXT")
_add_column("users", "last_seen", "INTEGER DEFAULT 0")
_add_column("users", "hide_seen", "INTEGER DEFAULT 0")
_add_column("users", "hide_photo", "INTEGER DEFAULT 0")
_add_column("users", "hide_bio", "INTEGER DEFAULT 0")
_add_column("users", "msg_priv", "INTEGER DEFAULT 0")
_add_column("users", "sess_ttl", "INTEGER DEFAULT 6")
_add_column("users", "color", "TEXT")
_add_column("sessions", "created", "INTEGER DEFAULT 0")
_add_column("sessions", "last_active", "INTEGER DEFAULT 0")
_add_column("sessions", "ua", "TEXT")
_add_column("messages", "created", "INTEGER DEFAULT 0")
_add_column("messages", "reply_to", "INTEGER")
_add_column("messages", "edited", "INTEGER DEFAULT 0")
_add_column("messages", "deleted", "INTEGER DEFAULT 0")
_add_column("messages", "fwd", "TEXT")
conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_room ON messages(room, id)")
conn.commit()


def make_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()


def new_token(name, ua=""):
    token = secrets.token_hex(16)
    now = int(time.time() * 1000)
    conn.execute("INSERT INTO sessions (token, name, created, last_active, ua) VALUES (?, ?, ?, ?, ?)",
                 (token, name, now, now, ua[:200]))
    conn.commit()
    return token


def register(name, password, ua=""):
    if conn.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        return None
    salt = secrets.token_hex(8)
    conn.execute("INSERT INTO users (name, salt, hash, avatar, bio, display) VALUES (?, ?, ?, ?, ?, ?)",
                 (name, salt, make_hash(password, salt), "🙂", "", name))
    conn.commit()
    return new_token(name, ua)


def login(name, password, ua=""):
    row = conn.execute("SELECT salt, hash FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    if not secrets.compare_digest(make_hash(password, row[0]), row[1]):
        return None
    return new_token(name, ua)


def name_by_token(token):
    row = conn.execute(
        "SELECT s.name, s.last_active, u.sess_ttl FROM sessions s LEFT JOIN users u ON u.name=s.name WHERE s.token=?",
        (token,)).fetchone()
    if not row:
        return None
    now = int(time.time() * 1000)
    ttl = (row[2] or 6) * 30 * 86400000
    if row[1] and now - row[1] > ttl:
        # faol bo'lmagan seans avtomatik tugatiladi
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    if not row[1] or now - row[1] > 60000:
        conn.execute("UPDATE sessions SET last_active=? WHERE token=?", (now, token))
        conn.commit()
    return row[0]


def list_sessions(name):
    rows = conn.execute(
        "SELECT rowid, token, ua, created, last_active FROM sessions WHERE name=? ORDER BY last_active DESC",
        (name,)).fetchall()
    return [{"id": r[0], "token": r[1], "ua": r[2] or "", "created": r[3] or 0, "last_active": r[4] or 0} for r in rows]


def end_session(name, sid, keep_token):
    cur = conn.execute("DELETE FROM sessions WHERE rowid=? AND name=? AND token!=?", (sid, name, keep_token))
    conn.commit()
    return cur.rowcount > 0


def end_other_sessions(name, keep_token):
    conn.execute("DELETE FROM sessions WHERE name=? AND token!=?", (name, keep_token))
    conn.commit()


def set_privacy(name, d):
    allowed = {"hide_seen": (0, 1), "hide_photo": (0, 1), "hide_bio": (0, 1), "msg_priv": (0, 1), "sess_ttl": (1, 3, 6, 12)}
    for k, vals in allowed.items():
        if k in d:
            v = int(d[k]) if isinstance(d[k], (int, bool)) else None
            if v in vals:
                conn.execute("UPDATE users SET " + k + "=? WHERE name=?", (v, name))
    conn.commit()


def prior_chat(room, name):
    return conn.execute("SELECT 1 FROM messages WHERE room=? AND name=? LIMIT 1", (room, name)).fetchone() is not None


def get_profile(name):
    row = conn.execute(
        "SELECT avatar, bio, display, last_seen, hide_seen, hide_photo, hide_bio, msg_priv, sess_ttl, color "
        "FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    return {"name": name, "avatar": row[0], "bio": row[1], "display": row[2] or name,
            "last_seen": row[3] or 0, "hide_seen": row[4] or 0, "hide_photo": row[5] or 0,
            "hide_bio": row[6] or 0, "msg_priv": row[7] or 0, "sess_ttl": row[8] or 6, "color": row[9] or ""}


def display_of(name):
    r = conn.execute("SELECT display FROM users WHERE name=?", (name,)).fetchone()
    return r[0] if r and r[0] else name


def set_profile(name, avatar, bio, display=None, hide_seen=None, color=None):
    conn.execute("UPDATE users SET avatar=?, bio=? WHERE name=?", (avatar, bio[:200], name))
    if color is not None:
        conn.execute("UPDATE users SET color=? WHERE name=?", (color, name))
    if display is not None:
        conn.execute("UPDATE users SET display=? WHERE name=?", (display, name))
    if hide_seen is not None:
        conn.execute("UPDATE users SET hide_seen=? WHERE name=?", (1 if hide_seen else 0, name))
    conn.commit()


def touch_seen(name):
    conn.execute("UPDATE users SET last_seen=? WHERE name=?", (int(time.time() * 1000), name))
    conn.commit()


# ---------- bloklash va shikoyat ----------

def is_blocked(blocker, blocked):
    return conn.execute("SELECT 1 FROM blocks WHERE blocker=? AND blocked=?", (blocker, blocked)).fetchone() is not None


def set_block(blocker, blocked, on):
    if on:
        conn.execute("INSERT OR IGNORE INTO blocks VALUES (?, ?)", (blocker, blocked))
    else:
        conn.execute("DELETE FROM blocks WHERE blocker=? AND blocked=?", (blocker, blocked))
    conn.commit()


def list_blocked(blocker):
    rows = conn.execute(
        "SELECT u.name, u.display, u.avatar FROM blocks b JOIN users u ON u.name=b.blocked WHERE b.blocker=?",
        (blocker,)).fetchall()
    return [{"name": r[0], "display": r[1] or r[0], "avatar": r[2]} for r in rows]


def add_report(reporter, msg_id):
    m = get_message(msg_id)
    if not m or m["room"].startswith("dm:") and reporter not in m["room"][3:].split("|"):
        return False
    conn.execute(
        "INSERT INTO reports (reporter, reported, room, msg_id, msg_text, created) VALUES (?, ?, ?, ?, ?, ?)",
        (reporter, m["name"], m["room"], msg_id, m["text"], int(time.time() * 1000)))
    conn.commit()
    return True


# ---------- xabarlar ----------

def save_message(room, name, text, reply_to=None, fwd=None):
    now = int(time.time() * 1000)
    cur = conn.execute(
        "INSERT INTO messages (room, name, text, created, reply_to, fwd) VALUES (?, ?, ?, ?, ?, ?)",
        (room, name, text, now, reply_to, fwd))
    conn.commit()
    return cur.lastrowid, now


def get_message(mid):
    r = conn.execute(
        "SELECT id, room, name, text, created, reply_to, edited, deleted, fwd FROM messages WHERE id=?",
        (mid,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "room": r[1], "name": r[2], "text": r[3], "time": r[4],
            "reply_to": r[5], "edited": r[6], "deleted": r[7], "fwd": r[8]}


def reply_info(mid):
    if not mid:
        return None
    m = get_message(mid)
    if not m:
        return None
    return {"id": m["id"], "name": m["name"], "display": display_of(m["name"]),
            "text": "" if m["deleted"] else (m["text"] or "")[:120]}


def edit_message(mid, room, name, text):
    cur = conn.execute(
        "UPDATE messages SET text=?, edited=1 WHERE id=? AND room=? AND name=? AND deleted=0",
        (text, mid, room, name))
    conn.commit()
    return cur.rowcount > 0


def delete_message(mid, room, name):
    cur = conn.execute(
        "UPDATE messages SET text='', deleted=1 WHERE id=? AND room=? AND name=? AND deleted=0",
        (mid, room, name))
    conn.commit()
    return cur.rowcount > 0


def delete_any(mid):
    cur = conn.execute("UPDATE messages SET text='', deleted=1 WHERE id=? AND deleted=0", (mid,))
    conn.commit()
    return cur.rowcount > 0


def fwd_info(name):
    return {"name": name, "display": display_of(name)} if name else None


def _pack(r, reactions=None):
    # r: id, name, text, avatar, created, edited, deleted, reply_to, display, color, hide_photo, fwd
    return {"type": "msg", "id": r[0], "name": r[1], "display": r[8] or r[1], "color": r[9] or "",
            "text": "" if r[6] else (r[2] or ""),
            "avatar": "🙂" if r[10] else (r[3] or "🙂"), "time": r[4] or 0, "edited": r[5] or 0,
            "deleted": r[6] or 0, "reply": reply_info(r[7]), "fwd": fwd_info(r[11]),
            "reactions": (reactions or {}).get(r[0], {})}


_SEL = ("SELECT m.id, m.name, m.text, u.avatar, m.created, m.edited, m.deleted, m.reply_to, u.display, u.color, "
        "u.hide_photo, m.fwd FROM messages m LEFT JOIN users u ON u.name=m.name ")


def last_messages(room, limit=80):
    rows = conn.execute(_SEL + "WHERE m.room=? ORDER BY m.id DESC LIMIT ?", (room, limit)).fetchall()
    rx = reactions_for([r[0] for r in rows])
    return [_pack(r, rx) for r in reversed(rows)]


def message_payload(mid):
    r = conn.execute(_SEL + "WHERE m.id=?", (mid,)).fetchone()
    if not r:
        return None
    return _pack(r, reactions_for([mid]))


# ---------- reaksiyalar va qadalgan xabar ----------

def reactions_for(ids):
    ids = list(ids)
    if not ids:
        return {}
    q = "SELECT msg_id, emoji, name FROM reactions WHERE msg_id IN (" + ",".join("?" * len(ids)) + ") ORDER BY rowid"
    out = {}
    for mid, emoji, name in conn.execute(q, ids).fetchall():
        out.setdefault(mid, {}).setdefault(emoji, []).append(name)
    return out


def toggle_reaction(mid, name, emoji):
    cur = conn.execute("SELECT emoji FROM reactions WHERE msg_id=? AND name=?", (mid, name)).fetchone()
    if cur and cur[0] == emoji:
        conn.execute("DELETE FROM reactions WHERE msg_id=? AND name=?", (mid, name))
    else:
        conn.execute("INSERT OR REPLACE INTO reactions (msg_id, name, emoji) VALUES (?, ?, ?)", (mid, name, emoji))
    conn.commit()
    return reactions_for([mid]).get(mid, {})


def set_pin(room, mid):
    conn.execute("INSERT OR REPLACE INTO pins (room, msg_id) VALUES (?, ?)", (room, mid))
    conn.commit()


def clear_pin(room):
    conn.execute("DELETE FROM pins WHERE room=?", (room,))
    conn.commit()


def pin_id(room):
    r = conn.execute("SELECT msg_id FROM pins WHERE room=?", (room,)).fetchone()
    return r[0] if r else None


def get_pin(room):
    mid = pin_id(room)
    if not mid:
        return None
    p = message_payload(mid)
    return p if p and not p["deleted"] else None


# ---------- guruhlar ----------

def room_gid(room):
    if room.startswith("g:") and room[2:].isdigit():
        return int(room[2:])
    return None


def group_room(gid):
    return "g:%d" % gid


def create_group(title, owner):
    import secrets as _s
    now = int(time.time() * 1000)
    cur = conn.execute("INSERT INTO chat_groups (title, avatar, owner, created, invite) VALUES (?, ?, ?, ?, ?)",
                       (title, "👥", owner, now, _s.token_urlsafe(8)))
    gid = cur.lastrowid
    conn.execute("INSERT INTO group_members (gid, name, role, joined) VALUES (?, ?, 'owner', ?)", (gid, owner, now))
    conn.commit()
    return gid


def get_group(gid):
    r = conn.execute("SELECT id, title, avatar, owner, created, invite FROM chat_groups WHERE id=?", (gid,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "title": r[1], "avatar": r[2] or "👥", "owner": r[3], "created": r[4], "invite": r[5]}


def member_role(gid, name):
    r = conn.execute("SELECT role FROM group_members WHERE gid=? AND name=?", (gid, name)).fetchone()
    return r[0] if r else None


def members_of(gid):
    rows = conn.execute(
        "SELECT m.name, m.role, u.display, u.avatar, u.hide_photo FROM group_members m JOIN users u ON u.name=m.name "
        "WHERE m.gid=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, m.joined", (gid,)).fetchall()
    return [{"name": r[0], "role": r[1], "display": r[2] or r[0], "avatar": "🙂" if r[4] else (r[3] or "🙂")} for r in rows]


def member_names(gid):
    return [r[0] for r in conn.execute("SELECT name FROM group_members WHERE gid=?", (gid,)).fetchall()]


def add_member(gid, name, role="member"):
    conn.execute("INSERT OR IGNORE INTO group_members (gid, name, role, joined) VALUES (?, ?, ?, ?)",
                 (gid, name, role, int(time.time() * 1000)))
    conn.commit()


def remove_member(gid, name):
    conn.execute("DELETE FROM group_members WHERE gid=? AND name=?", (gid, name))
    conn.commit()


def set_member_role(gid, name, role):
    conn.execute("UPDATE group_members SET role=? WHERE gid=? AND name=?", (role, gid, name))
    conn.commit()


def edit_group(gid, title=None, avatar=None):
    if title is not None:
        conn.execute("UPDATE chat_groups SET title=? WHERE id=?", (title, gid))
    if avatar is not None:
        conn.execute("UPDATE chat_groups SET avatar=? WHERE id=?", (avatar, gid))
    conn.commit()


def reset_invite(gid):
    import secrets as _s
    code = _s.token_urlsafe(8)
    conn.execute("UPDATE chat_groups SET invite=? WHERE id=?", (code, gid))
    conn.commit()
    return code


def group_by_invite(code):
    r = conn.execute("SELECT id FROM chat_groups WHERE invite=?", (code,)).fetchone()
    return r[0] if r else None


def user_groups(name):
    rows = conn.execute(
        "SELECT g.id, g.title, g.avatar, m.role, (SELECT COUNT(*) FROM group_members x WHERE x.gid=g.id) "
        "FROM group_members m JOIN chat_groups g ON g.id=m.gid WHERE m.name=?", (name,)).fetchall()
    return [{"id": r[0], "title": r[1], "avatar": r[2] or "👥", "role": r[3], "n": r[4]} for r in rows]


def delete_group(gid):
    room = group_room(gid)
    conn.execute("DELETE FROM group_members WHERE gid=?", (gid,))
    conn.execute("DELETE FROM chat_groups WHERE id=?", (gid,))
    conn.execute("DELETE FROM reads WHERE room=?", (room,))
    conn.execute("DELETE FROM pins WHERE room=?", (room,))
    conn.execute("DELETE FROM reactions WHERE msg_id IN (SELECT id FROM messages WHERE room=?)", (room,))
    conn.execute("DELETE FROM messages WHERE room=?", (room,))
    conn.commit()


def leave_group(gid, name):
    """Chiqish. Egasi chiqsa, huquq eng eski admin yoki a'zoga o'tadi; hech kim qolmasa guruh o'chadi."""
    role = member_role(gid, name)
    if role is None:
        return None
    remove_member(gid, name)
    rows = conn.execute(
        "SELECT name FROM group_members WHERE gid=? ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, joined",
        (gid,)).fetchall()
    if not rows:
        delete_group(gid)
        return "deleted"
    if role == "owner":
        set_member_role(gid, rows[0][0], "owner")
        conn.execute("UPDATE chat_groups SET owner=? WHERE id=?", (rows[0][0], gid))
        conn.commit()
    return "left"


# ---------- o'qildi belgisi va o'qilmaganlar ----------

def mark_read(room, name, upto):
    conn.execute(
        "INSERT INTO reads (room, name, last_id) VALUES (?, ?, ?) "
        "ON CONFLICT(room, name) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)",
        (room, name, upto))
    conn.commit()


def get_reads(room):
    rows = conn.execute("SELECT name, last_id FROM reads WHERE room=?", (room,)).fetchall()
    return {r[0]: r[1] for r in rows}


def unread(room, name):
    r = conn.execute("SELECT last_id FROM reads WHERE room=? AND name=?", (room, name)).fetchone()
    last = r[0] if r else 0
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE room=? AND id>? AND name!=? AND deleted=0",
        (room, last, name)).fetchone()[0]


def room_last(room):
    r = conn.execute(
        "SELECT name, text, created, deleted FROM messages WHERE room=? ORDER BY id DESC LIMIT 1",
        (room,)).fetchone()
    if not r:
        return None
    return {"name": r[0], "display": display_of(r[0]), "text": "" if r[3] else (r[1] or "")[:100],
            "time": r[2] or 0, "deleted": r[3] or 0}
