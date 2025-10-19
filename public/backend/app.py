# app.py
"""
Kompletný backend pre 2Launch (SQLite, jednoduché sessions, e-mail).
- Migruje existujúcu DB (pridá chýbajúce stĺpce).
- Generuje username/password pri registrácii a posiela uvítací email.
- Login vracia Bearer token, používa sa pre chránené endpointy.
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import string
from datetime import datetime, timedelta
import os
import time

DB_PATH = "database.db"

app = Flask(__name__)
CORS(app)

# =====================================
# 🚀 SMTP KONFIGURÁCIA (uprav podľa seba)
# =====================================
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "matejgerat@gmail.com"      # 👉 sem daj svoj Gmail
SMTP_PASS = "ibht ijyp iycn hynw"       # <-- zmeň na svoj App Password

# =====================================
# 📦 Inicializácia tabuľky(iek) ak neexistujú
# =====================================
def init_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    # Základná tabuľka (staršie verzie môžu mať menej stĺpcov — migrácia to doplní)
    c.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT,
            last_name TEXT,
            company_name TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            plan TEXT
            -- ďalšie stĺpce sa pridajú migráciou ak chýbajú
        )
    """)
    # sessions tabuľka na jednoduché tokeny
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            reg_id INTEGER,
            expires_at TEXT
        )
    """)
    conn.commit()
    conn.close()

# =====================================
# 🔧 Migračná funkcia: pridá chýbajúce stĺpce
# =====================================
def migrate_db(db_path=DB_PATH):
    """
    Skontroluje PRAGMA table_info(registrations) a pridá chýbajúce stĺpce.
    Je idempotentná (bezpečná spúšťať viackrát).
    """
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("PRAGMA table_info(registrations)")
    existing = [row[1] for row in c.fetchall()]

    def add_column(name, definition):
        if name not in existing:
            try:
                c.execute(f"ALTER TABLE registrations ADD COLUMN {name} {definition}")
                print(f"🔧 Pridaný stĺpec: {name} {definition}")
            except Exception as e:
                print(f"❌ Chyba pri pridávaní stĺpca {name}: {e}")

    # Požadované stĺpce v súčasnej verzii
    add_column("contact_method", "TEXT")
    add_column("username", "TEXT")
    add_column("password_hash", "TEXT")
    add_column("views", "INTEGER DEFAULT 0")
    add_column("orders", "INTEGER DEFAULT 0")
    add_column("created_at", "TEXT")
    # commit a close
    conn.commit()
    conn.close()

# =====================================
# 💌 Odoslanie e-mailu
# =====================================
def send_email(to, subject, body):
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print(f"✅ E-mail odoslaný na {to}")
        return True
    except Exception as e:
        print("❌ Chyba pri odosielaní e-mailu:", e)
        return False

# =====================================
# 🔧 Pomocné funkcie (username/password/session)
# =====================================
def slugify_name(name: str):
    s = (name or "").strip().lower().replace(" ", "_")
    s = "".join(ch for ch in s if ch.isalnum() or ch == "_")
    if not s:
        s = "user"
    return s

def username_exists(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM registrations WHERE username = ? LIMIT 1", (username,))
    r = c.fetchone()
    conn.close()
    return bool(r)

def generate_unique_username(company_name: str, max_attempts=20):
    base = slugify_name(company_name)
    for _ in range(max_attempts):
        suffix = ''.join(secrets.choice(string.digits) for _ in range(3))
        username = f"{base}_{suffix}"
        if not username_exists(username):
            return username
    # fallback: random token
    return f"{base}_{secrets.token_hex(4)}"

def generate_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def create_session(reg_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=7)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO sessions (token, reg_id, expires_at) VALUES (?, ?, ?)", (token, reg_id, expires))
    conn.commit()
    conn.close()
    return token, expires

def validate_token(token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT reg_id, expires_at FROM sessions WHERE token = ?", (token,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    reg_id, expires_at = row
    try:
        if datetime.fromisoformat(expires_at) < datetime.utcnow():
            return None
    except Exception:
        return None
    return reg_id

# =====================================
# 🧾 Email šablóny
# =====================================
EMAIL_TEMPLATES = {
    "welcome": {
        "subject": "Vitaj v 2Launch 🎉",
        "body": """Ahoj {first_name},

