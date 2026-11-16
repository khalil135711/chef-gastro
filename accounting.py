"""
CHEF Buchhaltung: Netto-Gehalt (Lohn), Rechnungen, Umsatzsteuer, Auswertung.
Nur Python-Standardbibliothek. Alle Geldbetraege in Cent (int).

WICHTIG: Die Lohnberechnung ist eine SCHAETZUNG (vereinfachte Lohnsteuer nach Tarif 2026,
nicht der amtliche Programmablaufplan). Fuer echte Abrechnungen, Meldungen und Steuer-
erklaerungen: Steuerberater / Lohnbuero / DATEV-Lohnsoftware nutzen.
Werte Stand 2026: SV-Saetze und BBG (Rentenversicherung 18,6 %, AV 2,6 %, KV 14,6 % + Zusatzbeitrag,
PV 3,6 %), Grundfreibetrag 12.348 EUR, Minijob 603 EUR, Mindestlohn 13,90 EUR.
"""
import csv
import html
import io
import json
import math
import re
from datetime import date, datetime, timedelta

# ----------------------------------------------------------------- Parameter 2026
BBG_RV, BBG_KV = 101400.0, 69750.0      # Jahresgrenzen (RV/AV, KV/PV)
RV, AV, KV_BASE, KV_RED, PV = 18.6, 2.6, 14.6, 14.0, 3.6
MINI, MIDI, MINDESTLOHN = 603.0, 2000.0, 13.90
WK, SA, GRUNDFREI = 1230.0, 36.0, 12348.0
KFB_FULL, ENTLASTUNG = 9756.0, 4260.0   # Kinderfreibetrag je Kind (beide Eltern), Alleinerziehende
SOLI_FREE = 20350.0
UMLAGEN_PCT = 2.5                       # Schaetzwert U1/U2/U3/Insolvenzgeld, kassenabhaengig
TAX_RATES = (0, 7, 19)

