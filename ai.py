"""AI do'st: bir nechta bepul AI xizmati bilan ishlaydi. Biri ishlamasa, keyingisiga o'tadi.

Kalitlar `ai_keys.json` faylida yoki muhit o'zgaruvchilarida (GEMINI_API_KEY, GROQ_API_KEY,
CEREBRAS_API_KEY, OPENROUTER_API_KEY) turadi. Fayl har so'rovda qayta o'qiladi, shuning uchun
kalitni o'zgartirgandan keyin serverni qayta ishga tushirish shart emas.
"""
import json
import os
import time
import urllib.error
import urllib.request

CONFIG_FILE = "ai_keys.json"

SYSTEM = (
    "Sen «AI do'st» — chat ilovasidagi samimiy, aqlli va xushmuomala do'stsan. "
    "Foydalanuvchi qaysi tilda (o'zbek, rus yoki ingliz) yozsa, o'sha tilda javob ber. "
    "Javoblaring qisqa, tabiiy va do'stona bo'lsin (odatda 1–4 gap), keraksiz uzun ro'yxatlar yozma. "
    "Savolga aniq javob ber; bilmasang, ochiq ayt. Zararli, noqonuniy yoki xavfli narsalarga yordam berma. "
    "Foydalanuvchi og'ir ruhiy holatda bo'lsa, hamdard bo'l va ishongan odamiga yoki mutaxassisga "
    "murojaat qilishni yumshoq tavsiya qil."
)

DEFAULTS = {
    "gemini": {"type": "gemini", "env": "GEMINI_API_KEY",
               "base": "https://generativelanguage.googleapis.com/v1beta",
               "models": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]},
    "groq": {"type": "openai", "env": "GROQ_API_KEY", "base": "https://api.groq.com/openai/v1",
             "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]},
    "cerebras": {"type": "openai", "env": "CEREBRAS_API_KEY", "base": "https://api.cerebras.ai/v1",
                 "models": ["llama-3.3-70b", "llama3.1-8b"]},
    "openrouter": {"type": "openai", "env": "OPENROUTER_API_KEY", "base": "https://openrouter.ai/api/v1",
                   "models": ["meta-llama/llama-3.3-70b-instruct:free", "google/gemma-3-27b-it:free"]},
}
ORDER = ["gemini", "groq", "cerebras", "openrouter"]

STATE = {}      # xizmat -> {"ok": vaqt, "err": matn, "until": vaqt}
TIMEOUT = 25


class AIError(Exception):
    def __init__(self, message, kind="other"):
        Exception.__init__(self, message)
        self.user_message = message
        self.kind = kind


def load_config():
    cfg = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    out = {"order": [n for n in cfg.get("order", ORDER) if n in DEFAULTS] or ORDER,
           "proxy": str(cfg.get("proxy", "") or ""), "providers": {}}
    for name, d in DEFAULTS.items():
        key = str(cfg.get(name, "") or os.environ.get(d["env"], "") or "").strip()
        p = dict(d)
        p["key"] = key
        m = (cfg.get("models") or {}).get(name)
        if m:
            p["models"] = [m] if isinstance(m, str) else list(m)
        b = (cfg.get("base") or {}).get(name)
        if b:
            p["base"] = str(b).rstrip("/")
        out["providers"][name] = p
    return out