ďakujeme, že si sa zaregistroval/a na 2Launch!
Tvoj biznis "{company_name}" je pripravený na rast 🚀

Prístup do administračného rozhrania:
  URL: {admin_url}
  Užívateľské meno: {username}
  Heslo: {password}

Odporúčame ihneď po prihlásení zmeniť heslo.

S pozdravom,
Tím 2Launch
"""
    },
    "payment_reminder": {
        "subject": "Pripomienka platby",
        "body": """Dobrý deň {first_name},

chceli by sme Vám pripomenúť, že platba za plán {plan} ešte nebola uhradená.

Prosíme o jej dokončenie čo najskôr.
Ďakujeme, že používate 2Launch!
"""
    }
}

# =====================================
# 🔁 Inicializácia + migrácia pri štarte
# =====================================
init_db()
migrate_db()

# =====================================
# 🧾 REGISTRÁCIA NOVÉHO POUŽÍVATEĽA
# =====================================
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    required = ["first_name", "last_name", "company_name", "phone", "email", "address", "plan", "contact_method"]
    if not all(field in data and str(data[field]).strip() for field in required):
        return jsonify({"success": False, "error": "Vyplň všetky polia!"}), 400

    # generuj username a heslo
    username = generate_unique_username(data["company_name"])
    password = generate_password()
    password_hash = generate_password_hash(password)
    created_at = datetime.utcnow().isoformat()

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO registrations (first_name, last_name, company_name, phone, email, address, plan, contact_method, username, password_hash, views, orders, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["first_name"], data["last_name"], data["company_name"],
            data["phone"], data["email"], data["address"],
            data["plan"], data["contact_method"], username, password_hash, 0, 0, created_at
        ))
        reg_id = c.lastrowid
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError as e:
        # napr. duplicate username (veľmi nepravdepodobné, ale robustne riešime)
        print("DB IntegrityError:", e)
        return jsonify({"success": False, "error": "Chýba jedinečný username, skúste znova."}), 500
    except Exception as e:
        print("DB ERROR:", e)
        return jsonify({"success": False, "error": "Chyba pri uložení do DB."}), 500

    # Poslať uvítací email s prihlasovacími údajmi
    admin_url = "http://127.0.0.1:5500/admin.html"  # uprav podľa nasadenia
    body = EMAIL_TEMPLATES["welcome"]["body"].format(
        first_name=data["first_name"],
        company_name=data["company_name"],
        admin_url=admin_url,
        username=username,
        password=password
    )
    sent = send_email(data["email"], EMAIL_TEMPLATES["welcome"]["subject"], body)

    return jsonify({"success": True, "sent_email": sent, "username": username, "reg_id": reg_id})

# =====================================
# 🔍 KONTROLA EXISTENCIE FIRMY (podľa názvu)
# =====================================
@app.route("/api/check_name", methods=["GET"])
def check_name():
    name = request.args.get("name", "").strip().lower()
    if not name:
        return jsonify({"found": False})
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM registrations WHERE LOWER(company_name) = ? LIMIT 1", (name,))
    result = c.fetchone()
    conn.close()
    return jsonify({"found": bool(result)})

# =====================================
# 🔐 LOGIN (vráti token)
# =====================================
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"success": False, "error": "Chýbajú prihlasovacie údaje"}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, password_hash FROM registrations WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"success": False, "error": "Nesprávne meno alebo heslo"}), 401

    reg_id, password_hash = row
    if not check_password_hash(password_hash, password):
        return jsonify({"success": False, "error": "Nesprávne meno alebo heslo"}), 401

    token, expires = create_session(reg_id)
    return jsonify({"success": True, "token": token, "expires": expires, "reg_id": reg_id})

# =====================================
# 🔎 VALIDÁCIA TOKENU (jednoduchý endpoint)
# =====================================
@app.route("/api/auth/validate", methods=["GET"])
def api_validate():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"valid": False}), 401
    token = auth.split(" ", 1)[1]
    reg_id = validate_token(token)
    if not reg_id:
        return jsonify({"valid": False}), 401
    return jsonify({"valid": True, "reg_id": reg_id})

# =====================================
# 📋 ZÍSKANIE VŠETKÝCH REGISTRÁCIÍ (pre admin)
# =====================================
@app.route("/api/all_registrations", methods=["GET"])
def get_all_registrations():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, first_name, last_name, company_name, email, phone, plan, created_at, username, views, orders FROM registrations ORDER BY id DESC")
        rows = c.fetchall()
        conn.close()
        data = [dict(row) for row in rows]
        # Nahradiť None s vhodnými defaultami
        for r in data:
            r["views"] = r.get("views") or 0
            r["orders"] = r.get("orders") or 0
            r["created_at"] = r.get("created_at") or ""
        return jsonify(data)
    except Exception as e:
        print("ERROR get_all_registrations:", e)
        return jsonify({"error": "Chyba servera"}), 500

# =====================================
# 📋 ZÍSKANIE JEDNEJ REGISTRÁCIE (chránené)
# =====================================
@app.route("/api/registration/<int:reg_id>", methods=["GET"])
def get_registration(reg_id):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    token = auth.split(" ", 1)[1]
    valid_id = validate_token(token)
    if not valid_id or int(valid_id) != int(reg_id):
        return jsonify({"error": "Unauthorized"}), 401

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, company_name, email, phone, plan, contact_method, address, username, views, orders, created_at FROM registrations WHERE id = ?", (reg_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404

    r = dict(row)
    r["views"] = r.get("views") or 0
    r["orders"] = r.get("orders") or 0
    r["created_at"] = r.get("created_at") or ""
    return jsonify(r)

# =====================================
# 📈 METRIKY (chránené)
# =====================================
@app.route("/api/metrics/<int:reg_id>", methods=["GET"])
def get_metrics(reg_id):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    token = auth.split(" ", 1)[1]
    valid_id = validate_token(token)
    if not valid_id or int(valid_id) != int(reg_id):
        return jsonify({"error": "Unauthorized"}), 401

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT views, orders, created_at FROM registrations WHERE id = ?", (reg_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404

    views, orders, created_at = row
    views = views or 0
    orders = orders or 0
    conversion = round((orders / views * 100), 2) if views > 0 else 0.0
    days_online = 0
    if created_at:
        try:
            days_online = (datetime.utcnow() - datetime.fromisoformat(created_at)).days
        except Exception:
            days_online = 0
    return jsonify({
        "views": views,
        "orders": orders,
        "conversion_rate": conversion,
        "days_online": days_online
    })

# =====================================
# ❌ VYMAZANIE ZÁZNAMU (chránené)
# =====================================
@app.route("/api/delete/<int:record_id>", methods=["DELETE"])
def delete_record(record_id):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    token = auth.split(" ", 1)[1]
    valid_id = validate_token(token)
    if not valid_id or int(valid_id) != int(record_id):
        return jsonify({"error": "Unauthorized"}), 401

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM registrations WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# =====================================
# ✉️ ODOSLANIE EMAILU CEZ ADMIN PANEL (chránené)
# =====================================
@app.route("/api/send_email", methods=["POST"])
def api_send_email():
    data = request.get_json() or {}
    email = data.get("email")
    subject = data.get("subject")
    body = data.get("body")
    token = request.headers.get("Authorization", "")
    if not token.startswith("Bearer "):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    token = token.split(" ", 1)[1]
    reg_id = validate_token(token)
    if not reg_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if not all([email, subject, body]):
        return jsonify({"success": False, "error": "Chýbajú údaje"}), 400

    ok = send_email(email, subject, body)
    return jsonify({"success": ok})

# =====================================
# 🔧 (Voliteľné) Endpoint pre zvýšenie návštev (môže volať verejná /to/<slug> stránka)
# =====================================
@app.route("/api/increment_view/<int:reg_id>", methods=["POST"])
def increment_view(reg_id):
    # jednoduchá verejná akcia, zapisuje views (nie chránené)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE registrations SET views = COALESCE(views,0) + 1 WHERE id = ?", (reg_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print("ERROR increment_view:", e)
        return jsonify({"success": False}), 500

# =====================================
# 🔧 ŠTART SERVERA
# =====================================
if __name__ == "__main__":
    print("🚀 2Launch backend beží na http://127.0.0.1:5000")
    app.run(debug=True)

