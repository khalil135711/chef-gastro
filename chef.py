#!/usr/bin/env python3
"""
CHEF - Gastro-Kassensystem (MVP zum Testen)
Nur Python-Standardbibliothek (Python 3.9+), keine Installation noetig.

    python chef.py demo     -> kompletter Testlauf in der Konsole
    python chef.py serve    -> Web-UI + JSON-API auf http://localhost:8000 (index.html im selben Ordner)

WICHTIG: Die TSE-Signatur ist hier ein MOCK (HMAC) und NICHT rechtsgueltig.
Fuer den echten Betrieb: Cloud-TSE (z.B. Fiskaly) anbinden.
Zahlung ist ebenfalls simuliert (payment_method wird nur gespeichert).
"""
import hashlib
import hmac
import json
import math
import os
import random
import re
import secrets
import sqlite3
import sys
import time
import io
import struct
import subprocess
import tempfile
import zipfile
import zlib
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import csv
import threading

try:
    import licensing
except ImportError:  # Einzelbetrieb ohne Mandanten-/Stripe-Modul
    import licensing_stub as licensing
import accounting

DB_PATH = os.getenv("CHEF_DB", "chef.db")
LOYALTY_EVERY = 5  # jede 5. Bestellung: guenstigster Artikel gratis
TSE_KEY = b"MOCK-TSE-KEY-NUR-ZUM-TESTEN"
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
INDEX2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index2.html")
KUNDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kunde.html")
BUCHHALTUNG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "buchhaltung.html")
WILLKOMMEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "willkommen.html")
BESTELLUNG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bestellung.html")
_tl = threading.local()  # aktueller Mandant der laufenden Anfrage


def _page(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ("<!doctype html><meta charset=utf-8><title>CHEF</title><body style='font:16px system-ui;max-width:640px;margin:40px auto'>"
                "<h2>CHEF Gastro</h2><p>Diese Seite (%s) liegt nicht im Ordner. Verfügbar: "
                "<a href='/buchhaltung'>/buchhaltung</a>, <a href='/health'>/health</a>. API siehe README.</p>"
                % os.path.basename(path))


def _tenant():
    return getattr(_tl, "tenant", None)

DEMO = os.getenv("CHEF_DEMO") == "1"  # oeffentliche Demo: Testdaten + bekannte Zugangsdaten
ADMIN_PIN = os.getenv("CHEF_ADMIN_PIN", "demo1234" if DEMO else "0000")
SERVICES = ["Kasse", "Bedienung", "Küche", "Bar", "Reinigung"]
TOKENS = {}


class AuthError(Exception):
    pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, category_id INTEGER, name TEXT NOT NULL,
  price_cents INTEGER NOT NULL, tax_rate REAL NOT NULL DEFAULT 19, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS ingredients(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, unit TEXT NOT NULL,
  stock REAL NOT NULL DEFAULT 0, min_stock REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS recipes(
  product_id INTEGER, ingredient_id INTEGER, qty REAL NOT NULL,
  PRIMARY KEY(product_id, ingredient_id));
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT,
  points INTEGER NOT NULL DEFAULT 0, orders_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL DEFAULT 1, customer_id INTEGER,
  total_cents INTEGER NOT NULL, payment_method TEXT NOT NULL, table_id INTEGER,
  tse_counter INTEGER NOT NULL, tse_signature TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, product_id INTEGER,
  name TEXT NOT NULL, qty INTEGER NOT NULL, price_cents INTEGER NOT NULL, tax_rate REAL NOT NULL);
CREATE TABLE IF NOT EXISTS employees(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT, max_hours_week REAL NOT NULL DEFAULT 40);
CREATE TABLE IF NOT EXISTS availability(
  employee_id INTEGER, weekday INTEGER, avail_from TEXT, avail_to TEXT);
CREATE TABLE IF NOT EXISTS tables(id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL DEFAULT 1, name TEXT NOT NULL, seats INTEGER NOT NULL DEFAULT 4, x INTEGER NOT NULL DEFAULT 20, y INTEGER NOT NULL DEFAULT 20, shape TEXT NOT NULL DEFAULT 'square');
CREATE TABLE IF NOT EXISTS wishes(employee_id INTEGER, weekday INTEGER, pref TEXT, PRIMARY KEY(employee_id, weekday));
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY, ingredient_id INTEGER NOT NULL, qty REAL NOT NULL, mhd TEXT,
  location TEXT NOT NULL DEFAULT 'Lager', received_at TEXT NOT NULL, taken_by TEXT, taken_at TEXT);