def _opener(proxy):
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _post(opener, url, payload, headers):
    h = {"Content-Type": "application/json", "User-Agent": "chat-ai/1.0"}
    h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST")
    with opener.open(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(opener, url, headers):
    h = {"User-Agent": "chat-ai/1.0"}
    h.update(headers)
    with opener.open(urllib.request.Request(url, headers=h), timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _err_text(e):
    try:
        body = e.read().decode("utf-8", "ignore")
    except Exception:
        body = ""
    try:
        j = json.loads(body)
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")
        body = str(msg or body)
    except Exception:
        pass
    return body.strip().replace("\n", " ")[:160]


def _merge(history):
    """Ketma-ket bir xil roldagi xabarlarni birlashtiradi, birinchisi 'user' bo'lishi uchun."""
    out = []
    for role, text in history:
        text = (text or "").strip()
        if not text:
            continue
        if out and out[-1][0] == role:
            out[-1][1] += "\n" + text
        else:
            out.append([role, text])
    while out and out[0][0] != "user":
        out.pop(0)
    return out


def _gemini(p, opener, history, system):
    contents = [{"role": "user" if r == "user" else "model", "parts": [{"text": t}]} for r, t in _merge(history)]
    payload = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
               "generationConfig": {"maxOutputTokens": 600, "temperature": 0.8}}
    last = None
    models = list(p["models"])
    tried_discovery = False
    i = 0
    while i < len(models):
        model = models[i]
        i += 1
        url = "%s/models/%s:generateContent" % (p["base"], model)
        try:
            j = _post(opener, url, payload, {"x-goog-api-key": p["key"]})
            parts = (((j.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
            text = "".join(x.get("text", "") for x in parts).strip()
            if text:
                return text
            last = AIError("bo'sh javob", "other")
        except urllib.error.HTTPError as e:
            msg = _err_text(e)
            if e.code in (401, 403):
                raise AIError("kalit qabul qilinmadi (%d): %s" % (e.code, msg), "auth")
            if e.code == 429:
                raise AIError("limit tugadi (429)", "quota")
            if e.code in (400, 404):
                last = AIError("model topilmadi (%s): %s" % (model, msg), "model")
                if i >= len(models) and not tried_discovery:
                    tried_discovery = True
                    try:
                        lst = _get(opener, p["base"] + "/models", {"x-goog-api-key": p["key"]}).get("models", [])
                        found = [m["name"].split("/")[-1] for m in lst
                                 if "generateContent" in (m.get("supportedGenerationMethods") or [])
                                 and "flash" in m.get("name", "") and "lite" not in m.get("name", "")
                                 and "preview" not in m.get("name", "") and "image" not in m.get("name", "")
                                 and "tts" not in m.get("name", "") and m["name"].split("/")[-1] not in models]
                        models += sorted(found, reverse=True)[:2]
                    except Exception:
                        pass
            else:
                last = AIError("xizmat xatosi (%d): %s" % (e.code, msg), "other")
        except Exception as e:
            raise AIError("ulanib bo'lmadi: %s" % str(e)[:100], "net")
    raise last or AIError("javob olinmadi", "other")


def _openai(p, opener, history, system):
    msgs = [{"role": "system", "content": system}]
    for r, t in _merge(history):
        msgs.append({"role": "user" if r == "user" else "assistant", "content": t})
    last = None
    models = list(p["models"])
    tried_discovery = False
    i = 0
    while i < len(models):
        model = models[i]
        i += 1
        try:
            j = _post(opener, p["base"] + "/chat/completions",
                      {"model": model, "messages": msgs, "max_tokens": 600, "temperature": 0.8},
                      {"Authorization": "Bearer " + p["key"]})
            text = (((j.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            if text:
                return text
            last = AIError("bo'sh javob", "other")
        except urllib.error.HTTPError as e:
            msg = _err_text(e)
            if e.code in (401, 403):
                raise AIError("kalit qabul qilinmadi (%d): %s" % (e.code, msg), "auth")
            if e.code == 429:
                raise AIError("limit tugadi (429)", "quota")
            if e.code in (400, 404):
                last = AIError("model topilmadi (%s): %s" % (model, msg), "model")
                if i >= len(models) and not tried_discovery:
                    tried_discovery = True
                    try:
                        lst = _get(opener, p["base"] + "/models", {"Authorization": "Bearer " + p["key"]}).get("data", [])
                        ids = [m.get("id", "") for m in lst]
                        pref = [x for x in ids if any(k in x.lower() for k in ("llama", "gpt-oss", "qwen", "gemma"))
                                and not any(k in x.lower() for k in ("guard", "whisper", "tts", "embed", "vision", "audio"))
                                and x not in models]
                        models += pref[:2]
                    except Exception:
                        pass
            else:
                last = AIError("xizmat xatosi (%d): %s" % (e.code, msg), "other")
        except Exception as e:
            raise AIError("ulanib bo'lmadi: %s" % str(e)[:100], "net")
    raise last or AIError("javob olinmadi", "other")


def ask(history, system=SYSTEM):
    """history: [("user"|"assistant", matn), ...]. (javob, xizmat nomi) qaytaradi yoki AIError ko'taradi."""
    cfg = load_config()
    opener = _opener(cfg["proxy"])
    now = time.time()
    configured = [n for n in cfg["order"] if cfg["providers"][n]["key"]]
    if not configured:
        raise AIError("AI hozircha ulanmagan: serverdagi ai_keys.json fayliga kalit yozilmagan.", "nokey")
    errors = []
    for name in configured:
        st = STATE.setdefault(name, {})
        if st.get("until", 0) > now:
            errors.append("%s: kutish (%d s)" % (name, int(st["until"] - now)))
            continue
        p = cfg["providers"][name]
        try:
            text = (_gemini if p["type"] == "gemini" else _openai)(p, opener, history, system)
            st["ok"] = time.time(); st["err"] = ""; st["until"] = 0
            return text, name
        except AIError as e:
            st["err"] = e.user_message
            st["until"] = time.time() + (600 if e.kind == "auth" else 120 if e.kind in ("quota", "net") else 30)
            errors.append("%s: %s" % (name, e.user_message))
    raise AIError("AI hozir javob bera olmayapti. " + " | ".join(errors)[:300], "all")


def enabled():
    """Kamida bitta kalit yozilgan bo'lsa True. Aks holda AI do'st yashirin turadi."""
    return any(p["key"] for p in load_config()["providers"].values())


def status():
    cfg = load_config()
    out = []
    now = time.time()
    for name in cfg["order"]:
        st = STATE.get(name, {})
        out.append({"name": name, "configured": bool(cfg["providers"][name]["key"]),
                    "ok": int(st.get("ok", 0) * 1000), "err": st.get("err", ""),
                    "wait": max(0, int(st.get("until", 0) - now))})
    return out


def split_text(text, size=900):
    text = (text or "").strip()
    parts = []
    while len(text) > size:
        cut = max(text.rfind("\n", 0, size), text.rfind(". ", 0, size), text.rfind(" ", 0, size))
        if cut < size // 2:
            cut = size
        parts.append(text[:cut + 1].strip())
        text = text[cut + 1:].strip()
    if text:
        parts.append(text)
    return parts[:3]