PROFILE_DEFAULTS = dict(type="regulaer", wage_type="stunde", hourly_cents=1390, monthly_cents=0,
                        tax_class=1, kfb=0.0, children=0, church=0, state="NW",
                        kv_zusatz=2.9, under_23=0, rv_free=0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS payroll_profiles(employee_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payslips(
  id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, period TEXT NOT NULL,
  brutto_cents INTEGER, netto_cents INTEGER, ag_cost_cents INTEGER, data TEXT, created_at TEXT,
  UNIQUE(employee_id, period));
CREATE TABLE IF NOT EXISTS invoices(
  id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, party TEXT NOT NULL,
  address TEXT, inv_date TEXT NOT NULL, due_date TEXT, service_date TEXT, status TEXT NOT NULL DEFAULT 'offen',
  net_cents INTEGER NOT NULL, tax_cents INTEGER NOT NULL, gross_cents INTEGER NOT NULL,
  paid_at TEXT, note TEXT, category TEXT, ext_number TEXT);
CREATE TABLE IF NOT EXISTS invoice_items(
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL, description TEXT NOT NULL, qty REAL NOT NULL,
  unit_cents INTEGER NOT NULL, tax_rate REAL NOT NULL, net_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_tax(invoice_id INTEGER, rate REAL, net_cents INTEGER, tax_cents INTEGER);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


def _r(x):
    return int(math.floor(x + 0.5))


def fmt(c):
    return f"{c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _month(m):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m or ""):
        raise ValueError("Monat im Format JJJJ-MM angeben, z. B. 2026-11")
    return m


def _bounds(month):
    y, mo = map(int, _month(month).split("-"))
    d1 = date(y, mo, 1)
    return d1.isoformat(), ((d1 + timedelta(32)).replace(day=1) - timedelta(1)).isoformat()


# ----------------------------------------------------------------- Lohn: Steuer
def _tarif(zve):
    """Einkommensteuer-Grundtarif 2026 (§ 32a EStG), ganze Euro."""
    x = math.floor(zve)
    if x <= 12348:
        return 0
    if x <= 17799:
        y = (x - 12348) / 10000
        return math.floor((914.51 * y + 1400) * y)
    if x <= 69878:
        z = (x - 17799) / 10000
        return math.floor((173.10 * z + 2397) * z + 1034.87)
    if x <= 277825:
        return math.floor(0.42 * x - 11135.63)
    return math.floor(0.45 * x - 19470.38)


def _est(zve, klasse):
    zve = max(0.0, math.floor(zve))
    if klasse == 3:                       # Splitting
        return 2 * _tarif(zve / 2)
    if klasse in (5, 6):                  # vereinfachte Annaeherung an den PAP
        return max(2 * (_tarif(zve * 1.25) - _tarif(zve * 0.75)), math.floor(zve * 0.14))
    return _tarif(zve)


def _pv_rates(p):
    """(AG-Satz, AN-Satz) der Pflegeversicherung in Prozent."""
    sn = p["state"] == "SN"
    ag = 1.3 if sn else 1.8
    an = 2.3 if sn else 1.8
    if int(p["children"]) == 0 and not int(p["under_23"]):
        an += 0.6
    elif int(p["children"]) >= 2:
        an -= 0.25 * min(int(p["children"]) - 1, 4)
    return ag, an


def calc_payroll(brutto_cents, profile):
    """Brutto -> Netto (Monat). Rueckgabe: Dict mit Cent-Betraegen und Warnungen."""
    p = {**PROFILE_DEFAULTS, **profile}
    B, warns = int(brutto_cents), []
    out = dict(brutto=B, rv=0, av=0, kv=0, pv=0, lohnsteuer=0, soli=0, kist=0, ag_sv=0, ag_pausch=0)
    if B <= 0:
        out.update(kind="kein Entgelt", netto=0, abzuege=0, ag_umlagen=0, ag_cost=0, warnings=["Kein Brutto im Monat"])
        return out
    z = float(p["kv_zusatz"])
    if p["type"] == "minijob":
        if B <= MINI * 100:
            out["rv"] = 0 if int(p["rv_free"]) else _r(B * (RV - 15) / 100)
            out["ag_pausch"] = _r(B * 0.30)            # 15 % RV + 13 % KV + 2 % Pauschsteuer
            out.update(kind="Minijob", lohnsteuer=0)
        else:
            warns.append(f"Minijob-Grenze ({MINI:.0f} EUR) ueberschritten: Berechnung als reguläre Beschäftigung (Midijob).")
    if "kind" not in out:
        ag_pv, an_pv = _pv_rates(p)
        ae = B
        if ae <= MIDI * 100 and ae > MINI * 100:       # Uebergangsbereich: reduzierte AN-Beitraege
            total = RV + AV + KV_BASE + z + PV
            F = 0.28 / (total / 100)
            G = MINI
            be = (F * G + (MIDI / (MIDI - G) - G / (MIDI - G) * F) * (ae / 100 - G)) * 100
            ag = dict(rv=RV / 2, av=AV / 2, kv=KV_BASE / 2 + z / 2, pv=ag_pv)
            tot = dict(rv=RV, av=AV, kv=KV_BASE + z, pv=ag_pv + an_pv)
            for k in ag:
                out[k] = max(0, _r(tot[k] / 100 * be - ag[k] / 100 * ae))
            out["kind"] = "Midijob (Übergangsbereich)"
        else:
            b_rv, b_kv = min(B, BBG_RV * 100 / 12), min(B, BBG_KV * 100 / 12)
            if B > BBG_RV * 100 / 12:
                warns.append("Brutto ueber Beitragsbemessungsgrenze (RV/AV).")
            out.update(rv=_r(b_rv * RV / 200), av=_r(b_rv * AV / 200),
                       kv=_r(b_kv * (KV_BASE / 2 + z / 2) / 100), pv=_r(b_kv * an_pv / 100), kind="Regulär")
        # Arbeitgeberseite
        b_rv, b_kv = min(B, BBG_RV * 100 / 12), min(B, BBG_KV * 100 / 12)
        out["ag_sv"] = _r(b_rv * (RV + AV) / 200 + b_kv * (KV_BASE / 2 + z / 2 + ag_pv) / 100)
        # Lohnsteuer (Jahresmethode, vereinfachte Vorsorgepauschale)
        kl = int(p["tax_class"])
        kv_basis = out["kv"] * (KV_RED / 2 + z / 2) / (KV_BASE / 2 + z / 2) if out["kv"] else 0
        vorsorge = 12 * (out["rv"] + out["av"] + kv_basis + out["pv"]) / 100
        wk, sa = (0.0, 0.0) if kl == 6 else (WK, SA)
        zve = B * 12 / 100 - wk - sa - vorsorge - (ENTLASTUNG if kl == 2 else 0)
        ls_y = _est(zve, kl)
        base = _est(zve - float(p["kfb"]) * KFB_FULL, kl) if kl not in (5, 6) else ls_y
        free = SOLI_FREE * (2 if kl == 3 else 1)
        soli_y = 0 if base <= free else min(0.055 * base, 0.119 * (base - free))
        kist_y = base * (0.08 if p["state"] in ("BW", "BY") else 0.09) if int(p["church"]) else 0
        out["lohnsteuer"], out["soli"], out["kist"] = _r(ls_y * 100 / 12), _r(soli_y * 100 / 12), _r(kist_y * 100 / 12)
    out["abzuege"] = out["rv"] + out["av"] + out["kv"] + out["pv"] + out["lohnsteuer"] + out["soli"] + out["kist"]
    out["netto"] = B - out["abzuege"]
    out["ag_umlagen"] = _r(B * UMLAGEN_PCT / 100)
    out["ag_cost"] = B + out["ag_sv"] + out["ag_pausch"] + out["ag_umlagen"]
    out["warnings"] = warns
    return out


# ----------------------------------------------------------------- Lohn: Profile und Abrechnung
def _emp(db, eid):
    e = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not e:
        raise LookupError("Mitarbeiter nicht gefunden")
    return e


def get_profile(db, eid):
    _emp(db, eid)
    r = db.execute("SELECT data FROM payroll_profiles WHERE employee_id=?", (eid,)).fetchone()
    return {**PROFILE_DEFAULTS, **(json.loads(r["data"]) if r else {}), "configured": bool(r)}


def set_profile(db, eid, d):
    _emp(db, eid)
    p = {**get_profile(db, eid), **{k: v for k, v in d.items() if k in PROFILE_DEFAULTS}}
    p.pop("configured", None)
    if p["type"] not in ("regulaer", "minijob") or p["wage_type"] not in ("stunde", "monat"):
        raise ValueError("type: regulaer|minijob, wage_type: stunde|monat")
    if int(p["tax_class"]) not in range(1, 7):
        raise ValueError("Steuerklasse 1 bis 6")
    if not re.fullmatch(r"[A-Z]{2}", str(p["state"])):
        raise ValueError("Bundesland als Kürzel, z. B. NW")
    for k in ("hourly_cents", "monthly_cents", "children"):
        if int(p[k]) < 0:
            raise ValueError(f"{k} darf nicht negativ sein")
    db.execute("INSERT OR REPLACE INTO payroll_profiles(employee_id,data) VALUES(?,?)", (eid, json.dumps(p)))
    db.commit()
    return get_profile(db, eid)


def _shift_hours(db, eid, period):
    h = 0.0
    for r in db.execute("SELECT shift_start, shift_end FROM shifts WHERE employee_id=? AND day LIKE ?", (eid, period + "-%")):
        a, b = (int(x) for x in r["shift_start"].split(":")), (int(x) for x in r["shift_end"].split(":"))
        a, b = list(a), list(b)
        h += (b[0] + b[1] / 60) - (a[0] + a[1] / 60)
    return h


def preview(db, eid, period, hours=None):
    _month(period)
    e, p = _emp(db, eid), get_profile(db, eid)
    warns, estimated = [], False
    if p["wage_type"] == "monat":
        hrs, brutto = None, int(p["monthly_cents"])
    else:
        hrs = float(hours) if hours not in (None, "") else _shift_hours(db, eid, period)
        if hours in (None, "") and hrs == 0:
            hrs, estimated = (e["max_hours_week"] or 0) * 52 / 12, True
            warns.append(f"Keine Schichten in {period}: Hochrechnung aus {e['max_hours_week']:g} Wochenstunden.")
        brutto = _r(hrs * int(p["hourly_cents"]))
        if int(p["hourly_cents"]) < MINDESTLOHN * 100:
            warns.append(f"Stundenlohn unter dem Mindestlohn ({MINDESTLOHN:.2f} EUR).")
    c = calc_payroll(brutto, p)
    return dict(employee_id=eid, name=e["name"], role=e["role"], period=period, hours=None if hrs is None else round(hrs, 2),
                estimated=estimated, brutto_cents=brutto, profile={k: p[k] for k in PROFILE_DEFAULTS}, calc=c,
                warnings=warns + c["warnings"])


def run_payroll(db, period, hours_map=None):
    out = []
    for r in db.execute("SELECT employee_id FROM payroll_profiles ORDER BY employee_id").fetchall():
        eid = r["employee_id"]
        pv = preview(db, eid, period, (hours_map or {}).get(str(eid)))
        db.execute("INSERT OR REPLACE INTO payslips(employee_id,period,brutto_cents,netto_cents,ag_cost_cents,data,created_at)"
                   " VALUES(?,?,?,?,?,?,?)", (eid, period, pv["brutto_cents"], pv["calc"]["netto"], pv["calc"]["ag_cost"],
                                              json.dumps(pv), datetime.now().isoformat(timespec="seconds")))
        out.append({"name": pv["name"], "brutto_cents": pv["brutto_cents"], "netto_cents": pv["calc"]["netto"]})
    db.commit()
    return {"period": period, "payslips": out, "netto_total_cents": sum(x["netto_cents"] for x in out),
            "brutto_total_cents": sum(x["brutto_cents"] for x in out)}


def list_payslips(db, period=None, eid=None):
    q, a = ("SELECT s.id, s.employee_id, e.name, s.period, s.brutto_cents, s.netto_cents, s.ag_cost_cents"
            " FROM payslips s JOIN employees e ON e.id=s.employee_id WHERE 1=1"), []
    if period:
        q, a = q + " AND s.period=?", a + [_month(period)]
    if eid:
        q, a = q + " AND s.employee_id=?", a + [eid]
    return [dict(r) for r in db.execute(q + " ORDER BY s.period DESC, e.name", a).fetchall()]


def payslip_text(db, pid, cfg, eid=None):
    r = db.execute("SELECT * FROM payslips WHERE id=?", (pid,)).fetchone()
    if not r or (eid is not None and r["employee_id"] != eid):
        raise LookupError("Abrechnung nicht gefunden")
    d = json.loads(r["data"])
    return _slip(d, cfg)


def _slip(d, cfg):
    c, W = d["calc"], 50
    line = lambda a, b: a.ljust(W - len(b)) + b
    L = [cfg.get("name", "").center(W), "ENTGELTABRECHNUNG (Schätzung)".center(W), "=" * W,
         line("Mitarbeiter:", d["name"]), line("Zeitraum:", d["period"]),
         line("Steuerklasse:", str(d["profile"]["tax_class"])), line("Art:", c["kind"])]
    if d["hours"] is not None:
        L.append(line("Stunden:", f"{d['hours']:g}" + (" (Hochrechnung)" if d["estimated"] else "")))
    L += ["-" * W, line("Brutto", fmt(c["brutto"]) + " EUR"), "-" * W]
    for k, n in (("lohnsteuer", "Lohnsteuer"), ("soli", "Solidaritätszuschlag"), ("kist", "Kirchensteuer"),
                 ("rv", "Rentenversicherung"), ("av", "Arbeitslosenversicherung"), ("kv", "Krankenversicherung"),
                 ("pv", "Pflegeversicherung")):
        if c[k]:
            L.append(line("- " + n, fmt(c[k]) + " EUR"))
    L += ["=" * W, line("NETTO (Auszahlung)", fmt(c["netto"]) + " EUR"), "=" * W,
          line("Arbeitgeberkosten gesamt", fmt(c["ag_cost"]) + " EUR")]
    L += ["Hinweis: " + w for w in d["warnings"]]
    L.append("Unverbindliche Berechnung. Keine amtliche Lohnabrechnung.")
    return "\n".join(L)


# ----------------------------------------------------------------- Rechnungen
def _items(items, gross_prices=False):
    if not items:
        raise ValueError("Mindestens eine Position noetig")
    out = []
    for it in items:
        qty, rate = float(it["qty"]), float(it.get("tax_rate", 19))
        unit = int(it["unit_cents"])
        if qty <= 0 or not str(it.get("description", "")).strip():
            raise ValueError("Position braucht Beschreibung und Menge > 0")
        if rate not in TAX_RATES:
            raise ValueError("Steuersatz muss 0, 7 oder 19 sein")
        net = _r(qty * (unit / (1 + rate / 100) if gross_prices else unit))
        out.append(dict(description=str(it["description"]).strip(), qty=qty, unit_cents=unit, tax_rate=rate, net_cents=net))
    return out


def create_invoice(db, kind, party, items, address="", inv_date=None, due_days=14, service_date=None, note="",
                   ext_number=None, category=None, gross_prices=False, status="offen"):
    if kind not in ("out", "in"):
        raise ValueError("kind muss out (Ausgangsrechnung) oder in (Eingangsrechnung) sein")
    if not str(party).strip():
        raise ValueError("Rechnungsempfaenger bzw. Lieferant fehlt")
    inv_date = inv_date or date.today().isoformat()
    date.fromisoformat(inv_date)
    lines = _items(items, gross_prices)
    by = {}
    for l in lines:
        by[l["tax_rate"]] = by.get(l["tax_rate"], 0) + l["net_cents"]
    tax_rows = [(r, n, _r(n * r / 100)) for r, n in sorted(by.items())]
    net, tax = sum(n for _, n, _ in tax_rows), sum(t for _, _, t in tax_rows)
    year = inv_date[:4]
    prefix = "RE" if kind == "out" else "ER"
    n = db.execute("SELECT COUNT(*) FROM invoices WHERE kind=? AND inv_date LIKE ?", (kind, year + "-%")).fetchone()[0] + 1
    number = f"{prefix}-{year}-{n:04d}"
    while db.execute("SELECT 1 FROM invoices WHERE number=?", (number,)).fetchone():
        n += 1
        number = f"{prefix}-{year}-{n:04d}"
    due = (date.fromisoformat(inv_date) + timedelta(int(due_days))).isoformat()
    cur = db.execute("INSERT INTO invoices(number,kind,party,address,inv_date,due_date,service_date,status,net_cents,tax_cents,"
                     "gross_cents,note,category,ext_number,paid_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (number, kind, party.strip(), address, inv_date, due, service_date or inv_date, status, net, tax, net + tax,
                      note, category, ext_number, inv_date if status == "bezahlt" else None))
    iid = cur.lastrowid
    for l in lines:
        db.execute("INSERT INTO invoice_items(invoice_id,description,qty,unit_cents,tax_rate,net_cents) VALUES(?,?,?,?,?,?)",
                   (iid, l["description"], l["qty"], l["unit_cents"], l["tax_rate"], l["net_cents"]))
    for r, nn, t in tax_rows:
        db.execute("INSERT INTO invoice_tax VALUES(?,?,?,?)", (iid, r, nn, t))
    db.commit()
    return {"id": iid, "number": number, "net_cents": net, "tax_cents": tax, "gross_cents": net + tax, "due_date": due}


def list_invoices(db, kind=None, status=None):
    q, a = "SELECT * FROM invoices WHERE 1=1", []
    if kind:
        q, a = q + " AND kind=?", a + [kind]
    if status:
        q, a = q + " AND status=?", a + [status]
    today = date.today().isoformat()
    out = []
    for r in db.execute(q + " ORDER BY inv_date DESC, id DESC", a).fetchall():
        d = dict(r)
        d["overdue"] = d["status"] == "offen" and bool(d["due_date"]) and d["due_date"] < today
        out.append(d)
    return out


def set_invoice_status(db, iid, status):
    if status not in ("offen", "bezahlt", "storniert"):
        raise ValueError("status: offen|bezahlt|storniert")
    cur = db.execute("UPDATE invoices SET status=?, paid_at=? WHERE id=?",
                     (status, date.today().isoformat() if status == "bezahlt" else None, iid))
    if cur.rowcount == 0:
        raise LookupError("Rechnung nicht gefunden")
    db.commit()
    return {"id": iid, "status": status}


def invoice_html(db, iid, cfg):
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
    if not inv:
        raise LookupError("Rechnung nicht gefunden")
    e = html.escape
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (iid,)).fetchall()
    taxes = db.execute("SELECT * FROM invoice_tax WHERE invoice_id=? ORDER BY rate", (iid,)).fetchall()
    de = lambda s: date.fromisoformat(s).strftime("%d.%m.%Y")
    rows = "".join(f"<tr><td>{e(i['description'])}</td><td class=r>{i['qty']:g}</td><td class=r>{fmt(i['unit_cents'])}</td>"
                   f"<td class=r>{i['tax_rate']:g} %</td><td class=r>{fmt(i['net_cents'])}</td></tr>" for i in items)
    trs = "".join(f"<tr><td colspan=4 class=r>zzgl. {t['rate']:g} % USt auf {fmt(t['net_cents'])}</td><td class=r>{fmt(t['tax_cents'])}</td></tr>" for t in taxes)
    title = "Rechnung" if inv["kind"] == "out" else "Eingangsrechnung (Beleg)"
    pay = (f"Bitte überweisen Sie bis {de(inv['due_date'])} auf IBAN {e(cfg.get('iban', ''))} {e(cfg.get('bic', ''))}."
           if inv["kind"] == "out" and cfg.get("iban") else "")
    ids = " | ".join(x for x in (f"Steuernr.: {cfg['tax_id']}" if cfg.get("tax_id") else "",
                                 f"USt-IdNr.: {cfg['ust_id']}" if cfg.get("ust_id") else "") if x)
    return f"""<!doctype html><html lang=de><meta charset=utf-8><title>{e(inv['number'])}</title>
<style>body{{font:14px system-ui,sans-serif;max-width:760px;margin:30px auto;color:#222}}table{{width:100%;border-collapse:collapse;margin:18px 0}}
th,td{{padding:6px 8px;border-bottom:1px solid #ddd;text-align:left}}.r{{text-align:right}}.tot td{{font-weight:700;border-top:2px solid #222}}
small{{color:#666}}@media print{{body{{margin:0}}}}</style>
<h2>{e(cfg.get('name', ''))}</h2><small>{e(cfg.get('address', ''))} {e(cfg.get('phone', ''))}<br>{e(ids)}</small>
<p><b>{e(inv['party'])}</b><br>{e(inv['address'] or '').replace(chr(10), '<br>')}</p>
<h3>{title} Nr. {e(inv['number'])}</h3>
<p>Rechnungsdatum: {de(inv['inv_date'])}<br>Leistungsdatum: {de(inv['service_date'])}{('<br>Beleg-Nr. Lieferant: ' + e(inv['ext_number'])) if inv['ext_number'] else ''}</p>
<table><tr><th>Leistung</th><th class=r>Menge</th><th class=r>Einzel netto</th><th class=r>USt</th><th class=r>Netto EUR</th></tr>{rows}
<tr><td colspan=4 class=r>Summe netto</td><td class=r>{fmt(inv['net_cents'])}</td></tr>{trs}
<tr class=tot><td colspan=4 class=r>Gesamtbetrag brutto</td><td class=r>{fmt(inv['gross_cents'])} EUR</td></tr></table>
<p>{pay}</p><p>{e(inv['note'] or '')}</p><small>{e(cfg.get('footer', ''))}</small></html>"""


# ----------------------------------------------------------------- Auswertung
def _split(gross, rate):
    net = _r(gross * 100 / (100 + rate))
    return net, gross - net


def summary(db, month):
    d1, d2 = _bounds(month)
    pos = {}
    for r in db.execute("SELECT oi.tax_rate r, SUM(oi.qty*oi.price_cents) g FROM order_items oi JOIN orders o ON o.id=oi.order_id"
                        " WHERE substr(o.created_at,1,10) BETWEEN ? AND ? GROUP BY oi.tax_rate", (d1, d2)):
        n, t = _split(int(r["g"] or 0), r["r"])
        pos[r["r"]] = dict(gross_cents=int(r["g"] or 0), net_cents=n, tax_cents=t)
    pay = [dict(method=r[0], cents=r[1] or 0, orders=r[2]) for r in db.execute(
        "SELECT payment_method, SUM(total_cents), COUNT(*) FROM orders WHERE substr(created_at,1,10) BETWEEN ? AND ?"
        " GROUP BY payment_method", (d1, d2))]
    days = [dict(day=r[0], orders=r[1], gross_cents=r[2] or 0) for r in db.execute(
        "SELECT substr(created_at,1,10), COUNT(*), SUM(total_cents) FROM orders WHERE substr(created_at,1,10) BETWEEN ? AND ?"
        " GROUP BY 1 ORDER BY 1", (d1, d2))]

    def inv(kind):
        out = {}
        for r in db.execute("SELECT t.rate, SUM(t.net_cents) n, SUM(t.tax_cents) x FROM invoice_tax t JOIN invoices i ON i.id=t.invoice_id"
                            " WHERE i.kind=? AND i.status!='storniert' AND i.inv_date BETWEEN ? AND ? GROUP BY t.rate", (kind, d1, d2)):
            out[r["rate"]] = dict(net_cents=r["n"], tax_cents=r["x"])
        return out
    out_i, in_i = inv("out"), inv("in")
    ust = sum(v["tax_cents"] for v in pos.values()) + sum(v["tax_cents"] for v in out_i.values())
    vst = sum(v["tax_cents"] for v in in_i.values())
    income = sum(v["net_cents"] for v in pos.values()) + sum(v["net_cents"] for v in out_i.values())
    expenses = sum(v["net_cents"] for v in in_i.values())
    pr = db.execute("SELECT COALESCE(SUM(brutto_cents),0), COALESCE(SUM(netto_cents),0), COALESCE(SUM(ag_cost_cents),0), COUNT(*)"
                    " FROM payslips WHERE period=?", (month,)).fetchone()
    openr = db.execute("SELECT COUNT(*), COALESCE(SUM(gross_cents),0) FROM invoices WHERE kind='out' AND status='offen'").fetchone()
    openi = db.execute("SELECT COUNT(*), COALESCE(SUM(gross_cents),0) FROM invoices WHERE kind='in' AND status='offen'").fetchone()
    return dict(month=month, pos_by_rate=pos, pos_payments=pay, days=days, out_invoices_by_rate=out_i, in_invoices_by_rate=in_i,
                ust_cents=ust, vorsteuer_cents=vst, zahllast_cents=ust - vst,
                income_net_cents=income, expenses_net_cents=expenses,
                payroll=dict(payslips=pr[3], brutto_cents=pr[0], netto_cents=pr[1], ag_cost_cents=pr[2]),
                result_cents=income - expenses - pr[2],
                open_out=dict(count=openr[0], cents=openr[1]), open_in=dict(count=openi[0], cents=openi[1]),
                note="Orientierungswerte. Umsatzsteuer-Voranmeldung und Jahresabschluss bitte mit dem Steuerberater abstimmen.")


def export_csv(db, month):
    """DATEV-aehnliche Buchungsliste (SKR03) als Vorlage. Vor dem Import mit dem Steuerberater abstimmen."""
    d1, d2 = _bounds(month)
    ert = {7: 8300, 19: 8400}
    w = io.StringIO()
    cw = csv.writer(w, delimiter=";")
    cw.writerow(["Umsatz", "S/H", "Konto", "Gegenkonto", "Belegdatum", "Belegnummer", "Buchungstext"])
    for r in db.execute("SELECT substr(o.created_at,1,10) d, o.payment_method m, oi.tax_rate t, SUM(oi.qty*oi.price_cents) g"
                        " FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE substr(o.created_at,1,10) BETWEEN ? AND ?"
                        " GROUP BY 1,2,3 ORDER BY 1", (d1, d2)):
        cw.writerow([f"{r['g'] / 100:.2f}".replace(".", ","), "S", 1000 if r["m"] == "cash" else 1200, ert.get(int(r["t"]), 8000),
                     r["d"], "KASSE-" + r["d"], f"Kassenumsatz {r['t']:g}% ({r['m']})"])
    for i in db.execute("SELECT * FROM invoices WHERE status!='storniert' AND inv_date BETWEEN ? AND ? ORDER BY inv_date", (d1, d2)):
        for t in db.execute("SELECT rate, net_cents+tax_cents g FROM invoice_tax WHERE invoice_id=?", (i["id"],)):
            if i["kind"] == "out":
                k, g = 1400, ert.get(int(t["rate"]), 8000)
            else:
                k = 4210 if i["category"] == "miete" else {7: 3300, 19: 3400}.get(int(t["rate"]), 4900)
                g = 1600
            cw.writerow([f"{t['g'] / 100:.2f}".replace(".", ","), "S", k, g, i["inv_date"], i["ext_number"] or i["number"],
                         f"{i['party']} ({t['rate']:g}%)"])
    return w.getvalue()


# ----------------------------------------------------------------- AI-Agent
def _period_from_text(q):
    m = re.search(r"\b(0?[1-9]|1[0-2])[./](20\d\d)\b", q)
    if m:
        return f"{m[2]}-{int(m[1]):02d}"
    if re.search(r"letzt\w* monat|vormonat|last month", q):
        return (date.today().replace(day=1) - timedelta(1)).strftime("%Y-%m")
    return date.today().strftime("%Y-%m")


def agent_answer(db, question):
    """Beantwortet Fragen zu Netto-Gehalt, Rechnungen, USt und Ergebnis. None = nicht zustaendig."""
    q = question.lower()
    per = _period_from_text(q)
    names = [r for r in db.execute("SELECT id, name FROM employees") if r["name"].lower() in q]
    if re.search(r"netto|gehalt|lohn(?!kosten)|brutto|abrechnung|verdien", q) and names:
        e = names[0]
        if not db.execute("SELECT 1 FROM payroll_profiles WHERE employee_id=?", (e["id"],)).fetchone():
            return {"answer": f"Für {e['name']} sind noch keine Lohndaten (Steuerklasse, Lohn) hinterlegt."}
        pv = preview(db, e["id"], per)
        c = pv["calc"]
        a = (f"{pv['name']} ({pv['period']}, Steuerklasse {pv['profile']['tax_class']}, {c['kind']}): "
             f"Brutto {fmt(c['brutto'])} EUR, Abzüge {fmt(c['abzuege'])} EUR (Steuern {fmt(c['lohnsteuer'] + c['soli'] + c['kist'])}, "
             f"Sozialversicherung {fmt(c['rv'] + c['av'] + c['kv'] + c['pv'])}), Netto {fmt(c['netto'])} EUR. "
             f"Arbeitgeberkosten {fmt(c['ag_cost'])} EUR. Das ist eine Schätzung.")
        return {"answer": a + (" " + pv["warnings"][0] if pv["warnings"] else ""),
                "options": [f"Rechnungen offen", f"Umsatzsteuer {per[5:]}/{per[:4]}", "Neues Gespräch"]}
    if re.search(r"rechnung|forderung|mahn", q):
        op = list_invoices(db, status="offen")
        if not op:
            return {"answer": "Es gibt keine offenen Rechnungen."}
        o_out, o_in = [i for i in op if i["kind"] == "out"], [i for i in op if i["kind"] == "in"]
        late = [i for i in op if i["overdue"]]
        a = (f"Offen: {len(o_out)} Ausgangsrechnungen über {fmt(sum(i['gross_cents'] for i in o_out))} EUR (Forderungen) und "
             f"{len(o_in)} Eingangsrechnungen über {fmt(sum(i['gross_cents'] for i in o_in))} EUR (zu zahlen).")
        if late:
            a += " Überfällig: " + ", ".join(f"{i['number']} {i['party']} ({fmt(i['gross_cents'])} EUR)" for i in late[:5]) + "."
        return {"answer": a}
    if re.search(r"umsatzsteuer|mehrwertsteuer|mwst|\bust\b|zahllast|vorsteuer", q):
        s = summary(db, per)
        return {"answer": f"{per}: Umsatzsteuer {fmt(s['ust_cents'])} EUR, Vorsteuer {fmt(s['vorsteuer_cents'])} EUR, "
                          f"Zahllast {fmt(s['zahllast_cents'])} EUR (Orientierungswert, nicht die Voranmeldung)."}
    if re.search(r"gewinn|ergebnis|auswertung|personalkosten|lohnkosten|buchhaltung|einnahmen|ausgaben", q):
        s = summary(db, per)
        return {"answer": f"{per}: Einnahmen netto {fmt(s['income_net_cents'])} EUR, Ausgaben netto {fmt(s['expenses_net_cents'])} EUR, "
                          f"Personalkosten {fmt(s['payroll']['ag_cost_cents'])} EUR "
                          f"({s['payroll']['payslips']} Abrechnungen gespeichert), Ergebnis {fmt(s['result_cents'])} EUR."}
    return None


# ----------------------------------------------------------------- Demo
def seed_demo(db):
    if db.execute("SELECT 1 FROM invoices").fetchone():
        return
    ids = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM employees")}
    for nm, d in (("Amina", dict(wage_type="stunde", hourly_cents=1550, tax_class=1)),
                  ("Youssef", dict(wage_type="monat", monthly_cents=320000, tax_class=3, kfb=1.0, children=1)),
                  ("Lena", dict(type="minijob", wage_type="monat", monthly_cents=60000)),
                  ("Mehdi", dict(wage_type="monat", monthly_cents=280000, tax_class=1))):
        if nm in ids:
            set_profile(db, ids[nm], d)
    t = date.today()
    create_invoice(db, "out", "Müller Logistik GmbH", [
        {"description": "Catering Betriebsfeier (Speisen)", "qty": 40, "unit_cents": 2200, "tax_rate": 7},
        {"description": "Getränkepauschale", "qty": 40, "unit_cents": 800, "tax_rate": 19}],
        address="Industriestr. 5\n53113 Bonn", inv_date=(t - timedelta(20)).isoformat(), service_date=(t - timedelta(21)).isoformat())
    create_invoice(db, "out", "Schmitz & Partner", [{"description": "Mittagstisch Team (Abo Monat)", "qty": 1, "unit_cents": 45000, "tax_rate": 7}],
                   address="Königstr. 12\n53111 Bonn", inv_date=(t - timedelta(3)).isoformat())
    create_invoice(db, "in", "Metro Großhandel", [{"description": "Fleisch, Brötchen, Käse", "qty": 1, "unit_cents": 84000, "tax_rate": 7},
                                                   {"description": "Getränke", "qty": 1, "unit_cents": 31000, "tax_rate": 19}],
                   ext_number="MG-77412", inv_date=(t - timedelta(8)).isoformat(), category="wareneinkauf")
    create_invoice(db, "in", "Immobilien Rhein KG", [{"description": f"Gewerbemiete {t:%m/%Y}", "qty": 1, "unit_cents": 280000, "tax_rate": 0}],
                   ext_number=f"M-{t:%Y%m}", inv_date=t.replace(day=1).isoformat(), category="miete", status="bezahlt")
    run_payroll(db, (t.replace(day=1) - timedelta(1)).strftime("%Y-%m"))
