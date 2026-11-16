"""Einzelbetrieb-Ersatz fuer licensing.py (Mandanten/Stripe). Wird nur genutzt, wenn keine echte licensing.py im Ordner liegt."""


def tenant_for_host(host):
    return None


def get_license(slug):
    return None


def license_state(lic):
    return "active"


def allowed_when_expired(method, path):
    return True


def db_path(slug):
    return "chef.db"


def check_setup_token(slug, token):
    raise ValueError("Mandanten-Einrichtung ist im Einzelbetrieb-Modus nicht verfuegbar")


def mark_setup_used(slug):
    pass


def status_for(slug):
    return {"state": "active", "mode": "single"}


def by_session(sid):
    raise LookupError("Nicht verfuegbar")


def verify_stripe(raw, sig, secret):
    return False


def handle_event(event):
    return {}


def cli(args):
    print("Lizenzverwaltung ist im Einzelbetrieb-Modus nicht verfuegbar.")