CREATE TABLE IF NOT EXISTS stock_log(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, ingredient_id INTEGER NOT NULL, batch_id INTEGER,
  action TEXT NOT NULL, qty REAL NOT NULL, who TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS absences(
  id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, start_day TEXT NOT NULL, end_day TEXT NOT NULL,
  kind TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS admin_auth(id INTEGER PRIMARY KEY, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY, employee_id INTEGER, day TEXT, shift_start TEXT, shift_end TEXT);
"""


# ----------------------------------------------------------------- Basis
def connect(path=None):
    db = sqlite3.connect(path or DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    ocols = [r[1] for r in db.execute("PRAGMA table_info(orders)")]
    for col, typ in (("status", "TEXT DEFAULT 'abgeholt'"), ("pickup_token", "TEXT"), ("pickup_no", "INTEGER"),
                     ("ready_at", "TEXT"), ("ring", "INTEGER DEFAULT 0")):
        if col not in ocols:
            db.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
    ccols = [r[1] for r in db.execute("PRAGMA table_info(customers)")]
    for c in ("phone", "card_code", "view_token", "consent_at"):
        if c not in ccols:
            db.execute(f"ALTER TABLE customers ADD COLUMN {c} TEXT")
    for r in db.execute("SELECT id FROM customers WHERE card_code IS NULL OR view_token IS NULL").fetchall():
        db.execute("UPDATE customers SET card_code=?, view_token=? WHERE id=?", (_new_code(), secrets.token_urlsafe(18), r["id"]))
    db.commit()
    accounting.ensure_schema(db)
    if "vacation_days" not in [r[1] for r in db.execute("PRAGMA table_info(employees)")]:
        db.execute("ALTER TABLE employees ADD COLUMN vacation_days INTEGER DEFAULT 26")
    ecols = [r[1] for r in db.execute("PRAGMA table_info(employees)")]
    for c in ("number", "pin_hash", "services", "note"):
        if c not in ecols:
            db.execute(f"ALTER TABLE employees ADD COLUMN {c} TEXT")
    if "table_id" not in [r[1] for r in db.execute("PRAGMA table_info(orders)")]:
        db.execute("ALTER TABLE orders ADD COLUMN table_id INTEGER")
    if not db.execute("SELECT 1 FROM locations").fetchone():
        db.execute("INSERT INTO locations(name) VALUES('Hauptfiliale')")
        db.commit()
    return db


def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def eur(cents):
    return f"{cents / 100:.2f}".replace(".", ",")


def _t(hhmm):
    h, m = hhmm.split(":")
    return int(h) + int(m) / 60


# ----------------------------------------------------------------- Einstellungen
DEFAULTS = {"name": "CHEF Gastro", "address": "", "tax_id": "", "phone": "",
            "footer": "Vielen Dank fuer Ihren Besuch!", "loyalty_every": "5", "points_per_euro": "1",
            "ust_id": "", "iban": "", "bic": ""}


def get_settings(db):
    d = dict(DEFAULTS)
    d.update({r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings")})
    return d


def save_settings(db, data):
    for k, v in data.items():
        if k not in DEFAULTS:
            continue
        if k in ("loyalty_every", "points_per_euro") and int(v) < 1:
            raise ValueError(f"{k} muss mindestens 1 sein")
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))
    db.commit()
    return get_settings(db)


def diagnose(db):
    out = []
    add = lambda n, st, d: out.append({"name": n, "status": st, "detail": d})
    qc = db.execute("PRAGMA quick_check").fetchone()[0]
    add("Datenbank", "ok" if qc == "ok" else "bad", f"Integritaetspruefung: {qc}")
    last = db.execute("SELECT * FROM orders ORDER BY tse_counter DESC LIMIT 1").fetchone()
    if last:
        valid = tse_sign(last["tse_counter"], last["created_at"], last["total_cents"]) == last["tse_signature"]
        add("TSE (Mock)", "ok" if valid else "bad",
            f"Signatur von Beleg #{last['tse_counter']} ist gueltig" if valid else "Signatur des letzten Belegs ist ungueltig")
        cnt, mx = db.execute("SELECT COUNT(*), MAX(tse_counter) FROM orders").fetchone()
        add("Belegnummern", "ok" if cnt == mx else "bad",
            f"{cnt} Belege, lueckenlos" if cnt == mx else f"Luecke: {mx - cnt} Nummern fehlen")
    else:
        add("TSE (Mock)", "warn", "Noch keine Bestellung zum Pruefen")
    neg = [r["name"] for r in db.execute("SELECT name FROM ingredients WHERE stock<0")]
    add("Lagerbestand", "bad" if neg else "ok", ("Negativer Bestand: " + ", ".join(neg)) if neg else "Kein negativer Bestand")
    add("Zahlungsterminal", "warn", "Nicht angebunden (Simulation). Stripe Terminal oder SumUp noetig.")
    add("Bondrucker", "warn", "Browser-Druck aktiv. Bluetooth-Drucker (ESC/POS) nicht eingerichtet.")
    add("Systemzeit", "ok", datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
    return out


# ----------------------------------------------------------------- Personal (Urlaub, Stunden)
def add_absence(db, employee_id, start, end, kind="urlaub", note=""):
    if kind not in ("urlaub", "krank"):
        raise ValueError("kind muss urlaub oder krank sein")
    if date.fromisoformat(end) < date.fromisoformat(start):
        raise ValueError("Das Ende liegt vor dem Beginn")
    if not db.execute("SELECT 1 FROM employees WHERE id=?", (employee_id,)).fetchone():
        raise LookupError("Mitarbeiter nicht gefunden")
    cur = db.execute("INSERT INTO absences(employee_id,start_day,end_day,kind,note) VALUES(?,?,?,?,?)",
                     (employee_id, start, end, kind, note))
    db.commit()
    return {"id": cur.lastrowid}


def _hours(db, eid, d1, d2):
    return sum(_t(r["shift_end"]) - _t(r["shift_start"]) for r in db.execute(
        "SELECT shift_start, shift_end FROM shifts WHERE employee_id=? AND day>=? AND day<=?",
        (eid, d1.isoformat(), d2.isoformat())))


def hr_summary(db, eid):
    e = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not e:
        raise LookupError("Mitarbeiter nicht gefunden")
    today = date.today()
    mon = today - timedelta(today.weekday())
    m1 = today.replace(day=1)
    m2 = (m1 + timedelta(32)).replace(day=1) - timedelta(1)
    y1, y2 = date(today.year, 1, 1), date(today.year, 12, 31)

    def days(kind, a, b):
        n = 0
        for r in db.execute("SELECT start_day, end_day FROM absences WHERE employee_id=? AND kind=?"
                            " AND end_day>=? AND start_day<=?", (eid, kind, a.isoformat(), b.isoformat())):
            n += (min(date.fromisoformat(r["end_day"]), b) - max(date.fromisoformat(r["start_day"]), a)).days + 1
        return n
    total = e["vacation_days"] if e["vacation_days"] is not None else 26
    used = days("urlaub", y1, y2)
    return {"name": e["name"], "role": e["role"], "year": today.year,
            "hours_week": round(_hours(db, eid, mon, mon + timedelta(6)), 1), "max_hours_week": e["max_hours_week"],
            "hours_month": round(_hours(db, eid, m1, m2), 1),
            "vacation_total": total, "vacation_used": used, "vacation_left": total - used,
            "sick_days_year": days("krank", y1, y2),
            "upcoming": rows(db, "SELECT kind, start_day, end_day FROM absences WHERE employee_id=? AND end_day>=?"
                                 " ORDER BY start_day LIMIT 3", (eid, today.isoformat()))}


# ----------------------------------------------------------------- MHD und Entnahme
LOCATIONS = ["Lager", "Kühlschrank", "Tiefkühler"]


class Conflict(Exception):
    def __init__(self, msg, warnings):
        super().__init__(msg)
        self.payload = {"error": msg, "warnings": warnings}


def _days_left(mhd):
    return None if not mhd else (date.fromisoformat(mhd) - date.today()).days


def _mhd_status(d):
    if d is None:
        return "ok", "ohne MHD"
    if d < 0:
        return "bad", f"seit {-d} Tg. abgelaufen"
    if d == 0:
        return "bad", "läuft heute ab"
    if d == 1:
        return "warn", "läuft morgen ab"
    return ("warn" if d <= 3 else "ok"), f"noch {d} Tage"


def _log(db, ingredient_id, batch_id, action, qty, who, note=""):
    db.execute("INSERT INTO stock_log(ts,ingredient_id,batch_id,action,qty,who,note) VALUES(?,?,?,?,?,?,?)",
               (datetime.now().isoformat(timespec="seconds"), ingredient_id, batch_id, action, qty, who, note))


def list_batches(db, ingredient_id=None):
    q = ("SELECT b.*, i.name ingredient, i.unit FROM batches b JOIN ingredients i ON i.id=b.ingredient_id"
         " WHERE b.qty>0.0001")
    args = ()
    if ingredient_id:
        q, args = q + " AND b.ingredient_id=?", (ingredient_id,)
    out = rows(db, q + " ORDER BY (b.mhd IS NULL), b.mhd, b.id", args)
    for b in out:
        b["days_left"] = _days_left(b["mhd"])
        b["level"], b["status"] = _mhd_status(b["days_left"])
        b["qty"] = round(b["qty"], 2)
    return out


def mhd_overview(db, days=3):
    bs = list_batches(db)
    return {"days": days,
            "expired": [b for b in bs if b["days_left"] is not None and b["days_left"] < 0],
            "soon": [b for b in bs if b["days_left"] is not None and 0 <= b["days_left"] <= days]}


def check_take(db, ingredient_id):
    ing = db.execute("SELECT * FROM ingredients WHERE id=?", (ingredient_id,)).fetchone()
    if not ing:
        raise LookupError("Zutat nicht gefunden")
    bs = list_batches(db, ingredient_id)
    w = []
    for b in bs:
        d = b["days_left"]
        if d is not None and d < 0:
            w.append({"kind": "expired", "level": "bad", "text":
                      f"MHD abgelaufen: {b['qty']:g} {ing['unit']} im {b['location']} (MHD {b['mhd']}). Nicht verwenden, bitte entsorgen."})
        elif b["location"] != "Lager":
            who = f" (entnommen von {b['taken_by']} am {b['taken_at'][:10]})" if b["taken_by"] else ""
            w.append({"kind": "open", "level": "warn", "text":
                      f"Schon entnommen: {b['qty']:g} {ing['unit']} {ing['name']} liegen noch im {b['location']}{who}. Bitte zuerst diese verbrauchen."})
        if d is not None and 0 <= d <= 1:
            w.append({"kind": "soon", "level": "warn", "text":
                      f"{b['qty']:g} {ing['unit']} im {b['location']} {b['status']} (MHD {b['mhd']})."})
    good = [b for b in bs if b["days_left"] is None or b["days_left"] >= 0]
    rec = next((b for b in good if b["location"] != "Lager"), None) or next((b for b in good), None)
    return {"ingredient": {"id": ing["id"], "name": ing["name"], "unit": ing["unit"]}, "batches": bs,
            "warnings": w, "recommended_batch_id": rec["id"] if rec else None}


def receive_goods(db, ingredient_id, qty, mhd=None, location="Lager", who=None):
    qty = float(qty)
    if qty <= 0:
        raise ValueError("Menge muss groesser als 0 sein")
    if location not in LOCATIONS:
        raise ValueError("Ungueltiger Lagerort")
    if not db.execute("SELECT 1 FROM ingredients WHERE id=?", (ingredient_id,)).fetchone():
        raise LookupError("Zutat nicht gefunden")
    if mhd and date.fromisoformat(mhd) < date.today():
        raise ValueError("Das MHD liegt in der Vergangenheit")
    cur = db.execute("INSERT INTO batches(ingredient_id,qty,mhd,location,received_at) VALUES(?,?,?,?,?)",
                     (ingredient_id, qty, mhd or None, location, datetime.now().isoformat(timespec="seconds")))
    db.execute("UPDATE ingredients SET stock=stock+? WHERE id=?", (qty, ingredient_id))
    _log(db, ingredient_id, cur.lastrowid, "eingelagert", qty, who, f"{location}, MHD {mhd or 'ohne'}")
    db.commit()
    return {"batch_id": cur.lastrowid}


def take_goods(db, ingredient_id, batch_id, qty, to_location="Kühlschrank", who=None, force=False):
    qty = float(qty)
    if qty <= 0:
        raise ValueError("Menge muss groesser als 0 sein")
    if to_location not in LOCATIONS:
        raise ValueError("Ungueltiger Zielort")
    b = db.execute("SELECT * FROM batches WHERE id=? AND ingredient_id=?", (batch_id, ingredient_id)).fetchone()
    if not b:
        raise LookupError("Charge nicht gefunden")
    if b["qty"] + 1e-9 < qty:
        raise ValueError(f"In dieser Charge sind nur noch {b['qty']:g} vorhanden")
    d = _days_left(b["mhd"])
    if d is not None and d < 0:
        raise ValueError("Das MHD ist abgelaufen. Diese Charge darf nicht entnommen werden.")
    if b["location"] == to_location:
        raise ValueError("Die Ware liegt schon dort")
    if not force:
        chk = check_take(db, ingredient_id)
        warns = [x for x in chk["warnings"] if x["kind"] == "open"]
        for x in chk["batches"]:
            if (x["location"] == "Lager" and x["id"] != b["id"] and x["mhd"] and b["mhd"]
                    and x["mhd"] < b["mhd"] and x["days_left"] >= 0):
                warns.append({"kind": "older", "level": "warn",
                              "text": f"Es gibt eine ältere Charge mit MHD {x['mhd']}. Bitte zuerst diese nehmen."})
        if warns:
            raise Conflict("Bitte prüfen, bevor du entnimmst", warns)
    now = datetime.now().isoformat(timespec="seconds")
    db.execute("UPDATE batches SET qty=qty-? WHERE id=?", (qty, b["id"]))
    t = db.execute("SELECT id FROM batches WHERE ingredient_id=? AND location=? AND IFNULL(mhd,'')=IFNULL(?,'')"
                   " AND qty>0.0001", (ingredient_id, to_location, b["mhd"])).fetchone()
    if t:
        db.execute("UPDATE batches SET qty=qty+?, taken_by=?, taken_at=? WHERE id=?", (qty, who, now, t["id"]))
    else:
        db.execute("INSERT INTO batches(ingredient_id,qty,mhd,location,received_at,taken_by,taken_at)"
                   " VALUES(?,?,?,?,?,?,?)", (ingredient_id, qty, b["mhd"], to_location, b["received_at"], who, now))
    db.execute("DELETE FROM batches WHERE qty<=0.0001")
    _log(db, ingredient_id, b["id"], "entnommen", qty, who, f"{b['location']} -> {to_location}")
    db.commit()
    return {"ok": True, "moved": qty, "to": to_location}


def use_up(db, batch_id, qty=None, reason="verbraucht", who=None):
    if reason not in ("verbraucht", "entsorgt"):
        raise ValueError("reason muss verbraucht oder entsorgt sein")
    b = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        raise LookupError("Charge nicht gefunden")
    q = b["qty"] if qty in (None, "") else float(qty)
    if q <= 0 or q > b["qty"] + 1e-9:
        raise ValueError(f"Ungueltige Menge (vorhanden: {b['qty']:g})")
    db.execute("UPDATE batches SET qty=qty-? WHERE id=?", (q, batch_id))
    db.execute("UPDATE ingredients SET stock=stock-? WHERE id=?", (q, b["ingredient_id"]))
    db.execute("DELETE FROM batches WHERE qty<=0.0001")
    _log(db, b["ingredient_id"], batch_id, reason, q, who, b["location"])
    db.commit()
    return {"ok": True}


def consume_batches(db, ingredient_id, qty):
    """Verkauf verbraucht zuerst, was schon im Kuehlschrank liegt, dann nach MHD (aelteste zuerst)."""
    left = qty
    for b in db.execute("SELECT * FROM batches WHERE ingredient_id=? AND qty>0.0001 ORDER BY"
                        " (location='Kühlschrank') DESC, (mhd IS NULL), mhd, id", (ingredient_id,)).fetchall():
        if left <= 0:
            break
        use = min(b["qty"], left)
        db.execute("UPDATE batches SET qty=qty-? WHERE id=?", (use, b["id"]))
        left -= use
    db.execute("DELETE FROM batches WHERE qty<=0.0001")


def seed_batches(db):
    ing = {r["name"]: r["stock"] for r in db.execute("SELECT name,stock FROM ingredients")}
    ids = {r["name"]: r["id"] for r in db.execute("SELECT id,name FROM ingredients")}
    now = datetime.now().isoformat(timespec="seconds")

    def put(name, qty, days, loc="Lager", who=None):
        if qty > 0.0001:
            db.execute("INSERT INTO batches(ingredient_id,qty,mhd,location,received_at,taken_by,taken_at)"
                       " VALUES(?,?,?,?,?,?,?)", (ids[name], qty, (date.today() + timedelta(days)).isoformat(),
                                                  loc, now, who, now if who else None))
    put("Broetchen", ing["Broetchen"], 3)
    p = min(8, ing["Patty"]); put("Patty", p, 1); put("Patty", ing["Patty"] - p, 9)
    k = min(10, ing["Kaese"]); put("Kaese", k, -1); put("Kaese", ing["Kaese"] - k, 20)
    put("Kartoffeln", ing["Kartoffeln"], 14); put("Cola-Flasche", ing["Cola-Flasche"], 120)
    put("Kaffeebohnen", ing["Kaffeebohnen"], 60)
    m = min(2, ing["Milch"]); put("Milch", m, 3, "Kühlschrank", "Amina"); put("Milch", ing["Milch"] - m, 7)
    db.commit()


# ----------------------------------------------------------------- Tischplan
def add_table(db, name=None, seats=4, x=20, y=20, shape="square"):
    if shape not in ("square", "round"):
        raise ValueError("shape muss square oder round sein")
    n = db.execute("SELECT COUNT(*) FROM tables").fetchone()[0] + 1
    name = (name or "").strip() or f"Tisch {n}"
    cur = db.execute("INSERT INTO tables(name,seats,x,y,shape) VALUES(?,?,?,?,?)",
                     (name, int(seats), int(x), int(y), shape))
    db.commit()
    return rows(db, "SELECT * FROM tables WHERE id=?", (cur.lastrowid,))[0]


def update_table(db, tid, **f):
    f = {k: v for k, v in f.items() if k in {"name", "seats", "x", "y", "shape"}}
    if not f:
        raise ValueError("Keine gueltigen Felder")
    if f.get("shape", "square") not in ("square", "round"):
        raise ValueError("shape muss square oder round sein")
    cur = db.execute(f"UPDATE tables SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), tid))
    if cur.rowcount == 0:
        raise LookupError("Tisch nicht gefunden")
    db.commit()
    return rows(db, "SELECT * FROM tables WHERE id=?", (tid,))[0]


def delete_table(db, tid):
    if db.execute("DELETE FROM tables WHERE id=?", (tid,)).rowcount == 0:
        raise LookupError("Tisch nicht gefunden")
    db.commit()
    return {"deleted": tid}


# ----------------------------------------------------------------- Stammdaten
def add_category(db, name):
    cur = db.execute("INSERT INTO categories(name) VALUES(?)", (name,))
    db.commit()
    return {"id": cur.lastrowid, "name": name}


def add_product(db, name, price_cents, category_id=None, tax_rate=19):
    if int(price_cents) < 0:
        raise ValueError("price_cents darf nicht negativ sein")
    cur = db.execute(
        "INSERT INTO products(category_id,name,price_cents,tax_rate) VALUES(?,?,?,?)",
        (category_id, name, int(price_cents), float(tax_rate)))
    db.commit()
    return {"id": cur.lastrowid, "name": name, "price_cents": int(price_cents)}


def update_product(db, pid, **fields):
    allowed = {"name", "price_cents", "tax_rate", "active", "category_id"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        raise ValueError("Keine gueltigen Felder")
    sets = ", ".join(f"{k}=?" for k in fields)
    cur = db.execute(f"UPDATE products SET {sets} WHERE id=?", (*fields.values(), pid))
    if cur.rowcount == 0:
        raise LookupError("Produkt nicht gefunden")
    db.commit()
    return rows(db, "SELECT * FROM products WHERE id=?", (pid,))[0]


def add_ingredient(db, name, unit, stock=0, min_stock=0):
    cur = db.execute("INSERT INTO ingredients(name,unit,stock,min_stock) VALUES(?,?,?,?)",
                     (name, unit, stock, min_stock))
    db.commit()
    return {"id": cur.lastrowid, "name": name}


def set_recipe(db, product_id, parts):
    """parts = [{"ingredient_id": 1, "qty": 2}, ...]"""
    db.execute("DELETE FROM recipes WHERE product_id=?", (product_id,))
    for p in parts:
        db.execute("INSERT INTO recipes(product_id,ingredient_id,qty) VALUES(?,?,?)",
                   (product_id, p["ingredient_id"], p["qty"]))
    db.commit()
    return {"product_id": product_id, "parts": len(parts)}


def add_customer(db, name, email=None):
    if not name or not name.strip():
        raise ValueError("Name fehlt")
    cur = db.execute("INSERT INTO customers(name,email,card_code,view_token) VALUES(?,?,?,?)",
                     (name, email, _new_code(), secrets.token_urlsafe(18)))
    db.commit()
    return {"id": cur.lastrowid, "name": name}


def add_employee(db, name, role="Service", max_hours_week=40):
    if not name or not name.strip():
        raise ValueError("Name fehlt")
    cur = db.execute("INSERT INTO employees(name,role,max_hours_week) VALUES(?,?,?)",
                     (name, role, max_hours_week))
    db.commit()
    return {"id": cur.lastrowid, "name": name}


def list_employees(db):
    out = rows(db, "SELECT * FROM employees ORDER BY name")
    for e in out:
        e["availability"] = rows(db, 'SELECT weekday, avail_from AS "from", avail_to AS "to"'
                                     " FROM availability WHERE employee_id=? ORDER BY weekday", (e["id"],))
    return out


def set_availability(db, employee_id, slots):
    """slots = [{"weekday": 0, "from": "09:00", "to": "20:00"}]  (0 = Montag)"""
    for s in slots:
        if not 0 <= int(s["weekday"]) <= 6:
            raise ValueError("weekday muss zwischen 0 (Montag) und 6 (Sonntag) liegen")
        if s["from"] >= s["to"]:
            raise ValueError("Die Startzeit muss vor der Endzeit liegen")
    db.execute("DELETE FROM availability WHERE employee_id=?", (employee_id,))
    for s in slots:
        db.execute("INSERT INTO availability VALUES(?,?,?,?)",
                   (employee_id, s["weekday"], s["from"], s["to"]))
    db.commit()
    return {"employee_id": employee_id, "slots": len(slots)}


# ----------------------------------------------------------------- Kasse
def tse_sign(counter, created_at, total):
    msg = f"{counter}|{created_at}|{total}".encode()
    return hmac.new(TSE_KEY, msg, hashlib.sha256).hexdigest()[:40]


def make_order(db, items, location_id=1, customer_id=None,
               payment_method="card", created_at=None, table_id=None):
    if not items:
        raise ValueError("Bestellung ohne Artikel")
    created_at = created_at or datetime.now().isoformat(timespec="seconds")
    lines = []
    for it in items:
        qty = int(it["qty"])
        if qty <= 0:
            raise ValueError("qty muss > 0 sein")
        p = db.execute("SELECT * FROM products WHERE id=? AND active=1",
                       (it["product_id"],)).fetchone()
        if not p:
            raise LookupError(f"Produkt {it['product_id']} nicht gefunden")
        lines.append({"product_id": p["id"], "name": p["name"], "qty": qty,
                      "price_cents": p["price_cents"], "tax_rate": p["tax_rate"]})
    real_lines = list(lines)

    free_item = None
    cust = None
    if customer_id:
        cust = db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not cust:
            raise LookupError("Kunde nicht gefunden")
        if (cust["orders_count"] + 1) % int(get_settings(db)["loyalty_every"]) == 0:
            cheapest = min(real_lines, key=lambda l: l["price_cents"])
            free_item = cheapest["name"]
            lines.append({"product_id": None, "name": f"Gratis: {free_item}", "qty": 1,
                          "price_cents": -cheapest["price_cents"],
                          "tax_rate": cheapest["tax_rate"]})

    total = sum(l["qty"] * l["price_cents"] for l in lines)
    counter = db.execute("SELECT COALESCE(MAX(tse_counter),0)+1 FROM orders").fetchone()[0]
    signature = tse_sign(counter, created_at, total)
    cur = db.execute(
        "INSERT INTO orders(location_id,customer_id,total_cents,payment_method,"
        "tse_counter,tse_signature,created_at,table_id) VALUES(?,?,?,?,?,?,?,?)",
        (location_id, customer_id, total, payment_method, counter, signature, created_at, table_id))
    oid = cur.lastrowid
    pno = db.execute("SELECT COALESCE(MAX(pickup_no),0)+1 FROM orders WHERE substr(created_at,1,10)=?",
                     (created_at[:10],)).fetchone()[0]
    ptoken = secrets.token_urlsafe(9)
    db.execute("UPDATE orders SET status='neu', pickup_no=?, pickup_token=? WHERE id=?", (pno, ptoken, oid))
    for l in lines:
        db.execute("INSERT INTO order_items(order_id,product_id,name,qty,price_cents,tax_rate)"
                   " VALUES(?,?,?,?,?,?)",
                   (oid, l["product_id"], l["name"], l["qty"], l["price_cents"], l["tax_rate"]))
    # Lagerbestand abziehen (nach Rezept)
    for l in real_lines:
        for r in db.execute("SELECT * FROM recipes WHERE product_id=?", (l["product_id"],)):
            db.execute("UPDATE ingredients SET stock = stock - ? WHERE id=?",
                       (r["qty"] * l["qty"], r["ingredient_id"]))
            consume_batches(db, r["ingredient_id"], r["qty"] * l["qty"])
    points = 0
    if cust:
        points = max(total, 0) // 100 * int(get_settings(db)["points_per_euro"])
        db.execute("UPDATE customers SET orders_count=orders_count+1, points=points+? WHERE id=?",
                   (points, customer_id))
    db.commit()
    return {"order_id": oid, "total_cents": total, "tse_counter": counter,
            "tse_signature": signature, "free_item": free_item, "points_earned": points,
            "pickup_no": pno, "pickup_token": ptoken,
            "customer_name": cust["name"] if cust else None}


def receipt_text(db, order_id):
    o = db.execute("SELECT o.*, l.name loc FROM orders o JOIN locations l ON l.id=o.location_id"
                   " WHERE o.id=?", (order_id,)).fetchone()
    if not o:
        raise LookupError("Bestellung nicht gefunden")
    items = rows(db, "SELECT * FROM order_items WHERE order_id=?", (order_id,))
    W = 42

    def two(left, right):
        return left[:W - len(right) - 1].ljust(W - len(right)) + right

    cfg = get_settings(db)
    tr = db.execute("SELECT name FROM tables WHERE id=?", (o["table_id"],)).fetchone()
    out = [cfg["name"].center(W)] + [x.center(W) for x in (cfg["address"], cfg["phone"],
           f"Steuernr.: {cfg['tax_id']}" if cfg["tax_id"] else "") if x] + [o["loc"].center(W), "-" * W]
    taxes = {}
    for it in items:
        gross = it["qty"] * it["price_cents"]
        out.append(two(f'{it["qty"]}x {it["name"]}', eur(gross)))
        taxes[it["tax_rate"]] = taxes.get(it["tax_rate"], 0) + gross
    out += ["-" * W, two("GESAMT EUR", eur(o["total_cents"])), ""]
    for rate, gross in sorted(taxes.items()):
        if gross == 0:
            continue
        net = gross / (1 + rate / 100)
        out.append(two(f"MwSt {rate:g}%: netto {eur(round(net))}", eur(round(gross - net))))
    out += ["", f"Zahlung: {o['payment_method']}", f"Beleg-Nr: {o['id']}", *([f"Abholnummer: {o['pickup_no']}"] if o["pickup_no"] else []), *([f"Tisch: {tr['name']}"] if tr else []),
            f"Datum: {o['created_at']}", f"TSE-Zaehler: {o['tse_counter']}",
            f"TSE-Signatur: {o['tse_signature']}", "(MOCK-TSE, nicht rechtsgueltig)"]
    if cfg["footer"]:
        out += ["", cfg["footer"].center(W)]
    if o["customer_id"]:
        c = db.execute("SELECT * FROM customers WHERE id=?", (o["customer_id"],)).fetchone()
        out += ["-" * W, f"Kunde: {c['name']}", f"Punkte gesamt: {c['points']}",
                f"Bestellungen bis Gratis-Artikel: "
                f"{int(cfg['loyalty_every']) - (c['orders_count'] % int(cfg['loyalty_every']))}"]
    return "\n".join(out)


# ----------------------------------------------------------------- Berichte
def _since(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def sales_report(db, days=7):
    since = _since(days)
    per_day = rows(db, "SELECT substr(created_at,1,10) day, COUNT(*) orders,"
                       " SUM(total_cents) revenue_cents FROM orders WHERE created_at>=?"
                       " GROUP BY day ORDER BY day", (since,))
    top = rows(db, "SELECT name, SUM(qty) sold, SUM(qty*price_cents) revenue_cents"
                   " FROM order_items WHERE product_id IS NOT NULL AND order_id IN"
                   " (SELECT id FROM orders WHERE created_at>=?)"
                   " GROUP BY name ORDER BY sold DESC LIMIT 5", (since,))
    return {"days": days, "orders": sum(d["orders"] for d in per_day),
            "revenue_cents": sum(d["revenue_cents"] for d in per_day),
            "per_day": per_day, "top_products": top}


def stock_forecast(db, window_days=7):
    used = {r["ingredient_id"]: r["used"] for r in rows(
        db, "SELECT r.ingredient_id, SUM(oi.qty*r.qty) used FROM order_items oi"
            " JOIN orders o ON o.id=oi.order_id JOIN recipes r ON r.product_id=oi.product_id"
            " WHERE o.created_at>=? GROUP BY r.ingredient_id", (_since(window_days),))}
    result = []
    for ing in rows(db, "SELECT * FROM ingredients ORDER BY name"):
        avg = used.get(ing["id"], 0) / window_days
        left = ing["stock"] / avg if avg > 0 else None
        if left is None:
            status = "kein Verbrauch"
        elif left <= 0:
            status = "LEER / negativ: sofort bestellen"
        elif left <= 1:
            status = "KRITISCH: reicht nur noch ca. 1 Tag"
        elif left <= 2:
            status = "reicht noch ca. 2 Tage"
        else:
            status = f"reicht noch ca. {left:.1f} Tage"
        result.append({"ingredient": ing["name"], "unit": ing["unit"],
                       "stock": round(ing["stock"], 1), "min_stock": ing["min_stock"],
                       "avg_daily_use": round(avg, 1),
                       "days_left": None if left is None else round(left, 1), "status": status})
    return result


def purchase_suggestions(db, target_days=3):
    out = []
    for f in stock_forecast(db):
        need = f["avg_daily_use"] * target_days - f["stock"]
        if f["stock"] <= f["min_stock"]:
            need = max(need, f["min_stock"] * 2 - f["stock"])
        if need > 0:
            out.append({"ingredient": f["ingredient"], "order_qty": math.ceil(need),
                        "unit": f["unit"], "reason": f["status"]})
    return out


# ----------------------------------------------------------------- Personal
def generate_shifts(db, week_start, staff_per_day=2, start="10:00", end="18:00"):
    ws = date.fromisoformat(week_start)
    hrs = _t(end) - _t(start)
    emps = rows(db, "SELECT * FROM employees")
    if not emps:
        raise ValueError("Keine Mitarbeiter vorhanden")
    days = [ws + timedelta(i) for i in range(7)]
    db.execute("DELETE FROM shifts WHERE day>=? AND day<=?",
               (days[0].isoformat(), days[-1].isoformat()))
    worked = {e["id"]: 0.0 for e in emps}
    warnings = []
    for d in days:
        cands = []
        for e in emps:
            free = db.execute("SELECT 1 FROM availability WHERE employee_id=? AND weekday=?"
                              " AND avail_from<=? AND avail_to>=?",
                              (e["id"], d.weekday(), start, end)).fetchone()
            away = db.execute("SELECT 1 FROM absences WHERE employee_id=? AND start_day<=? AND end_day>=?",
                              (e["id"], d.isoformat(), d.isoformat())).fetchone()
            if free and not away and worked[e["id"]] + hrs <= e["max_hours_week"]:
                cands.append(e)
        wish = {r["employee_id"]: r["pref"] for r in db.execute(
            "SELECT employee_id, pref FROM wishes WHERE weekday=?", (d.weekday(),))}
        # Wunschtag zuerst, "lieber nicht" nur wenn sonst niemand frei ist, dann wer am wenigsten hat
        cands.sort(key=lambda e: (wish.get(e["id"]) == "avoid", wish.get(e["id"]) != "want", worked[e["id"]]))
        chosen = cands[:staff_per_day]
        for e in chosen:
            db.execute("INSERT INTO shifts(employee_id,day,shift_start,shift_end) VALUES(?,?,?,?)",
                       (e["id"], d.isoformat(), start, end))
            worked[e["id"]] += hrs
        if len(chosen) < staff_per_day:
            warnings.append(f"{d.isoformat()}: nur {len(chosen)} von {staff_per_day} besetzt")
    db.commit()
    return get_shifts(db, week_start, warnings)


def get_shifts(db, week_start, warnings=None):
    ws = date.fromisoformat(week_start)
    we = (ws + timedelta(6)).isoformat()
    shifts = rows(db, "SELECT s.day, e.name, s.shift_start, s.shift_end FROM shifts s"
                      " JOIN employees e ON e.id=s.employee_id WHERE s.day>=? AND s.day<=?"
                      " ORDER BY s.day, e.name", (ws.isoformat(), we))
    hours = {}
    for s in shifts:
        hours[s["name"]] = hours.get(s["name"], 0) + _t(s["shift_end"]) - _t(s["shift_start"])
    return {"week_start": week_start, "shifts": shifts,
            "hours_per_employee": hours, "warnings": warnings or []}


# ----------------------------------------------------------------- Kundenplattform und Apple Wallet
REG = {}


class WalletUnavailable(Exception):
    pass


def _new_code():
    return "KK-" + secrets.token_hex(4).upper()


def _clean_person(name, email, phone):
    name, email, phone = (name or "").strip(), (email or "").strip().lower(), (phone or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Bitte gib deinen Namen ein.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 120:
        raise ValueError("Bitte gib eine gültige E-Mail-Adresse ein.")
    if phone and not re.fullmatch(r"[0-9+()/\s-]{5,30}", phone):
        raise ValueError("Die Handynummer sieht nicht richtig aus.")
    return name, email, phone


def register_customer(db, name, email, phone, consent, ip="?"):
    now = time.time()
    hits = [t for t in REG.get(ip, []) if now - t < 3600]
    if len(hits) >= 5:
        raise ValueError("Zu viele Anmeldungen von diesem Gerät. Bitte versuche es später noch einmal.")
    REG[ip] = hits + [now]
    if consent is not True:
        raise ValueError("Bitte stimme der Speicherung deiner Daten zu.")
    name, email, phone = _clean_person(name, email, phone)
    if db.execute("SELECT 1 FROM customers WHERE lower(email)=?", (email,)).fetchone():
        raise ValueError("Für diese E-Mail-Adresse gibt es schon eine Karte. Frag im Lokal nach deinem Link.")
    tok = secrets.token_urlsafe(18)
    code = _new_code()
    db.execute("INSERT INTO customers(name,email,phone,card_code,view_token,consent_at) VALUES(?,?,?,?,?,?)",
               (name, email, phone, code, tok, datetime.now().isoformat(timespec="seconds")))
    db.commit()
    return {"token": tok, "card_code": code}


def _card_row(db, token):
    c = db.execute("SELECT * FROM customers WHERE view_token=?", (token,)).fetchone() if token else None
    if not c:
        raise LookupError("Karte nicht gefunden")
    return c


def public_card(db, token):
    c = _card_row(db, token)
    cfg = get_settings(db)
    every = int(cfg["loyalty_every"])
    return {"name": c["name"], "email": c["email"], "phone": c["phone"] or "", "card_code": c["card_code"],
            "points": c["points"], "until_free": every - (c["orders_count"] % every),
            "business": cfg["name"], "apple": apple_ready()}


def update_card(db, token, body):
    c = _card_row(db, token)
    name, email, phone = _clean_person(body.get("name"), body.get("email"), body.get("phone"))
    if db.execute("SELECT 1 FROM customers WHERE lower(email)=? AND id!=?", (email, c["id"])).fetchone():
        raise ValueError("Diese E-Mail-Adresse ist schon vergeben.")
    db.execute("UPDATE customers SET name=?, email=?, phone=? WHERE id=?", (name, email, phone, c["id"]))
    db.commit()
    return public_card(db, token)


def delete_card(db, token):
    c = _card_row(db, token)
    db.execute("UPDATE orders SET customer_id=NULL WHERE customer_id=?", (c["id"],))
    db.execute("DELETE FROM customers WHERE id=?", (c["id"],))
    db.commit()
    return {"deleted": True}


def card_orders(db, token, limit=20):
    """Bestellungen einer Kundenkarte (neueste zuerst) fuer die Kundenseite."""
    c = _card_row(db, token)
    out = []
    for o in db.execute("SELECT * FROM orders WHERE customer_id=? ORDER BY id DESC LIMIT ?", (c["id"], limit)).fetchall():
        status = o["status"] or "abgeholt"
        e = {"pickup_no": o["pickup_no"], "status": status, "created_at": o["created_at"],
             "total_cents": o["total_cents"],
             "items": [{"name": i["name"], "qty": i["qty"], "free": i["product_id"] is None}
                       for i in db.execute("SELECT name, qty, product_id FROM order_items WHERE order_id=?", (o["id"],))]}
        if status in ("neu", "in_arbeit"):
            e["ahead"] = db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('neu','in_arbeit')"
                                    " AND substr(created_at,1,10)=? AND id<?", (o["created_at"][:10], o["id"])).fetchone()[0]
        out.append(e)
    return out


def customer_by_code(db, code):
    r = rows(db, "SELECT id, name, points, orders_count, card_code FROM customers WHERE card_code=?", (code.upper(),))
    if not r:
        raise LookupError("Diese Karte kenne ich nicht.")
    return r[0]


# Code 128 (Zeichensatz B) als SVG, fuer die Kundenseite
C128 = ("212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 "
        "113222 123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
        "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 "
        "113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
        "314111 221411 431111 111224 111422 121124 121421 142121 141122 141221 112214 112412 122114 122411 142112 "
        "142211 241211 221114 413111 241112 134111 111242 121142 121241 114212 124112 124211 411212 421112 421211 "
        "212141 214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 114131 311141 411131 "
        "211412 211214 211232").split()
C128_STOP = "2331112"


def code128_svg(text):
    vals = [104]
    for ch in text:
        if not 32 <= ord(ch) <= 126:
            raise ValueError("Zeichen nicht darstellbar")
        vals.append(ord(ch) - 32)
    vals.append((104 + sum(v * i for i, v in enumerate(vals[1:], 1))) % 103)
    pat = "".join(C128[v] for v in vals) + C128_STOP
    x, bars = 10, []
    for i, w in enumerate(pat):
        if i % 2 == 0:
            bars.append(f'<rect x="{x}" y="0" width="{w}" height="70"/>')
        x += int(w)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {x + 10} 70" preserveAspectRatio="none">'
            f'<rect width="{x + 10}" height="70" fill="#fff"/><g fill="#000">{"".join(bars)}</g></svg>')


def _png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _wallet_env():
    keys = {"type_id": "CHEF_PASS_TYPE_ID", "team": "CHEF_TEAM_ID", "cert": "CHEF_PASS_CERT",
            "key": "CHEF_PASS_KEY", "wwdr": "CHEF_PASS_WWDR"}
    env = {k: os.getenv(v, "") for k, v in keys.items()}
    if not all(env.values()) or not all(os.path.isfile(env[k]) for k in ("cert", "key", "wwdr")):
        return None
    return env


def apple_ready():
    return _wallet_env() is not None


def build_pkpass(db, token):
    env = _wallet_env()
    if not env:
        raise WalletUnavailable("Apple Wallet ist in diesem Lokal noch nicht eingerichtet.")
    c = _card_row(db, token)
    cfg = get_settings(db)
    back = [{"key": "card", "label": "Kartennummer", "value": c["card_code"]},
            {"key": "info", "label": "So sammelst du Punkte",
             "value": f"Zeige die Karte beim Bezahlen. Jede {cfg['loyalty_every']}. Bestellung gibt es einen Artikel gratis."}]
    if os.getenv("CHEF_PUBLIC_URL"):
        back.append({"key": "link", "label": "Meine Punkte und Daten",
                     "value": f"{os.getenv('CHEF_PUBLIC_URL').rstrip('/')}/kunde#{token}"})
    pass_json = {
        "formatVersion": 1, "passTypeIdentifier": env["type_id"], "teamIdentifier": env["team"],
        "serialNumber": c["card_code"], "organizationName": cfg["name"], "description": f"Kundenkarte {cfg['name']}",
        "logoText": cfg["name"], "foregroundColor": "rgb(255,255,255)", "backgroundColor": "rgb(14,124,102)",
        "labelColor": "rgb(205,236,226)",
        "storeCard": {"primaryFields": [{"key": "name", "label": "KUNDE", "value": c["name"]}],
                      "secondaryFields": [{"key": "code", "label": "KARTE", "value": c["card_code"]}],
                      "backFields": back},
        "barcodes": [{"format": "PKBarcodeFormatCode128", "message": c["card_code"],
                      "messageEncoding": "iso-8859-1", "altText": c["card_code"]}],
        "barcode": {"format": "PKBarcodeFormatCode128", "message": c["card_code"], "messageEncoding": "iso-8859-1"}}
    files = {"pass.json": json.dumps(pass_json, ensure_ascii=False).encode(),
             "icon.png": _png(29, 29, (14, 124, 102)), "icon@2x.png": _png(58, 58, (14, 124, 102))}
    manifest = json.dumps({k: hashlib.sha1(v).hexdigest() for k, v in files.items()}).encode()
    with tempfile.TemporaryDirectory() as d:
        mp, sp = os.path.join(d, "manifest.json"), os.path.join(d, "signature")
        with open(mp, "wb") as f:
            f.write(manifest)
        cmd = ["openssl", "smime", "-binary", "-sign", "-certfile", env["wwdr"], "-signer", env["cert"],
               "-inkey", env["key"], "-in", mp, "-out", sp, "-outform", "DER"]
        if os.getenv("CHEF_PASS_KEY_PASSWORD"):
            cmd += ["-passin", "env:CHEF_PASS_KEY_PASSWORD"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise WalletUnavailable("Die Wallet-Karte konnte nicht signiert werden (openssl fehlt).")
        if r.returncode:
            print("Wallet-Signatur fehlgeschlagen:", r.stderr.decode()[:300], file=sys.stderr)
            raise WalletUnavailable("Die Wallet-Karte konnte nicht signiert werden. Prüfe Zertifikat und Schlüssel.")
        with open(sp, "rb") as f:
            sig = f.read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in {**files, "manifest.json": manifest, "signature": sig}.items():
            z.writestr(k, v)
    return buf.getvalue()


# ----------------------------------------------------------------- Kueche und Abholung
def kitchen_orders(db):
    now = datetime.now()
    out = rows(db, "SELECT o.id, o.pickup_no, o.status, o.created_at, o.ready_at, o.ring, t.name AS tisch"
                   " FROM orders o LEFT JOIN tables t ON t.id=o.table_id"
                   " WHERE o.status IN ('neu','in_arbeit','fertig') AND o.created_at>=? ORDER BY o.created_at",
               ((now - timedelta(hours=6)).isoformat(timespec="seconds"),))
    for o in out:
        o["items"] = rows(db, "SELECT name, qty FROM order_items WHERE order_id=? AND product_id IS NOT NULL", (o["id"],))
        o["minutes"] = int((now - datetime.fromisoformat(o["created_at"])).total_seconds() // 60)
    return out


def set_order_status(db, oid, status):
    if status not in ("neu", "in_arbeit", "fertig", "abgeholt"):
        raise ValueError("Ungueltiger Status")
    if db.execute("UPDATE orders SET status=?, ready_at=CASE WHEN ?='fertig' THEN ? ELSE ready_at END WHERE id=?",
                  (status, status, datetime.now().isoformat(timespec="seconds"), oid)).rowcount == 0:
        raise LookupError("Bestellung nicht gefunden")
    db.commit()
    return {"ok": True, "status": status}


def ring_order(db, oid):
    o = db.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise LookupError("Bestellung nicht gefunden")
    if o["status"] != "fertig":
        raise ValueError("Die Bestellung ist noch nicht fertig")
    db.execute("UPDATE orders SET ring=COALESCE(ring,0)+1 WHERE id=?", (oid,))
    db.commit()
    return {"ok": True}


def public_order(db, key):
    today = date.today().isoformat()
    if key.isdigit() and len(key) <= 4:
        o = db.execute("SELECT * FROM orders WHERE pickup_no=? AND substr(created_at,1,10)=? ORDER BY id DESC",
                       (int(key), today)).fetchone()
    else:
        o = db.execute("SELECT * FROM orders WHERE pickup_token=?", (key,)).fetchone()
    if not o or not o["pickup_no"]:
        raise LookupError("Bestellung nicht gefunden")
    return _order_view(db, o)


def my_active_order(db, token):
    """Aktive Bestellung (heute) der Kundenkarte, ohne Abholnummer."""
    c = _card_row(db, token)
    o = db.execute("SELECT * FROM orders WHERE customer_id=? AND substr(created_at,1,10)=?"
                   " AND status IN ('neu','in_arbeit','fertig') AND pickup_no IS NOT NULL ORDER BY id DESC",
                   (c["id"], date.today().isoformat())).fetchone()
    if not o:
        raise LookupError("Keine aktive Bestellung")
    return _order_view(db, o)


def _order_view(db, o):
    ahead = 0
    if o["status"] in ("neu", "in_arbeit"):
        ahead = db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('neu','in_arbeit')"
                           " AND substr(created_at,1,10)=? AND id<?", (o["created_at"][:10], o["id"])).fetchone()[0]
    return {"business": get_settings(db)["name"], "pickup_no": o["pickup_no"], "status": o["status"],
            "ring": o["ring"] or 0, "ahead": ahead}


# ----------------------------------------------------------------- Mitarbeiter-Portal
FAILS = {}
SERVICE_AREAS = {"Kasse": {"kasse"}, "Bedienung": {"kasse", "kueche"}, "Bar": {"kasse", "lager", "kueche"},
                 "Küche": {"lager", "kueche"}, "Reinigung": set()}
AREA_ROUTES = {
    "kasse": [("POST", r"/orders"), ("GET", r"/orders/\d+/receipt"),
              ("GET", r"/(products|categories|customers|tables|customers/code/[A-Za-z0-9-]+)")],
    "kueche": [("GET", r"/kitchen/orders"), ("POST", r"/kitchen/orders/\d+/(status|ring)")],
    "lager": [("GET", r"/(ingredients|purchasing/suggestions|stock/(forecast|batches|mhd|check|log))"),
              ("POST", r"/stock/(receive|take|use)")],
}


def _hash(pin, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"pbkdf2${salt}${hashlib.pbkdf2_hmac('sha256', str(pin).encode(), salt.encode(), 60000).hex()}"


def _verify(pin, stored):
    if stored.startswith("pbkdf2$"):
        return hmac.compare_digest(_hash(pin, stored.split("$")[1]), stored)
    return hmac.compare_digest(stored, hashlib.sha256(("chef:" + str(pin)).encode()).hexdigest())


def admin_ok(db, pin):
    r = db.execute("SELECT hash FROM admin_auth WHERE id=1").fetchone()
    if r:
        return _verify(pin, r["hash"])
    return _tenant() is None and hmac.compare_digest(str(pin), ADMIN_PIN)  # Mandanten brauchen die Einrichtung


def setup_admin(db, body):
    slug = _tenant()
    if not slug:
        raise ValueError("Die Einrichtung gibt es nur für einen Betrieb")
    pin = str(body.get("pin", "")).strip()
    if not pin.isdigit() or not 6 <= len(pin) <= 10:
        raise ValueError("Die Admin-PIN braucht 6 bis 10 Ziffern")
    licensing.check_setup_token(slug, str(body.get("token", "")))
    db.execute("INSERT OR REPLACE INTO admin_auth(id,hash) VALUES(1,?)", (_hash(pin),))
    db.commit()
    licensing.mark_setup_used(slug)
    return {"ok": True}


def export_orders_csv(db):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Beleg", "Datum", "Summe_EUR", "Zahlung", "TSE_Zaehler", "TSE_Signatur"])
    for r in db.execute("SELECT id, created_at, total_cents, payment_method, tse_counter, tse_signature FROM orders ORDER BY id"):
        w.writerow([r["id"], r["created_at"], f"{r['total_cents'] / 100:.2f}", r["payment_method"], r["tse_counter"], r["tse_signature"]])
    return out.getvalue()


def login(db, number, pin):
    number, pin = str(number).strip(), str(pin).strip()
    key = number.lower()
    fkey = f"{_tenant()}|{key}"
    n, until = FAILS.get(fkey, (0, 0))
    if until > time.time():
        raise AuthError("Zu viele Versuche. Bitte warte kurz.")
    if key == "admin":
        ok, role, eid, name = admin_ok(db, pin), "admin", None, "Administrator"
    else:
        e = db.execute("SELECT * FROM employees WHERE number=?", (number,)).fetchone()
        ok = bool(e and e["pin_hash"] and _verify(pin, e["pin_hash"]))
        role, eid, name = "emp", e["id"] if e else None, e["name"] if e else ""
    if not ok:
        FAILS[fkey] = (n + 1, time.time() + 30 if n + 1 >= 5 else 0)
        raise AuthError("Nummer oder PIN falsch")
    FAILS.pop(fkey, None)
    tok = secrets.token_hex(16)
    TOKENS[(_tenant(), tok)] = (role, eid, time.time() + 12 * 3600)
    return {"token": tok, "role": role, "name": name}


def who(auth):
    tok = (auth or "").replace("Bearer ", "").strip()
    t = TOKENS.get((_tenant(), tok))
    if not t or t[2] < time.time():
        TOKENS.pop((_tenant(), tok), None)
        raise AuthError("Bitte zuerst anmelden")
    return t[0], t[1]


def areas_for(db, eid):
    e = db.execute("SELECT services FROM employees WHERE id=?", (eid,)).fetchone()
    out = set()
    for sv in ((e["services"] or "").split(",") if e else []):
        out |= SERVICE_AREAS.get(sv, set())
    return out


def authorize(db, role, me, method, path):
    if role == "admin":
        return
    for area in areas_for(db, me):
        for m, pat in AREA_ROUTES.get(area, []):
            if m == method and re.fullmatch(pat, path):
                return
    raise PermissionError("Dafür hast du keine Berechtigung.")


def employee_view(db, eid):
    e = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not e:
        raise LookupError("Mitarbeiter nicht gefunden")
    monday = date.today() - timedelta(days=date.today().weekday())
    shifts = rows(db, "SELECT day, shift_start, shift_end FROM shifts WHERE employee_id=? AND day>=?"
                      " ORDER BY day LIMIT 14", (eid, monday.isoformat()))
    week_end = (monday + timedelta(7)).isoformat()
    hours = sum(_t(x["shift_end"]) - _t(x["shift_start"]) for x in shifts if x["day"] < week_end)
    return {"id": e["id"], "name": e["name"], "number": e["number"] or "", "role": e["role"] or "",
            "services": [x for x in (e["services"] or "").split(",") if x],
            "max_hours_week": e["max_hours_week"], "hours_this_week": hours, "shifts": shifts,
            "wishes": rows(db, "SELECT weekday, pref FROM wishes WHERE employee_id=? ORDER BY weekday", (eid,)),
            "note": e["note"] or ""}


def save_wishes(db, eid, wishes, note=None):
    for w in wishes:
        if not 0 <= int(w["weekday"]) <= 6 or w["pref"] not in ("want", "avoid", "none"):
            raise ValueError("Ungueltiger Wunsch")
    db.execute("DELETE FROM wishes WHERE employee_id=?", (eid,))
    for w in wishes:
        if w["pref"] != "none":
            db.execute("INSERT INTO wishes(employee_id,weekday,pref) VALUES(?,?,?)",
                       (eid, int(w["weekday"]), w["pref"]))
    if note is not None:
        db.execute("UPDATE employees SET note=? WHERE id=?", (str(note)[:300], eid))
    db.commit()


def admin_upsert(db, eid, d):
    num = str(d["number"]).strip() if "number" in d else None
    if num is not None:
        if not num or num.lower() == "admin":
            raise ValueError("Ungueltige Mitarbeiternummer")
        if db.execute("SELECT 1 FROM employees WHERE number=? AND id IS NOT ?", (num, eid)).fetchone():
            raise ValueError("Diese Nummer ist schon vergeben")
    if d.get("pin") and len(str(d["pin"])) < 4:
        raise ValueError("Die PIN braucht mindestens 4 Stellen")
    if eid is None:
        if not str(d.get("name", "")).strip():
            raise ValueError("Name fehlt")
        if not num or not d.get("pin"):
            raise ValueError("Nummer und PIN sind noetig")
        eid = db.execute("INSERT INTO employees(name,role,max_hours_week) VALUES(?,?,?)",
                         (d["name"].strip(), d.get("role", "Service"), d.get("max_hours_week", 30))).lastrowid
    elif not db.execute("SELECT 1 FROM employees WHERE id=?", (eid,)).fetchone():
        raise LookupError("Mitarbeiter nicht gefunden")
    sets = {k: d[k] for k in ("name", "role", "max_hours_week", "note") if k in d}
    if num is not None:
        sets["number"] = num
    if "services" in d:
        sets["services"] = ",".join(x for x in d["services"] if x in SERVICES)
    if d.get("pin"):
        sets["pin_hash"] = _hash(d["pin"])
    if sets:
        db.execute(f"UPDATE employees SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), eid))
    db.commit()
    if "wishes" in d:
        save_wishes(db, eid, d["wishes"])
    return employee_view(db, eid)


# ----------------------------------------------------------------- AI-Agent
def _llm(question, context):
    """Optional: echtes Claude, wenn ANTHROPIC_API_KEY gesetzt ist."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import urllib.request
    body = json.dumps({
        "model": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"), "max_tokens": 600,
        "system": "Du bist der Assistent eines Restaurant-Kassensystems. "
                  "Antworte kurz und nur auf Basis der gelieferten Daten.",
        "messages": [{"role": "user", "content":
                      f"Daten:\n{json.dumps(context, ensure_ascii=False)}\n\nFrage: {question}"}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", body, {
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return "".join(b.get("text", "") for b in json.load(r)["content"])
    except Exception:
        return None


def _de(d):
    return date.fromisoformat(d).strftime("%d.%m.%Y")


def _hr_text(h, what):
    out = []
    if what in ("hours", "all"):
        out.append(f"{h['name']} hat diese Woche {h['hours_week']:g} von max. {h['max_hours_week']:g} Stunden "
                   f"und in diesem Monat {h['hours_month']:g} Stunden (laut Schichtplan).")
    if what in ("vac", "all"):
        out.append(f"Urlaub {h['year']}: {h['vacation_used']} von {h['vacation_total']} Tagen genommen, "
                   f"{h['vacation_left']} übrig." + (f" Krank: {h['sick_days_year']} Tage." if what == "all" else ""))
    if what == "all" and h["upcoming"]:
        out.append("Geplant: " + ", ".join(f"{a['kind']} {_de(a['start_day'])} bis {_de(a['end_day'])}" for a in h["upcoming"]) + ".")
    return " ".join(out)


def _product_flow(db, q, st):
    q = q.strip()
    if re.search(r"\b(abbrechen|cancel|stop)\b", q, re.I):
        return {"answer": "Okay, ich habe das Anlegen abgebrochen.", "options": HELP_OPTIONS}
    step = st.get("step", "name")
    price = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur|euro)?\s*$", q, re.I)
    if step == "name":
        name = q[:price.start()].strip() if price else q
        if not name:
            return {"answer": "Wie soll das Produkt heißen?", "state": st}
        st["name"] = name
        if price:
            st["price"] = round(float(price.group(1).replace(",", ".")) * 100)
    elif step == "price":
        if not price:
            return {"answer": "Bitte gib den Preis als Zahl an, zum Beispiel 8,50.", "state": st}
        st["price"] = round(float(price.group(1).replace(",", ".")) * 100)
    elif step == "cat":
        cats = rows(db, "SELECT id, name FROM categories")
        hit = next((c for c in cats if c["name"].lower() == q.lower()), None)
        if not hit:
            return {"answer": "Diese Kategorie kenne ich nicht. Bitte wähle eine aus.", "state": st,
                    "options": [c["name"] for c in cats]}
        st["cat"] = hit["id"]
    elif step == "tax":
        t = re.search(r"(7|19)", q)
        if not t:
            return {"answer": "Welcher MwSt-Satz gilt: 7 % oder 19 %?", "state": st, "options": ["7 %", "19 %"]}
        p = add_product(db, st["name"], st["price"], st["cat"], int(t.group(1)))
        return {"answer": f"Fertig: {p['name']} für {eur(p['price_cents'])} EUR ist angelegt und sofort an der Kasse verfügbar.",
                "options": HELP_OPTIONS}
    if "price" not in st:
        st["step"] = "price"
        return {"answer": f"Was kostet {st['name']}?", "state": st}
    if "cat" not in st:
        st["step"] = "cat"
        return {"answer": "In welche Kategorie soll das Produkt?", "state": st,
                "options": [c["name"] for c in rows(db, "SELECT name FROM categories")]}
    st["step"] = "tax"
    return {"answer": "Welcher MwSt-Satz gilt?", "state": st, "options": ["7 %", "19 %"]}


HELP_OPTIONS = ["Umsatz diese Woche", "Was muss ich bestellen?", "Neues Produkt anlegen", "Offene Rechnungen", "Neues Gespräch"]


def ask_agent(db, question, state=None):
    q = question.lower()
    if re.search(r"neue[sn]?\s+(diskussion|gespr|chat)|new\s+chat|nouvelle\s+discussion|diskussion\s+jdida|discussion\s+jdida", q):
        return {"answer": "Neues Gespräch gestartet. Wobei kann ich helfen?", "reset": True, "options": HELP_OPTIONS[:3]}
    if state and state.get("flow") == "product":
        return _product_flow(db, question, {**state})
    if "produkt" in q or "produit" in q:
        if re.search(r"neu|hinzu|anleg|erstell|add|zid|zad|jdid", q):
            body = re.sub(r"(?i)\b(ein\s+)?(neues?|neu|produkte?|produit|anlegen|hinzufügen|hinzufuegen|erstellen|add|zid|zad|jdid|jdida)\b", " ", question)
            body = re.sub(r"\s+", " ", body).strip(" :,-")
            return _product_flow(db, body, {"flow": "product", "step": "name"}) if body else \
                {"answer": "Wie soll das neue Produkt heißen? (Du kannst auch gleich den Preis dazuschreiben, z. B. „Pizza 8,50“.)",
                 "state": {"flow": "product", "step": "name"}}
    acc = accounting.agent_answer(db, question)
    if acc:
        acc.setdefault("options", HELP_OPTIONS)
        return acc
    emp = next((e for e in rows(db, "SELECT id, name FROM employees") if e["name"].lower() in q), None)
    if emp:
        what = ("all" if re.search(r"\bhr\b|alles|alle|ga3|kolchi|personal", q) else
                "vac" if re.search(r"urlaub|ijaza|conge|congé|vacation", q) else
                "hours" if re.search(r"stunden|sa3a|sa3at|hours|heures|arbeit", q) else None)
        if what:
            return {"answer": _hr_text(hr_summary(db, emp["id"]), what),
                    "options": [f"Urlaub von {emp['name']}" if what == "hours" else f"Stunden von {emp['name']}",
                                f"Alle HR-Daten von {emp['name']}"] if what != "all" else HELP_OPTIONS}
        n = emp["name"]
        return {"answer": f"Was möchtest du über {n} wissen?",
                "options": [f"Stunden von {n}", f"Urlaub von {n}", f"Alle HR-Daten von {n}"]}
    r = _ask_basic(db, question)
    r.setdefault("options", HELP_OPTIONS)
    return r


def _ask_basic(db, question):
    sales = sales_report(db, 7)
    sugg = purchase_suggestions(db)
    monday = date.today() - timedelta(days=date.today().weekday())
    shifts = get_shifts(db, monday.isoformat())
    ctx = {"umsatz_7_tage": sales, "bestand": stock_forecast(db),
           "bestellvorschlaege": sugg, "schichten_diese_woche": shifts,
           "buchhaltung_monat": accounting.summary(db, date.today().strftime("%Y-%m"))}
    answer = _llm(question, ctx)
    if answer:
        return {"answer": answer, "source": "claude"}
    q = question.lower()
    if any(w in q for w in ["umsatz", "verkauf", "verkauft", "tba3", "tbi3", "sales", "revenue"]):
        top = ", ".join(f"{t['name']} ({t['sold']}x)" for t in sales["top_products"][:3])
        a = (f"Letzte 7 Tage: {sales['orders']} Bestellungen, "
             f"{eur(sales['revenue_cents'])} EUR Umsatz. Top: {top}.")
    elif any(w in q for w in ["bestell", "einkauf", "nchri", "chri", "kaufen", "bestand", "mzl"]):
        a = ("Du solltest bestellen: " + "; ".join(
            f"{s['order_qty']} {s['unit']} {s['ingredient']} ({s['reason']})" for s in sugg)
             if sugg else "Aktuell ist nichts zu bestellen.")
    elif any(w in q for w in ["schicht", "khdm", "dienst", "mitarbeiter", "stunden"]):
        a = "Stunden diese Woche: " + (", ".join(
            f"{n}: {h:g}h" for n, h in shifts["hours_per_employee"].items())
            or "noch kein Schichtplan (POST /shifts/generate).")
    else:
        a = "Frag mich z.B. nach Umsatz/Verkauf, Bestellung/Einkauf oder Schichten."
    return {"answer": a, "source": "regeln (ANTHROPIC_API_KEY setzen fuer Claude)"}


# ----------------------------------------------------------------- Demo-Daten
def seed_demo(db):
    if db.execute("SELECT 1 FROM products").fetchone():
        return {"seeded": False, "msg": "Demo-Daten existieren schon"}
    rnd = random.Random(42)
    cat = {n: add_category(db, n)["id"] for n in ["Burger", "Beilagen", "Getraenke"]}
    prod = {}
    for name, price, c, tax in [("Cheeseburger", 990, "Burger", 7), ("Hamburger", 890, "Burger", 7),
                                ("Pommes", 350, "Beilagen", 7), ("Cola", 300, "Getraenke", 19),
                                ("Kaffee", 280, "Getraenke", 19)]:
        prod[name] = add_product(db, name, price, cat[c], tax)["id"]
    ing = {}
    for name, unit, stock, mn in [("Broetchen", "Stk", 150, 15), ("Patty", "Stk", 135, 15),
                                  ("Kaese", "Scheibe", 110, 15), ("Kartoffeln", "g", 14000, 3000),
                                  ("Cola-Flasche", "Stk", 120, 20), ("Kaffeebohnen", "g", 1100, 300), ("Milch", "L", 30, 5)]:
        ing[name] = add_ingredient(db, name, unit, stock, mn)["id"]
    R = lambda **kw: [{"ingredient_id": ing[k], "qty": v} for k, v in kw.items()]
    set_recipe(db, prod["Cheeseburger"], R(Broetchen=1, Patty=1, Kaese=1))
    set_recipe(db, prod["Hamburger"], R(Broetchen=1, Patty=1))
    set_recipe(db, prod["Pommes"], R(Kartoffeln=250))
    set_recipe(db, prod["Cola"], [{"ingredient_id": ing["Cola-Flasche"], "qty": 1}])
    set_recipe(db, prod["Kaffee"], R(Kaffeebohnen=9, Milch=0.05))
    for name, role, mx, days in [("Amina", "Service", 30, range(0, 6)), ("Youssef", "Koch", 40, range(0, 7)),
                                 ("Lena", "Service", 20, [1, 3, 5, 6]), ("Mehdi", "Koch", 40, range(2, 7))]:
        eid = add_employee(db, name, role, mx)["id"]
        set_availability(db, eid, [{"weekday": d, "from": "09:00", "to": "20:00"} for d in days])
    for num, nm, sv, wl in [("1001", "Amina", "Kasse,Bedienung", [(5, "avoid")]), ("1002", "Youssef", "Küche", []),
                            ("1003", "Lena", "Bar,Bedienung", [(6, "want")]), ("1004", "Mehdi", "Küche,Kasse", [(2, "avoid")])]:
        db.execute("UPDATE employees SET number=?, pin_hash=?, services=? WHERE name=?", (num, _hash("1234"), sv, nm))
        save_wishes(db, db.execute("SELECT id FROM employees WHERE name=?", (nm,)).fetchone()["id"],
                    [{"weekday": d, "pref": p} for d, p in wl])
    add_customer(db, "Karim", "karim@example.com")
    eid = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM employees")}
    t0 = date.today()
    for nm, a, b, kind in [("Amina", -60, -49, "urlaub"), ("Mehdi", -10, -9, "krank"), ("Lena", 20, 26, "urlaub")]:
        add_absence(db, eid[nm], (t0 + timedelta(a)).isoformat(), (t0 + timedelta(b)).isoformat(), kind)
    for i, (x, y, seats, sh) in enumerate([(30, 30, 2, "round"), (170, 30, 2, "round"), (310, 30, 4, "square"),
                                           (30, 170, 4, "square"), (190, 170, 6, "square"),
                                           (380, 190, 4, "round")], 1):
        add_table(db, f"Tisch {i}", seats, x, y, sh)
    # 7 Tage Verkaufshistorie, damit Prognose und Bericht etwas zeigen
    names = list(prod)
    weights = [4, 3, 3, 3, 2]
    for back in range(7, 0, -1):
        for _ in range(rnd.randint(12, 20)):
            ts = (datetime.now() - timedelta(days=back)).replace(
                hour=rnd.randint(11, 21), minute=rnd.randint(0, 59), second=0, microsecond=0)
            picks = rnd.choices(names, weights, k=rnd.randint(1, 3))
            make_order(db, [{"product_id": prod[n], "qty": 1} for n in picks],
                       payment_method=rnd.choice(["card", "cash"]),
                       created_at=ts.isoformat(timespec="seconds"))
    db.execute("UPDATE orders SET status='abgeholt'")  # Demo-Historie ist abgeschlossen
    db.commit()
    seed_batches(db)
    accounting.seed_demo(db)
    return {"seeded": True, "products": prod}


# ----------------------------------------------------------------- HTTP-API
def dispatch(db, method, path, qs, body, auth="", ip=""):
    g = lambda k, d=None: qs.get(k, [d])[0]
    if method == "GET" and path in ("/portal", "/index2.html"):
        return ("html", _page(INDEX2))
    if path in ("/willkommen", "/willkommen.html") and method == "GET":
        return ("html", _page(WILLKOMMEN))
    if path in ("/buchhaltung", "/buchhaltung.html") and method == "GET":
        return ("html", _page(BUCHHALTUNG))
    if path == "/demo/info" and method == "GET" and DEMO:
        return {"hinweis": "Nur Testdaten. Keine echten Personen- oder Finanzdaten eingeben.",
                "logins": [{"nummer": "admin", "pin": ADMIN_PIN, "rolle": "Administrator (alles inkl. Buchhaltung)"}] +
                          [{"nummer": n, "pin": "1234", "rolle": r} for n, r in
                           (("1001", "Amina: Kasse, Bedienung"), ("1002", "Youssef: Küche"),
                            ("1003", "Lena: Bar, Bedienung"), ("1004", "Mehdi: Küche, Kasse"))],
                "seiten": ["/buchhaltung", "/portal", "/"]}
    if path == "/license/status" and method == "GET":
        return licensing.status_for(_tenant())
    if path == "/license/by-session" and method == "GET":
        if _tenant():
            raise LookupError("Nicht verfügbar")
        return licensing.by_session(g("id", ""))
    if path == "/setup/admin" and method == "POST":
        return setup_admin(db, body)
    if path in ("/bestellung", "/bestellung.html") and method == "GET":
        return ("html", _page(BESTELLUNG))
    if path in ("/kunde", "/kunde.html") and method == "GET":
        return ("html", _page(KUNDE))
    if path.startswith("/public/"):
        if path == "/public/info" and method == "GET":
            cfg = get_settings(db)
            return {"business": cfg["name"], "loyalty_every": int(cfg["loyalty_every"])}
        if path == "/public/register" and method == "POST":
            return register_customer(db, body.get("name"), body.get("email"), body.get("phone"),
                                     body.get("consent"), ip or "?")
        mm = re.fullmatch(r"/public/my-order/([A-Za-z0-9_-]{10,64})", path)
        if mm and method == "GET":
            return my_active_order(db, mm[1])
        mo = re.fullmatch(r"/public/order/([A-Za-z0-9_-]{1,64})", path)
        if mo and method == "GET":
            return public_order(db, mo[1])
        m = re.fullmatch(r"/public/card/([A-Za-z0-9_-]{10,64})(/.*)?", path)
        if m:
            tok, sub = m[1], m[2] or ""
            if sub == "" and method == "GET":
                return public_card(db, tok)
            if sub == "/orders" and method == "GET":
                return card_orders(db, tok)
            if sub == "/update" and method == "POST":
                return update_card(db, tok, body)
            if sub == "/delete" and method == "POST":
                return delete_card(db, tok)
            if sub == "/apple.pkpass" and method == "GET":
                return ("pkpass", build_pkpass(db, tok))
            if sub == "/barcode.svg" and method == "GET":
                return ("svg", code128_svg(_card_row(db, tok)["card_code"]))
        raise LookupError("Route nicht gefunden")
    if path == "/auth/login" and method == "POST":
        return login(db, body["number"], body["pin"])
    if path.startswith("/portal/"):
        role, me = who(auth)
        if path == "/portal/me" and method == "GET":
            if role == "admin":
                return {"role": "admin", "name": "Administrator", "services_all": SERVICES, "areas": ["kasse", "lager"]}
            return {**employee_view(db, me), "role": "emp", "services_all": SERVICES, "areas": sorted(areas_for(db, me))}
        if path == "/portal/wishes" and method == "POST":
            target = body["employee_id"] if role == "admin" else me
            save_wishes(db, target, body["wishes"], body.get("note"))
            return employee_view(db, target)
        if path == "/portal/payslips" and method == "GET" and role == "emp":
            return accounting.list_payslips(db, eid=me)
        mp = re.fullmatch(r"/portal/payslips/(\d+)", path)
        if mp and role == "emp" and method == "GET":
            return ("text", accounting.payslip_text(db, int(mp[1]), get_settings(db), eid=me))
        if role != "admin":
            raise PermissionError("Nur fuer Administratoren")
        if path == "/portal/admin/employees":
            if method == "POST":
                return admin_upsert(db, None, body)
            return [employee_view(db, r["id"]) for r in db.execute("SELECT id FROM employees ORDER BY name")]
        m = re.fullmatch(r"/portal/admin/employees/(\d+)", path)
        if m and method == "POST":
            return admin_upsert(db, int(m[1]), body)
    if method == "GET" and path in ("/", "/index.html"):
        return ("html", _page(INDEX))
    if method == "GET" and path == "/health":
        return {"ok": True}
    if path == "/auth/logout" and method == "POST":
        TOKENS.pop((_tenant(), (auth or "").replace("Bearer ", "").strip()), None)
        return {"ok": True}
    role, me = who(auth)  # ab hier ist alles geschuetzt
    authorize(db, role, me, method, path)
    emp = db.execute("SELECT name FROM employees WHERE id=?", (me,)).fetchone() if me else None
    actor = lambda b: emp["name"] if emp else (b.get("who") or "Administrator")
    if path == "/export/orders.csv" and method == "GET":
        return ("csv", export_orders_csv(db))
    if path == "/kitchen/orders" and method == "GET":
        return kitchen_orders(db)
    mk = re.fullmatch(r"/kitchen/orders/(\d+)/(status|ring)", path)
    if mk and method == "POST":
        return set_order_status(db, int(mk[1]), body.get("status", "")) if mk[2] == "status" else ring_order(db, int(mk[1]))
    if method == "POST" and path == "/demo/seed":
        return seed_demo(db)
    if path == "/categories":
        return add_category(db, body["name"]) if method == "POST" else rows(db, "SELECT * FROM categories")
    if path == "/products":
        if method == "POST":
            return add_product(db, body["name"], body["price_cents"],
                               body.get("category_id"), body.get("tax_rate", 19))
        return rows(db, "SELECT * FROM products WHERE active=1")
    m = re.fullmatch(r"/products/(\d+)", path)
    if m and method == "POST":
        return update_product(db, int(m[1]), **body)
    if path == "/ingredients":
        if method == "POST":
            return add_ingredient(db, body["name"], body["unit"], body.get("stock", 0),
                                  body.get("min_stock", 0))
        return rows(db, "SELECT * FROM ingredients")
    m = re.fullmatch(r"/recipes/(\d+)", path)
    if m and method == "POST":
        return set_recipe(db, int(m[1]), body["parts"])
    if path == "/absences" and method == "POST":
        return add_absence(db, int(body["employee_id"]), body["start"], body["end"],
                           body.get("kind", "urlaub"), body.get("note", ""))
    m = re.fullmatch(r"/employees/(\d+)/hr", path)
    if m and method == "GET":
        return hr_summary(db, int(m[1]))
    if path == "/stock/batches" and method == "GET":
        return list_batches(db, int(g("ingredient_id")) if g("ingredient_id") else None)
    if path == "/stock/mhd" and method == "GET":
        return mhd_overview(db, int(g("days", 3)))
    if path == "/stock/check" and method == "GET":
        return check_take(db, int(g("ingredient_id")))
    if path == "/stock/receive" and method == "POST":
        return receive_goods(db, int(body["ingredient_id"]), body["qty"], body.get("mhd"),
                             body.get("location", "Lager"), actor(body))
    if path == "/stock/take" and method == "POST":
        return take_goods(db, int(body["ingredient_id"]), int(body["batch_id"]), body["qty"],
                          body.get("to_location", "Kühlschrank"), actor(body), bool(body.get("force")))
    if path == "/stock/use" and method == "POST":
        return use_up(db, int(body["batch_id"]), body.get("qty"), body.get("reason", "verbraucht"), actor(body))
    if path == "/stock/log" and method == "GET":
        return rows(db, "SELECT l.ts, i.name ingredient, i.unit, l.action, l.qty, l.who, l.note FROM stock_log l"
                        " JOIN ingredients i ON i.id=l.ingredient_id ORDER BY l.id DESC LIMIT 20")
    if path == "/tables":
        return add_table(db, body.get("name"), body.get("seats", 4), body.get("x", 20),
                         body.get("y", 20), body.get("shape", "square")) if method == "POST" \
            else rows(db, "SELECT * FROM tables ORDER BY id")
    m = re.fullmatch(r"/tables/(\d+)(/delete)?", path)
    if m and method == "POST":
        return delete_table(db, int(m[1])) if m[2] else update_table(db, int(m[1]), **body)
    if path == "/settings":
        return save_settings(db, body) if method == "POST" else get_settings(db)
    if path == "/diagnose" and method == "GET":
        return diagnose(db)
    if path == "/customers" and method == "GET":
        return rows(db, "SELECT id, name, email, points, orders_count, card_code FROM customers ORDER BY name")
    if path == "/customers" and method == "POST":
        return add_customer(db, body["name"], body.get("email"))
    m = re.fullmatch(r"/customers/code/([A-Za-z0-9-]+)", path)
    if m and method == "GET":
        return customer_by_code(db, m[1])
    m = re.fullmatch(r"/customers/(\d+)", path)
    if m and method == "GET":
        r = rows(db, "SELECT id, name, email, points, orders_count, card_code FROM customers WHERE id=?", (int(m[1]),))
        if not r:
            raise LookupError("Kunde nicht gefunden")
        return r[0]
    if path == "/orders" and method == "POST":
        return make_order(db, body["items"], body.get("location_id", 1), body.get("customer_id"),
                          body.get("payment_method", "card"),
                          table_id=body.get("table_id"))
    m = re.fullmatch(r"/orders/(\d+)/receipt", path)
    if m and method == "GET":
        return ("text", receipt_text(db, int(m[1])))
    if path == "/reports/sales":
        return sales_report(db, int(g("days", 7)))
    if path == "/stock/forecast":
        return stock_forecast(db)
    if path == "/purchasing/suggestions":
        return purchase_suggestions(db, float(g("target_days", 3)))
    if path == "/employees" and method == "GET":
        return list_employees(db)
    if path == "/employees" and method == "POST":
        return add_employee(db, body["name"], body.get("role", "Service"), body.get("max_hours_week", 40))
    m = re.fullmatch(r"/employees/(\d+)/availability", path)
    if m and method == "POST":
        return set_availability(db, int(m[1]), body["slots"])
    if path == "/shifts/generate" and method == "POST":
        return generate_shifts(db, body["week_start"], body.get("staff_per_day", 2),
                               body.get("start", "10:00"), body.get("end", "18:00"))
    if path == "/shifts" and method == "GET":
        return get_shifts(db, g("week_start"))
    if path.startswith("/accounting/"):
        return accounting_routes(db, method, path, g, body)
    if path == "/agent/ask" and method == "POST":
        return ask_agent(db, body["question"], body.get("state"))
    raise LookupError("Route nicht gefunden")


def accounting_routes(db, method, path, g, body):
    """Buchhaltung (nur Administratoren, siehe authorize())."""
    p = path[len("/accounting"):]
    month = g("month") or date.today().strftime("%Y-%m")
    if p == "/summary" and method == "GET":
        return accounting.summary(db, month)
    if p == "/export.csv" and method == "GET":
        return ("csv", accounting.export_csv(db, month))
    m = re.fullmatch(r"/payroll/profile/(\d+)", p)
    if m:
        return accounting.set_profile(db, int(m[1]), body) if method == "POST" else accounting.get_profile(db, int(m[1]))
    if p == "/payroll/preview" and method == "POST":
        return accounting.preview(db, int(body["employee_id"]), body.get("period", month), body.get("hours"))
    if p == "/payroll/run" and method == "POST":
        return accounting.run_payroll(db, body.get("period", month), body.get("hours"))
    if p == "/payslips" and method == "GET":
        return accounting.list_payslips(db, g("period"))
    m = re.fullmatch(r"/payslips/(\d+)/text", p)
    if m and method == "GET":
        return ("text", accounting.payslip_text(db, int(m[1]), get_settings(db)))
    if p == "/invoices" and method == "GET":
        return accounting.list_invoices(db, g("kind"), g("status"))
    if p == "/invoices" and method == "POST":
        return accounting.create_invoice(db, body.get("kind", "out"), body.get("party", ""), body.get("items", []),
                                         body.get("address", ""), body.get("date"), body.get("due_days", 14),
                                         body.get("service_date"), body.get("note", ""), body.get("ext_number"),
                                         body.get("category"), bool(body.get("gross_prices")))
    m = re.fullmatch(r"/invoices/(\d+)/(print|status)", p)
    if m and m[2] == "print" and method == "GET":
        return ("html", accounting.invoice_html(db, int(m[1]), get_settings(db)))
    if m and m[2] == "status" and method == "POST":
        return accounting.set_invoice_status(db, int(m[1]), body.get("status", ""))
    raise LookupError("Route nicht gefunden")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _webhook(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        if not secret or not licensing.verify_stripe(raw, self.headers.get("Stripe-Signature", ""), secret):
            return self._send(400, {"error": "Ungültige Signatur"})
        try:
            return self._send(200, {"ok": True, "result": licensing.handle_event(json.loads(raw))})
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": str(e)})

    def _handle(self, method):
        url = urlparse(self.path)
        try:
            tenant = licensing.tenant_for_host(self.headers.get("Host", ""))
        except LookupError as e:
            return self._send(404, {"error": str(e)})
        _tl.tenant = tenant
        if url.path == "/stripe/webhook" and method == "POST" and not tenant:
            return self._webhook()
        if tenant:
            lic = licensing.get_license(tenant)
            if not lic:
                return self._send(404, {"error": "Unbekannter Betrieb"})
            if licensing.license_state(lic) == "expired" and not licensing.allowed_when_expired(method, url.path):
                return self._send(402, {"error": "Das Abo ist abgelaufen. Neue Vorgänge sind gesperrt, dein Datenexport bleibt verfügbar."})
        db = connect(licensing.db_path(tenant)) if tenant else connect()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            res, status = dispatch(db, method, url.path, parse_qs(url.query), body,
                            self.headers.get("Authorization", ""), self.client_address[0]), 200
        except WalletUnavailable as e:
            res, status = {"error": str(e)}, 503
        except Conflict as e:
            res, status = e.payload, 409
        except AuthError as e:
            res, status = {"error": str(e)}, 401
        except PermissionError as e:
            res, status = {"error": str(e)}, 403
        except LookupError as e:
            res, status = {"error": str(e)}, 404
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as e:
            res, status = {"error": f"{type(e).__name__}: {e}"}, 400
        finally:
            db.close()
        if isinstance(res, tuple):
            data = res[1] if isinstance(res[1], bytes) else res[1].encode()
            ctype = {"text": "text/plain", "html": "text/html", "svg": "image/svg+xml", "csv": "text/csv",
                     "pkpass": "application/vnd.apple.pkpass"}[res[0]] + ("" if res[0] in ("svg", "pkpass") else "; charset=utf-8")
        else:
            data, ctype = json.dumps(res, ensure_ascii=False, indent=2).encode(), "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


# ----------------------------------------------------------------- Demo-Lauf
def demo():
    db = connect(":memory:")

    def show(title, data):
        print(f"\n=== {title} ===")
        print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2))

    seed_demo(db)
    show("1) Umsatz der letzten 7 Tage", sales_report(db, 7))
    show("2) Lagerprognose", stock_forecast(db))
    show("3) Bestellvorschlaege", purchase_suggestions(db, 3))
    ids = {r["name"]: r["id"] for r in rows(db, "SELECT id,name FROM products")}
    print("\n=== 4) Treue-Test: Kunde Karim bestellt 5x ===")
    for i in range(1, 6):
        r = make_order(db, [{"product_id": ids["Cheeseburger"], "qty": 1},
                            {"product_id": ids["Cola"], "qty": 1}], customer_id=1)
        print(f"Bestellung {i}: {eur(r['total_cents'])} EUR, Punkte +{r['points_earned']}, "
              f"Gratis: {r['free_item']}")
    show("5) Beleg der letzten Bestellung (mit Gratis-Artikel)", receipt_text(db, r["order_id"]))
    monday = date.today() - timedelta(days=date.today().weekday()) + timedelta(7)
    show("6) Schichtplan naechste Woche (2 Personen/Tag, 10-18 Uhr)",
         generate_shifts(db, monday.isoformat(), 2))
    for q in ["Chhal tba3t had l'osbou3?", "Chno khassni nchri?", "Chkon khdam had l'osbou3, stunden?"]:
        show(f"7) Agent: {q}", ask_agent(db, q))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd == "demo":
        demo()
    elif cmd == "serve":
        port = int(os.getenv("PORT", 8000))
        host = os.getenv("CHEF_HOST", "127.0.0.1")  # 0.0.0.0 nur setzen, wenn andere Geraete zugreifen sollen
        if ADMIN_PIN == "0000" and host not in ("127.0.0.1", "localhost"):
            sys.exit("Abbruch: Setze zuerst CHEF_ADMIN_PIN (nicht 0000), bevor der Server im Netzwerk erreichbar ist.")
        if DEMO:
            _d = connect()
            print("Demo-Daten:", seed_demo(_d).get("seeded"))
            _d.close()
        print(f"Chef laeuft auf http://{host}:{port}  (DB: {DB_PATH})")
        if ADMIN_PIN == "0000":
            print("WARNUNG: Admin-PIN ist 0000. Setze CHEF_ADMIN_PIN, bevor du echte Daten nutzt.")
        print("Mitarbeiter-Portal: http://localhost:%d/portal" % port)
        print("Tipp: erst  curl -X POST localhost:%d/demo/seed  fuer Testdaten" % port)
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    elif cmd == "license":
        licensing.cli(sys.argv[2:])
    else:
        print(__doc__)
