import sqlite3
import hashlib
import secrets

conn = sqlite3.connect("chat.db", check_same_thread=False)
conn.execute("CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, salt TEXT, hash TEXT, avatar TEXT, bio TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, name TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, name TEXT, text TEXT)")
conn.commit()

def make_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()

def new_token(name):
    token = secrets.token_hex(16)
    conn.execute("INSERT INTO sessions VALUES (?, ?)", (token, name))
    conn.commit()
    return token

def register(name, password):
    if conn.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        return None
    salt = secrets.token_hex(8)
    conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                 (name, salt, make_hash(password, salt), "🙂", ""))
    conn.commit()
    return new_token(name)

def login(name, password):
    row = conn.execute("SELECT salt, hash FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    if not secrets.compare_digest(make_hash(password, row[0]), row[1]):
        return None
    return new_token(name)

def name_by_token(token):
    row = conn.execute("SELECT name FROM sessions WHERE token=?", (token,)).fetchone()
    return row[0] if row else None

def get_profile(name):
    row = conn.execute("SELECT avatar, bio FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    return {"name": name, "avatar": row[0], "bio": row[1]}

def set_profile(name, avatar, bio):
    conn.execute("UPDATE users SET avatar=?, bio=? WHERE name=?", (avatar, bio[:200], name))
    conn.commit()

def save_message(room, name, text):
    conn.execute("INSERT INTO messages (room, name, text) VALUES (?, ?, ?)", (room, name, text))
    conn.commit()

def last_messages(room):
    rows = conn.execute(
        "SELECT m.name, m.text, u.avatar FROM messages m JOIN users u ON u.name=m.name "
        "WHERE m.room=? ORDER BY m.id DESC LIMIT 50", (room,)).fetchall()
    return [{"type": "msg", "name": r[0], "text": r[1], "avatar": r[2]} for r in reversed(rows)]