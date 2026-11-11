"""
Web-Based Hotel Management System  (Flask + SQL database)

Public side : home, online booking, demo card payment, booking confirmation,
              guest feedback form
Staff side  : front-desk booking, guest list and booking page (check-in by QR,
              edit, extend, cancel), restaurant and service charges,
              housekeeping, checkout with PDF invoice, change password
Admin side  : reports with charts, email/SMS log, staff accounts

Run locally :  pip install -r requirements.txt
               python app.py          ->  http://127.0.0.1:5000
On Render   :  set DATABASE_URL and SECRET_KEY (see DEPLOY.md)

First logins (change them straight away!):
               admin / admin123      reception / hotel123
"""
import csv
import hmac
import io
import mimetypes
import os
import re
import secrets
import smtplib
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from itsdangerous import BadSignature, URLSafeSerializer
from werkzeug.security import check_password_hash, generate_password_hash

import db
from documents import build_invoice_pdf, qr_png, qr_svg

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
app.teardown_appcontext(db.close_db)

# ------------------------------------------------------------------
# SETTINGS  (change names, prices and pictures here)
# ------------------------------------------------------------------
HOTEL_NAME = os.environ.get("HOTEL_NAME", "Grand Oasis Hotel")
CURRENCY = os.environ.get("CURRENCY", "SAR")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

IMAGES = {  # file names inside the static folder
    "logo": "logo.png",
    "banner": "banner.jpg",
    "hotel": "hotel.jpg",
    "restaurant": "restaurant.jpg",
}

ROOM_TYPES = {
    "Single": {"price": 5000, "image": "room_single.jpg"},
    "Double": {"price": 8000, "image": "room_double.jpg"},
    "Suite": {"price": 15000, "image": "room_suite.jpg"},
}

ROOMS = {  # room number -> room type
    "101": "Single", "102": "Double", "103": "Single", "104": "Double",
    "201": "Suite", "202": "Suite", "203": "Double", "204": "Single",
}

MENU = {  # restaurant item -> price
    "Chicken Biryani": 350, "Grilled Fish": 450, "Club Sandwich": 250,
    "Pasta Alfredo": 300, "Fresh Salad": 150, "Tea": 50, "Coffee": 80,
    "Fresh Juice": 120,
}

SERVICES = {  # extra services: kind -> {item: price}
    "Laundry": {"Shirt or top (wash and iron)": 25,
                "Trousers (wash and iron)": 30, "Suit dry clean": 80},
    "Spa": {"Massage (60 min)": 350, "Facial": 250},
    "Taxi": {"Airport transfer": 150, "City ride": 60},
}

PAYMENT_METHODS = ["Cash", "Card", "Bank transfer"]
HOUSEKEEPING = ["Clean", "Dirty", "Maintenance"]

# Booking states
BOOKED, CHECKED_IN, CHECKED_OUT, CANCELLED = (
    "Booked", "Checked-in", "Checked-out", "Cancelled")

# Cancelling on or after the arrival date keeps the first night's charge
FIRST_NIGHT_FEE_ON_LATE_CANCEL = True

# Email (optional). Fill these with environment variables, never in the code.
try:
    import local_settings as _ls   # optional private file (not uploaded to GitHub)
except ImportError:
    _ls = None


def _setting(name, default=""):
    """Environment variable first, then local_settings.py, then the default."""
    return os.environ.get(name) or getattr(_ls, name, default)


SMTP_HOST = _setting("SMTP_HOST")
SMTP_PORT = int(_setting("SMTP_PORT", "587") or 587)
SMTP_USER = _setting("SMTP_USER")
SMTP_PASSWORD = _setting("SMTP_PASSWORD")
SMTP_SENDER = _setting("SMTP_SENDER") or SMTP_USER
EMAIL_LIVE = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

DEFAULT_PASSWORDS = {"admin": "admin123", "reception": "hotel123"}
feedback_signer = URLSafeSerializer(app.secret_key, salt="guest-feedback")

db.init_db(list(ROOMS), [
    ("admin", generate_password_hash(os.environ.get("ADMIN_PASSWORD",
                                                    "admin123")), "admin"),
    ("reception", generate_password_hash("hotel123"), "staff"),
])


# ------------------------------------------------------------------
# SMALL HELPERS
# ------------------------------------------------------------------
def today_str():
    return date.today().isoformat()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_date(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def money(value):
    return "{} {:,}".format(CURRENCY, to_int(value)).strip()


app.jinja_env.filters["money"] = money
app.jinja_env.filters["num"] = lambda v: "{:,}".format(to_int(v))


def logo_path():
    path = os.path.join(BASE_DIR, "static", IMAGES["logo"])
    return path if os.path.exists(path) else None


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_forms():
    """Reject form posts that do not carry this session's secret token."""
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not hmac.compare_digest(session.get("csrf", ""), sent):
            abort(400)


@app.errorhandler(400)
def bad_request(error):
    return render_template(
        "error.html", title="Please try again",
        message="Your session expired or the form was not valid. "
                "Go back, refresh the page and send the form again."), 400


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", title="Page not found",
                           message="That page does not exist."), 404


@app.context_processor
def inject_globals():
    return {"hotel_name": HOTEL_NAME, "images": IMAGES, "today": today_str(),
            "currency": CURRENCY, "user": session.get("user"),
            "role": session.get("role"),
            "default_pw": session.get("default_pw", False)}


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                flash("Please log in to continue.", "error")
                return redirect(url_for("login", next=request.path))
            if role and session.get("role") != role:
                flash("Only an administrator can open that page.", "error")
                return redirect(url_for("home"))
            return view(*args, **kwargs)
        return wrapper
    return decorator


# ------------------------------------------------------------------
# ROOMS AND BOOKINGS
# ------------------------------------------------------------------
def room_overview():
    """Every room with today's status and housekeeping state."""
    clean = {r["room_no"]: r["status"]
             for r in db.query("SELECT room_no, status FROM housekeeping")}
    status = {no: "Available" for no in ROOMS}
    for r in db.query("SELECT room_no, status, check_out FROM bookings "
                      "WHERE status IN ('Booked', 'Checked-in')"):
        no = r["room_no"]
        if no not in status:
            continue
        if r["status"] == CHECKED_IN:
            status[no] = "Occupied"
        elif status[no] != "Occupied" and r["check_out"] > today_str():
            status[no] = "Reserved"
    return [{"no": no, "type": rtype,
             "price": ROOM_TYPES[rtype]["price"],
             "image": ROOM_TYPES[rtype]["image"],
             "status": status[no], "housekeeping": clean.get(no, "Clean")}
            for no, rtype in ROOMS.items()]


def room_is_free(room_no, check_in, check_out, exclude_id=""):
    """True if no active booking overlaps these dates."""
    clash = db.one(
        "SELECT booking_id FROM bookings WHERE room_no = ? "
        "AND status IN ('Booked', 'Checked-in') AND check_in < ? "
        "AND check_out > ? AND booking_id <> ?",
        (room_no, check_out.isoformat(), check_in.isoformat(), exclude_id))
    return clash is None


def next_booking_id():
    numbers = [int(r["booking_id"][1:])
               for r in db.query("SELECT booking_id FROM bookings")
               if r["booking_id"][1:].isdigit()]
    return "B{:03d}".format(max(numbers, default=0) + 1)


BOOKING_KEYS = ("name", "phone", "email", "id_proof", "room_no",
                "check_in", "check_out")


def read_booking_form(source):
    return {key: source.get(key, "").strip() for key in BOOKING_KEYS}


def validate_booking(form, exclude_id="", email_required=False,
                     allow_past_checkin=False, dates=True):
    """Return (errors, phone, check_in, check_out)."""
    errors = []
    phone = form["phone"].replace(" ", "").lstrip("+")
    email = form["email"]
    if not form["name"]:
        errors.append("Enter the guest's name.")
    if not (phone.isdigit() and 7 <= len(phone) <= 15):
        errors.append("Enter a phone number with 7 to 15 digits.")
    if email_required and not email:
        errors.append("Enter an email address for the confirmation.")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errors.append("Enter a valid email address.")
    if not form["id_proof"]:
        errors.append("Enter the guest's ID proof number.")
    check_in = check_out = None
    if dates:
        if form["room_no"] not in ROOMS:
            errors.append("Choose a room from the list.")
        try:
            check_in = parse_date(form["check_in"])
            check_out = parse_date(form["check_out"])
            if check_in < date.today() and not allow_past_checkin:
                errors.append("Check-in date cannot be in the past.")
            if check_out <= check_in:
                errors.append("Check-out must be after the check-in date.")
        except ValueError:
            errors.append("Enter valid check-in and check-out dates.")
        if not errors and not room_is_free(form["room_no"], check_in,
                                           check_out, exclude_id):
            errors.append("Room {} is already booked for those dates. "
                          "Choose another room or different dates."
                          .format(form["room_no"]))
    return errors, phone, check_in, check_out


def create_booking(form, phone, check_in, check_out, source):
    rtype = ROOMS[form["room_no"]]
    nights = (check_out - check_in).days
    charge = nights * ROOM_TYPES[rtype]["price"]
    for _ in range(5):  # retry if two bookings pick the same number
        booking_id = next_booking_id()
        try:
            db.execute(
                "INSERT INTO bookings (booking_id, name, phone, email, "
                "id_proof, room_no, room_type, check_in, check_out, nights, "
                "room_charge, source, status, created_at, refund_amount) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (booking_id, form["name"], phone, form["email"],
                 form["id_proof"], form["room_no"], rtype,
                 check_in.isoformat(), check_out.isoformat(), nights, charge,
                 source, BOOKED, now_str()))
            return booking_id
        except db.DatabaseError:
            continue
    raise RuntimeError("Could not create the booking")


def get_booking(booking_id):
    return db.one("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,))


def add_payment(booking_id, amount, method):
    db.execute("INSERT INTO payments (booking_id, amount, method, created_at) "
               "VALUES (?,?,?,?)", (booking_id, amount, method, now_str()))


def add_charge(booking_id, kind, description, qty, unit_price):
    db.execute("INSERT INTO charges (booking_id, kind, description, qty, "
               "unit_price, amount, created_at) VALUES (?,?,?,?,?,?,?)",
               (booking_id, kind, description, qty, unit_price,
                qty * unit_price, now_str()))


def get_bill(booking_id, title="INVOICE", prefix="INV"):
    """Everything needed to show or print a bill."""
    b = get_booking(booking_id)
    if b is None:
        return None
    charges = db.query("SELECT * FROM charges WHERE booking_id = ? ORDER BY id",
                       (booking_id,))
    payments = db.query("SELECT * FROM payments WHERE booking_id = ? "
                        "ORDER BY id", (booking_id,))
    nights = to_int(b["nights"]) or 1
    room_charge = to_int(b["room_charge"])
    lines = [{"description": "Room {} ({}) - accommodation".format(
                  b["room_no"], b["room_type"]),
              "qty": nights, "unit": room_charge // nights,
              "amount": room_charge, "kind": "Room"}]
    for c in charges:
        lines.append({"description": "{}: {}".format(c["kind"],
                                                     c["description"]),
                      "qty": to_int(c["qty"]), "unit": to_int(c["unit_price"]),
                      "amount": to_int(c["amount"]), "kind": c["kind"]})
    total = sum(line["amount"] for line in lines)
    paid = sum(to_int(p["amount"]) for p in payments)
    return {"booking": b, "lines": lines, "payments": payments,
            "total": total, "paid": paid, "due": max(total - paid, 0),
            "issued": today_str(), "title": title, "prefix": prefix}


def totals_map():
    """booking_id -> (total, paid) for the guest list and reports."""
    charges = {r["booking_id"]: to_int(r["s"]) for r in db.query(
        "SELECT booking_id, SUM(amount) AS s FROM charges GROUP BY booking_id")}
    paid = {r["booking_id"]: to_int(r["s"]) for r in db.query(
        "SELECT booking_id, SUM(amount) AS s FROM payments GROUP BY booking_id")}
    return charges, paid


def payment_label(total, paid, status):
    if status == CANCELLED:
        return "-"
    if paid >= total > 0:
        return "Paid"
    return "Part paid" if paid > 0 else "Unpaid"


def invoice_pdf(booking_id):
    b = get_booking(booking_id)
    final = b["status"] == CHECKED_OUT
    bill = get_bill(booking_id,
                    "INVOICE" if final else "BOOKING SUMMARY",
                    "INV" if final else "BK")
    return build_invoice_pdf(bill, HOTEL_NAME, CURRENCY, logo_path())


# ------------------------------------------------------------------
# EMAIL AND SMS
# ------------------------------------------------------------------
def email_images(booking_id):
    """Pictures used inside the email: {content-id: (bytes, subtype)}."""
    images = {"qr-code": (qr_png("HOTEL:" + booking_id), "png")}
    path = logo_path()
    if path:
        with open(path, "rb") as f:
            subtype = (mimetypes.guess_type(path)[0] or "image/png").split("/")[1]
            images["hotel-logo"] = (f.read(), subtype)
    return images


def build_mime(to, subject, text, html, images, pdf):
    msg = EmailMessage()
    msg["From"] = "{} <{}>".format(HOTEL_NAME, SMTP_SENDER or "noreply@example.com")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    html_part = msg.get_payload()[1]
    for cid, (data, subtype) in images.items():
        html_part.add_related(data, "image", subtype, cid="<{}>".format(cid))
    if pdf:
        msg.add_attachment(pdf[1], maintype="application", subtype="pdf",
                           filename=pdf[0])
    return msg


def deliver(msg):
    """Send by SMTP. Returns 'Sent', 'Failed' or 'Demo' (not configured)."""
    if not EMAIL_LIVE:
        return "Demo"
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=8)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=8)
            server.starttls()
        with server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return "Sent"
    except Exception:
        return "Failed"


def log_message(booking_id, channel, to, subject, text, html, mode):
    db.execute("INSERT INTO messages (created_at, booking_id, channel, "
               "to_addr, subject, body_text, body_html, mode) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (now_str(), booking_id, channel, to, subject, text, html, mode))


EMAIL_KINDS = {
    "confirmed": {
        "subject": "Booking confirmed - {id}",
        "heading": "Your booking is confirmed",
        "intro": "Thank you for choosing {hotel}. Your reservation details "
                 "and payment summary are below. Show the QR code at "
                 "reception for a quick check-in.",
        "sms": "{hotel}: booking {id} confirmed. Room {room}, {ci} to {co}. "
               "Show your QR code at reception.",
        "qr": True, "pdf": True},
    "modified": {
        "subject": "Booking updated - {id}",
        "heading": "Your booking was updated",
        "intro": "Your reservation at {hotel} was changed. The latest "
                 "details and charges are below.",
        "sms": "{hotel}: booking {id} was updated. Room {room}, {ci} to {co}.",
        "qr": True, "pdf": True},
    "cancelled": {
        "subject": "Booking cancelled - {id}",
        "heading": "Your booking was cancelled",
        "intro": "Your reservation at {hotel} has been cancelled.",
        "sms": "{hotel}: booking {id} was cancelled.",
        "qr": False, "pdf": False},
    "invoice": {
        "subject": "Your invoice from {hotel} - {id}",
        "heading": "Thank you for staying with us",
        "intro": "We hope you enjoyed your stay at {hotel}. Your final "
                 "invoice is below and attached as a PDF.",
        "sms": "{hotel}: thank you for staying with us. Your invoice for "
               "booking {id} was emailed to you.",
        "qr": False, "pdf": True},
}


def notify_guest(booking_id, kind):
    """Send (or log) an email and an SMS about a booking."""
    b = get_booking(booking_id)
    if b is None:
        return
    spec = EMAIL_KINDS[kind]
    fields = {"id": booking_id, "hotel": HOTEL_NAME, "room": b["room_no"],
              "ci": b["check_in"], "co": b["check_out"]}
    subject = spec["subject"].format(**fields)
    intro = spec["intro"].format(**fields)

    if b.get("email"):
        bill = get_bill(booking_id, "INVOICE" if kind == "invoice"
                        else "BOOKING SUMMARY",
                        "INV" if kind == "invoice" else "BK")
        feedback_url = None
        if kind == "invoice":
            feedback_url = url_for("feedback", token=feedback_signer.dumps(
                booking_id), _external=True)
        images = email_images(booking_id) if kind != "cancelled" else {}
        html = render_template(
            "email.html", heading=spec["heading"], intro=intro, b=b,
            bill=bill, show_qr=spec["qr"], has_logo="hotel-logo" in images,
            feedback_url=feedback_url, kind=kind)
        text = "{}\n\n{}\n\nBooking {} - Room {} ({} to {}).\nTotal {}, paid {}, balance {}.".format(
            spec["heading"], intro, booking_id, b["room_no"], b["check_in"],
            b["check_out"], money(bill["total"]), money(bill["paid"]),
            money(bill["due"]))
        pdf = None
        if spec["pdf"]:
            pdf = ("{}-{}.pdf".format("invoice" if kind == "invoice"
                                      else "booking", booking_id),
                   invoice_pdf(booking_id))
        mode = deliver(build_mime(b["email"], subject, text, html, images, pdf))
        log_message(booking_id, "Email", b["email"], subject, text, html, mode)
    if b.get("phone"):
        log_message(booking_id, "SMS", b["phone"], subject,
                    spec["sms"].format(**fields), "", "Demo")


# ------------------------------------------------------------------
# LOGIN, ACCOUNT AND STAFF MANAGEMENT
# ------------------------------------------------------------------
FAILED_LOGINS = {}  # simple in-memory limit: (ip, username) -> [timestamps]


def too_many_attempts(key):
    recent = [t for t in FAILED_LOGINS.get(key, []) if time.time() - t < 600]
    FAILED_LOGINS[key] = recent
    return len(recent) >= 5


@app.route("/login", methods=["GET", "POST"])
def login():
    next_page = request.values.get("next", "")
    if not next_page.startswith("/") or next_page.startswith("//"):
        next_page = url_for("home")

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        key = (request.remote_addr, username)
        if too_many_attempts(key):
            flash("Too many wrong attempts. Wait 10 minutes and try again.",
                  "error")
            return render_template("login.html", next_page=next_page)
        account = db.one("SELECT * FROM users WHERE username = ?", (username,))
        if account and check_password_hash(account["password_hash"], password):
            session.clear()
            session["user"] = username
            session["role"] = account["role"]
            session["default_pw"] = DEFAULT_PASSWORDS.get(username) == password
            flash("Welcome, {}.".format(username), "success")
            return redirect(next_page)
        FAILED_LOGINS.setdefault(key, []).append(time.time())
        flash("Wrong username or password.", "error")
    return render_template("login.html", next_page=next_page)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/account", methods=["GET", "POST"])
@login_required()
def account():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        again = request.form.get("again", "")
        row = db.one("SELECT * FROM users WHERE username = ?",
                     (session["user"],))
        if not row or not check_password_hash(row["password_hash"], current):
            flash("Your current password is not correct.", "error")
        elif len(new) < 8:
            flash("The new password needs at least 8 characters.", "error")
        elif new != again:
            flash("The two new passwords do not match.", "error")
        else:
            db.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                       (generate_password_hash(new), session["user"]))
            session["default_pw"] = False
            flash("Password changed.", "success")
            return redirect(url_for("account"))
    return render_template("account.html")


@app.route("/staff", methods=["GET", "POST"])
@login_required("admin")
def staff():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "staff")
        if not re.match(r"^[a-z0-9_.-]{3,30}$", username):
            flash("Username: 3 to 30 letters, numbers, dots or dashes.", "error")
        elif len(password) < 8:
            flash("The password needs at least 8 characters.", "error")
        elif role not in ("admin", "staff"):
            flash("Choose a role.", "error")
        elif db.one("SELECT username FROM users WHERE username = ?", (username,)):
            flash("That username already exists.", "error")
        else:
            db.execute("INSERT INTO users (username, password_hash, role, "
                       "created_at) VALUES (?,?,?,?)",
                       (username, generate_password_hash(password), role,
                        now_str()))
            flash("Account {} created.".format(username), "success")
        return redirect(url_for("staff"))
    users = db.query("SELECT username, role, created_at FROM users "
                     "ORDER BY username")
    return render_template("staff.html", users=users)


@app.route("/staff/<username>/reset", methods=["POST"])
@login_required("admin")
def staff_reset(username):
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("The password needs at least 8 characters.", "error")
    elif not db.one("SELECT username FROM users WHERE username = ?", (username,)):
        flash("That account does not exist.", "error")
    else:
        db.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                   (generate_password_hash(password), username))
        flash("Password reset for {}.".format(username), "success")
    return redirect(url_for("staff"))


@app.route("/staff/<username>/delete", methods=["POST"])
@login_required("admin")
def staff_delete(username):
    admins = db.query("SELECT username FROM users WHERE role = 'admin'")
    if username == session["user"]:
        flash("You cannot delete the account you are using.", "error")
    elif len(admins) == 1 and admins[0]["username"] == username:
        flash("There must be at least one administrator.", "error")
    else:
        db.execute("DELETE FROM users WHERE username = ?", (username,))
        flash("Account {} deleted.".format(username), "success")
    return redirect(url_for("staff"))


# ------------------------------------------------------------------
# PUBLIC PAGES
# ------------------------------------------------------------------
@app.route("/")
def home():
    rooms = room_overview()
    stats = {
        "total": len(rooms),
        "available": sum(1 for r in rooms if r["status"] == "Available"
                         and r["housekeeping"] != "Maintenance"),
        "occupied": sum(1 for r in rooms if r["status"] == "Occupied"),
        "reserved": sum(1 for r in rooms if r["status"] == "Reserved"),
    }
    arrivals = departures = []
    if session.get("user"):
        arrivals = db.query("SELECT * FROM bookings WHERE status = 'Booked' "
                            "AND check_in = ? ORDER BY booking_id",
                            (today_str(),))
        departures = db.query("SELECT * FROM bookings WHERE status = "
                              "'Checked-in' AND check_out = ? ORDER BY "
                              "booking_id", (today_str(),))
    return render_template("home.html", rooms=rooms, stats=stats,
                           arrivals=arrivals, departures=departures)


@app.route("/online", methods=["GET", "POST"])
def online_booking():
    form = {key: "" for key in BOOKING_KEYS}
    form["room_no"] = request.args.get("room", "")
    if request.method == "POST":
        form = read_booking_form(request.form)
        errors, phone, check_in, check_out = validate_booking(
            form, email_required=True)
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            form["phone"] = phone
            session["pending"] = form
            return redirect(url_for("pay"))
    rooms = room_overview()
    return render_template("online.html", form=form, rooms=rooms,
                           prices={r["no"]: r["price"] for r in rooms})


def luhn_ok(number):
    total = 0
    for i, ch in enumerate(reversed(number)):
        digit = int(ch)
        if i % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def validate_card(name, number, expiry, cvv):
    errors = []
    number = number.replace(" ", "").replace("-", "")
    if not name:
        errors.append("Enter the name on the card.")
    if not (number.isdigit() and 13 <= len(number) <= 19 and luhn_ok(number)):
        errors.append("The card number is not valid.")
    try:
        month, year = expiry.split("/")
        month, year = int(month), 2000 + int(year)
        if not 1 <= month <= 12:
            raise ValueError
        today = date.today()
        if (year, month) < (today.year, today.month):
            errors.append("The card has expired.")
    except ValueError:
        errors.append("Enter the expiry date as MM/YY.")
    if not (cvv.isdigit() and 3 <= len(cvv) <= 4):
        errors.append("Enter the 3 or 4 digit security code.")
    return errors


@app.route("/pay", methods=["GET", "POST"])
def pay():
    form = session.get("pending")
    if not form:
        flash("Start by choosing your room and dates.", "error")
        return redirect(url_for("online_booking"))
    check_in, check_out = parse_date(form["check_in"]), parse_date(form["check_out"])
    nights = (check_out - check_in).days
    rtype = ROOMS[form["room_no"]]
    summary = {"form": form, "nights": nights, "type": rtype,
               "charge": nights * ROOM_TYPES[rtype]["price"]}

    if request.method == "POST":
        errors = validate_card(request.form.get("card_name", "").strip(),
                               request.form.get("card_number", ""),
                               request.form.get("expiry", "").strip(),
                               request.form.get("cvv", "").strip())
        if errors:
            for message in errors:
                flash(message, "error")
            return render_template("pay.html", s=summary)
        if not room_is_free(form["room_no"], check_in, check_out):
            session.pop("pending", None)
            flash("Sorry, that room was just booked. No payment was taken. "
                  "Please choose another room.", "error")
            return redirect(url_for("online_booking"))

        # Card details are checked and thrown away. Only the booking is saved.
        booking_id = create_booking(form, form["phone"], check_in, check_out,
                                    "Online")
        add_payment(booking_id, summary["charge"], "Online card")
        notify_guest(booking_id, "confirmed")
        session.pop("pending", None)
        session["last_booking"] = booking_id
        return redirect(url_for("confirmation"))
    return render_template("pay.html", s=summary)


@app.route("/confirmation")
def confirmation():
    booking_id = session.get("last_booking")
    bill = get_bill(booking_id, "BOOKING SUMMARY", "BK") if booking_id else None
    if bill is None:
        return redirect(url_for("online_booking"))
    return render_template("confirmation.html", bill=bill,
                           email_live=EMAIL_LIVE,
                           qr=qr_svg("HOTEL:" + booking_id, 150))


@app.route("/invoice/<booking_id>.pdf")
def invoice(booking_id):
    allowed = session.get("user") or session.get("last_booking") == booking_id
    if not allowed:
        return redirect(url_for("login", next=request.path))
    if get_booking(booking_id) is None:
        abort(404)
    return Response(invoice_pdf(booking_id), mimetype="application/pdf",
                    headers={"Content-Disposition":
                             "inline; filename=invoice-{}.pdf".format(booking_id)})


@app.route("/feedback/<token>", methods=["GET", "POST"])
def feedback(token):
    try:
        booking_id = feedback_signer.loads(token)
    except BadSignature:
        abort(404)
    b = get_booking(booking_id)
    if b is None or b["status"] != CHECKED_OUT:
        abort(404)
    existing = db.one("SELECT * FROM feedback WHERE booking_id = ?", (booking_id,))
    if existing:
        return render_template("feedback_done.html", b=b)
    if request.method == "POST":
        rating = to_int(request.form.get("rating"))
        comment = request.form.get("comment", "").strip()[:500]
        if not 1 <= rating <= 5:
            flash("Please choose a rating from 1 to 5 stars.", "error")
        else:
            db.execute("INSERT INTO feedback (booking_id, rating, comment, "
                       "created_at) VALUES (?,?,?,?)",
                       (booking_id, rating, comment, now_str()))
            return render_template("feedback_done.html", b=b, just_sent=True)
    return render_template("feedback.html", b=b)


# ------------------------------------------------------------------
# STAFF: BOOKING, GUEST LIST, BOOKING PAGE
# ------------------------------------------------------------------
@app.route("/book", methods=["GET", "POST"])
@login_required()
def book_room():
    form = {key: "" for key in BOOKING_KEYS}
    form["room_no"] = request.args.get("room", "")
    if request.method == "POST":
        form = read_booking_form(request.form)
        errors, phone, check_in, check_out = validate_booking(form)
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            booking_id = create_booking(form, phone, check_in, check_out,
                                        "Front desk")
            notify_guest(booking_id, "confirmed")
            flash("Booking {} saved.".format(booking_id), "success")
            return redirect(url_for("booking_page", booking_id=booking_id))
    rooms = room_overview()
    return render_template("book.html", form=form, rooms=rooms,
                           prices={r["no"]: r["price"] for r in rooms})


@app.route("/guests")
@login_required()
def guests():
    query = request.args.get("q", "").strip().lower()
    status = request.args.get("status", "")
    rows = db.query("SELECT * FROM bookings ORDER BY created_at DESC, "
                    "booking_id DESC")
    charges, paid = totals_map()
    result = []
    for r in rows:
        if status and r["status"] != status:
            continue
        haystack = " ".join(str(r.get(k) or "") for k in (
            "booking_id", "name", "phone", "email", "room_no")).lower()
        if query and query not in haystack:
            continue
        total = to_int(r["room_charge"]) + charges.get(r["booking_id"], 0)
        r["total"] = total
        r["payment"] = payment_label(total, paid.get(r["booking_id"], 0),
                                     r["status"])
        result.append(r)
    return render_template("guests.html", rows=result, q=query, status=status,
                           statuses=[BOOKED, CHECKED_IN, CHECKED_OUT, CANCELLED])


@app.route("/checkin")
@login_required()
def checkin_lookup():
    code = request.args.get("code", "").strip().upper()
    if code:
        match = re.search(r"B\d{3,}", code)
        if match and get_booking(match.group(0)):
            return redirect(url_for("booking_page", booking_id=match.group(0)))
        flash("No booking found for that code.", "error")
    return render_template("checkin.html")


def check_in_problem(b):
    """Why this guest cannot be checked in yet (None if all is fine)."""
    if b["status"] != BOOKED:
        return "Only a booking with status Booked can be checked in."
    if today_str() < b["check_in"]:
        return "Check-in opens on {}.".format(b["check_in"])
    if today_str() >= b["check_out"]:
        return "This booking's stay period has ended."
    room = db.one("SELECT status FROM housekeeping WHERE room_no = ?",
                  (b["room_no"],))
    if room and room["status"] != "Clean":
        return "Room {} is marked {}. Mark it Clean on the Housekeeping " \
               "page first.".format(b["room_no"], room["status"])
    return None


@app.route("/booking/<booking_id>")
@login_required()
def booking_page(booking_id):
    b = get_booking(booking_id)
    if b is None:
        abort(404)
    bill = get_bill(booking_id, "BOOKING SUMMARY", "BK")
    refund = db.one("SELECT SUM(amount) AS s FROM payments WHERE booking_id = ? "
                    "AND amount < 0", (booking_id,))
    feedback_row = db.one("SELECT * FROM feedback WHERE booking_id = ?",
                          (booking_id,))
    return render_template(
        "booking.html", b=b, bill=bill, problem=check_in_problem(b),
        qr=qr_svg("HOTEL:" + booking_id, 140), methods=PAYMENT_METHODS,
        feedback_row=feedback_row,
        feedback_url=(url_for("feedback", token=feedback_signer.dumps(booking_id))
                      if b["status"] == CHECKED_OUT else None))


@app.route("/booking/<booking_id>/checkin", methods=["POST"])
@login_required()
def booking_checkin(booking_id):
    b = get_booking(booking_id)
    if b is None:
        abort(404)
    problem = check_in_problem(b)
    if problem:
        flash(problem, "error")
    else:
        db.execute("UPDATE bookings SET status = ?, checked_in_at = ? "
                   "WHERE booking_id = ?", (CHECKED_IN, now_str(), booking_id))
        flash("{} is checked in to room {}.".format(b["name"], b["room_no"]),
              "success")
    return redirect(url_for("booking_page", booking_id=booking_id))


@app.route("/booking/<booking_id>/edit", methods=["GET", "POST"])
@login_required()
def booking_edit(booking_id):
    b = get_booking(booking_id)
    if b is None:
        abort(404)
    if b["status"] not in (BOOKED, CHECKED_IN):
        flash("Only active bookings can be edited.", "error")
        return redirect(url_for("booking_page", booking_id=booking_id))
    dates_locked = b["status"] == CHECKED_IN  # guests in the hotel keep room/dates
    form = {key: b.get(key) or "" for key in BOOKING_KEYS}

    if request.method == "POST":
        form = read_booking_form(request.form)
        if dates_locked:
            form["room_no"], form["check_in"], form["check_out"] = (
                b["room_no"], b["check_in"], b["check_out"])
        errors, phone, check_in, check_out = validate_booking(
            form, exclude_id=booking_id, dates=not dates_locked)
        if errors:
            for message in errors:
                flash(message, "error")
        elif dates_locked:
            db.execute("UPDATE bookings SET name=?, phone=?, email=?, "
                       "id_proof=? WHERE booking_id=?",
                       (form["name"], phone, form["email"], form["id_proof"],
                        booking_id))
            flash("Guest details updated.", "success")
            return redirect(url_for("booking_page", booking_id=booking_id))
        else:
            rtype = ROOMS[form["room_no"]]
            nights = (check_out - check_in).days
            db.execute(
                "UPDATE bookings SET name=?, phone=?, email=?, id_proof=?, "
                "room_no=?, room_type=?, check_in=?, check_out=?, nights=?, "
                "room_charge=? WHERE booking_id=?",
                (form["name"], phone, form["email"], form["id_proof"],
                 form["room_no"], rtype, check_in.isoformat(),
                 check_out.isoformat(), nights,
                 nights * ROOM_TYPES[rtype]["price"], booking_id))
            notify_guest(booking_id, "modified")
            flash("Booking updated. The guest was notified.", "success")
            return redirect(url_for("booking_page", booking_id=booking_id))

    rooms = room_overview()
    return render_template("booking_edit.html", b=b, form=form, rooms=rooms,
                           dates_locked=dates_locked,
                           prices={r["no"]: r["price"] for r in rooms})


@app.route("/booking/<booking_id>/extend", methods=["POST"])
@login_required()
def booking_extend(booking_id):
    b = get_booking(booking_id)
    if b is None:
        abort(404)
    if b["status"] not in (BOOKED, CHECKED_IN):
        flash("Only active bookings can be extended.", "error")
        return redirect(url_for("booking_page", booking_id=booking_id))
    try:
        old_out = parse_date(b["check_out"])
        new_out = parse_date(request.form.get("new_check_out", ""))
    except ValueError:
        flash("Enter a valid new check-out date.", "error")
        return redirect(url_for("booking_page", booking_id=booking_id))
    if new_out <= old_out:
        flash("The new check-out date must be after {}.".format(b["check_out"]),
              "error")
    elif not room_is_free(b["room_no"], old_out, new_out, booking_id):
        flash("Room {} is already booked after {}. Move the guest to another "
              "room or choose an earlier date.".format(b["room_no"],
                                                       b["check_out"]), "error")
    else:
        nights = (new_out - parse_date(b["check_in"])).days
        price = ROOM_TYPES[b["room_type"]]["price"]
        db.execute("UPDATE bookings SET check_out=?, nights=?, room_charge=? "
                   "WHERE booking_id=?",
                   (new_out.isoformat(), nights, nights * price, booking_id))
        notify_guest(booking_id, "modified")
        flash("Stay extended to {} ({} nights in total).".format(
            new_out.isoformat(), nights), "success")
    return redirect(url_for("booking_page", booking_id=booking_id))


@app.route("/booking/<booking_id>/cancel", methods=["POST"])
@login_required()
def booking_cancel(booking_id):
    b = get_booking(booking_id)
    if b is None:
        abort(404)
    if b["status"] != BOOKED:
        flash("Only a booking that has not been checked in can be cancelled.",
              "error")
        return redirect(url_for("booking_page", booking_id=booking_id))
    bill = get_bill(booking_id)
    fee = 0
    if FIRST_NIGHT_FEE_ON_LATE_CANCEL and today_str() >= b["check_in"]:
        fee = ROOM_TYPES[b["room_type"]]["price"]
    refund = max(bill["paid"] - fee, 0)
    if refund:
        add_payment(booking_id, -refund, "Refund")
    reason = request.form.get("reason", "").strip()[:200]
    db.execute("UPDATE bookings SET status=?, cancel_reason=?, refund_amount=? "
               "WHERE booking_id=?", (CANCELLED, reason, refund, booking_id))
    notify_guest(booking_id, "cancelled")
    flash("Booking cancelled. Refund: {}.".format(money(refund)), "success")
    return redirect(url_for("booking_page", booking_id=booking_id))


@app.route("/booking/<booking_id>/payment", methods=["POST"])
@login_required()
def booking_payment(booking_id):
    b = get_booking(booking_id)
    if b is None:
        abort(404)
    bill = get_bill(booking_id)
    amount = to_int(request.form.get("amount"))
    method = request.form.get("method", "")
    if b["status"] not in (BOOKED, CHECKED_IN):
        flash("Payments can only be added to active bookings.", "error")
    elif method not in PAYMENT_METHODS:
        flash("Choose a payment method.", "error")
    elif not 0 < amount <= bill["due"]:
        flash("Enter an amount between 1 and the balance due ({}).".format(
            money(bill["due"])), "error")
    else:
        add_payment(booking_id, amount, method)
        flash("Payment of {} recorded.".format(money(amount)), "success")
    return redirect(url_for("booking_page", booking_id=booking_id))


# ------------------------------------------------------------------
# STAFF: RESTAURANT, SERVICES, HOUSEKEEPING, CHECKOUT
# ------------------------------------------------------------------
def in_house_guests():
    return db.query("SELECT * FROM bookings WHERE status = 'Checked-in' "
                    "ORDER BY room_no")


@app.route("/restaurant", methods=["GET", "POST"])
@login_required()
def restaurant():
    if request.method == "POST":
        booking_id = request.form.get("booking_id", "")
        item = request.form.get("item", "")
        qty = to_int(request.form.get("qty", "1"))
        b = get_booking(booking_id)
        if b is None or b["status"] != CHECKED_IN:
            flash("Choose a guest who is checked in.", "error")
        elif item not in MENU:
            flash("Choose an item from the menu.", "error")
        elif not 1 <= qty <= 50:
            flash("Quantity must be between 1 and 50.", "error")
        else:
            add_charge(booking_id, "Restaurant", item, qty, MENU[item])
            flash("Added {} x{} to room {} ({}).".format(
                item, qty, b["room_no"], money(MENU[item] * qty)), "success")
        return redirect(url_for("restaurant"))
    guests_in = in_house_guests()
    for g in guests_in:
        g["food"] = to_int(db.one(
            "SELECT SUM(amount) AS s FROM charges WHERE booking_id = ? AND "
            "kind = 'Restaurant'", (g["booking_id"],))["s"])
    return render_template("restaurant.html", menu=MENU, in_house=guests_in)


@app.route("/services", methods=["GET", "POST"])
@login_required()
def services():
    if request.method == "POST":
        booking_id = request.form.get("booking_id", "")
        choice = request.form.get("service", "")
        qty = to_int(request.form.get("qty", "1"))
        kind, _, item = choice.partition("|")
        b = get_booking(booking_id)
        if b is None or b["status"] != CHECKED_IN:
            flash("Choose a guest who is checked in.", "error")
        elif item not in SERVICES.get(kind, {}):
            flash("Choose a service from the list.", "error")
        elif not 1 <= qty <= 50:
            flash("Quantity must be between 1 and 50.", "error")
        else:
            price = SERVICES[kind][item]
            add_charge(booking_id, kind, item, qty, price)
            flash("Added {}: {} x{} to room {} ({}).".format(
                kind, item, qty, b["room_no"], money(price * qty)), "success")
        return redirect(url_for("services"))
    recent = db.query(
        "SELECT c.*, b.room_no, b.name FROM charges c JOIN bookings b ON "
        "b.booking_id = c.booking_id WHERE c.kind <> 'Restaurant' AND "
        "b.status = 'Checked-in' ORDER BY c.id DESC")
    return render_template("services.html", services=SERVICES,
                           in_house=in_house_guests(), recent=recent)


@app.route("/housekeeping", methods=["GET", "POST"])
@login_required()
def housekeeping():
    if request.method == "POST":
        room_no = request.form.get("room_no", "")
        status = request.form.get("status", "")
        if room_no in ROOMS and status in HOUSEKEEPING:
            db.execute("UPDATE housekeeping SET status = ? WHERE room_no = ?",
                       (status, room_no))
            flash("Room {} marked {}.".format(room_no, status), "success")
        else:
            flash("Choose a valid room and status.", "error")
        return redirect(url_for("housekeeping"))
    return render_template("housekeeping.html", rooms=room_overview(),
                           options=HOUSEKEEPING)


@app.route("/checkout", methods=["GET", "POST"])
@login_required()
def checkout():
    if request.method == "POST":
        b = get_booking(request.form.get("booking_id", ""))
        if b is None or b["status"] != CHECKED_IN:
            flash("Choose a guest who is checked in.", "error")
            return redirect(url_for("checkout"))
        return render_template("bill.html", bill=get_bill(b["booking_id"]),
                               final=False, methods=PAYMENT_METHODS,
                               qr=qr_svg("HOTEL:" + b["booking_id"], 110))
    return render_template("checkout.html", in_house=in_house_guests())


@app.route("/checkout/confirm", methods=["POST"])
@login_required()
def confirm_checkout():
    booking_id = request.form.get("booking_id", "")
    method = request.form.get("payment_method", "Cash")
    if method not in PAYMENT_METHODS:
        method = "Cash"
    b = get_booking(booking_id)
    if b is None or b["status"] != CHECKED_IN:
        flash("That booking was not found or is not checked in.", "error")
        return redirect(url_for("checkout"))

    before = get_bill(booking_id)
    if before["due"] > 0:
        add_payment(booking_id, before["due"], method)
    db.execute("UPDATE bookings SET status = ?, checked_out_at = ? "
               "WHERE booking_id = ?", (CHECKED_OUT, now_str(), booking_id))
    db.execute("UPDATE housekeeping SET status = 'Dirty' WHERE room_no = ?",
               (b["room_no"],))
    notify_guest(booking_id, "invoice")
    flash("Checkout complete. Room {} is marked Dirty for cleaning.".format(
        b["room_no"]), "success")
    return render_template(
        "bill.html", bill=get_bill(booking_id), final=True,
        methods=PAYMENT_METHODS, qr=qr_svg("HOTEL:" + booking_id, 110),
        paid_now=before["due"],
        feedback_url=url_for("feedback", token=feedback_signer.dumps(booking_id)))


# ------------------------------------------------------------------
# ADMIN: REPORTS AND MESSAGES
# ------------------------------------------------------------------
def build_report():
    bookings = db.query("SELECT * FROM bookings")
    charge_rows = db.query("SELECT c.*, b.status AS bstatus FROM charges c "
                           "JOIN bookings b ON b.booking_id = c.booking_id")
    charge_sum, _ = totals_map()
    live = [b for b in bookings if b["status"] != CANCELLED]
    done = [b for b in live if b["status"] == CHECKED_OUT]
    active = [b for b in live if b["status"] != CHECKED_OUT]

    def total(b):
        return to_int(b["room_charge"]) + charge_sum.get(b["booking_id"], 0)

    live_ids = {b["booking_id"] for b in live}
    by_category = {"Room": sum(to_int(b["room_charge"]) for b in live)}
    items = {}
    for c in charge_rows:
        if c["booking_id"] not in live_ids:
            continue
        by_category[c["kind"]] = by_category.get(c["kind"], 0) + to_int(c["amount"])
        if c["kind"] == "Restaurant":
            items[c["description"]] = items.get(c["description"], 0) + to_int(c["qty"])

    by_type = {t: 0 for t in ROOM_TYPES}
    for b in live:
        if b["room_type"] in by_type:
            by_type[b["room_type"]] += to_int(b["room_charge"])

    sources = {}
    for b in live:
        key = b["source"] or "Front desk"
        sources[key] = sources.get(key, 0) + 1

    last_days = [(date.today() - timedelta(days=n)).isoformat()
                 for n in range(6, -1, -1)]
    daily = {d: 0 for d in last_days}
    for b in done:
        day = (b["checked_out_at"] or "")[:10]
        if day in daily:
            daily[day] += total(b)

    ratings = db.query("SELECT rating, comment, booking_id, created_at FROM "
                       "feedback ORDER BY id DESC")
    nights = [to_int(b["nights"]) for b in live]
    status_map = {r["no"]: r["status"] for r in room_overview()}
    occupied = sum(1 for s in status_map.values() if s == "Occupied")
    cancelled = [b for b in bookings if b["status"] == CANCELLED]
    return {
        "bookings": len(live),
        "occupancy": round(100 * occupied / len(ROOMS)),
        "revenue_done": sum(total(b) for b in done),
        "revenue_expected": sum(total(b) for b in active),
        "avg_stay": round(sum(nights) / len(nights), 1) if nights else 0,
        "cancelled": len(cancelled),
        "refunds": sum(to_int(b["refund_amount"]) for b in cancelled),
        "by_category": list(by_category.items()),
        "by_type": list(by_type.items()),
        "by_source": sorted(sources.items(), key=lambda x: -x[1]),
        "top_items": sorted(items.items(), key=lambda x: -x[1])[:6],
        "daily": [(d[5:], v) for d, v in daily.items()],
        "rating_avg": round(sum(r["rating"] for r in ratings) / len(ratings), 1)
        if ratings else None,
        "rating_count": len(ratings),
        "comments": [r for r in ratings if r["comment"]][:5],
    }


@app.route("/reports")
@login_required("admin")
def reports():
    return render_template("reports.html", r=build_report())


@app.route("/reports/export")
@login_required("admin")
def export_records():
    charges, paid = totals_map()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["booking_id", "name", "phone", "email", "room_no",
                     "room_type", "check_in", "check_out", "nights",
                     "room_charge", "extra_charges", "total", "paid", "status",
                     "source"])
    for b in db.query("SELECT * FROM bookings ORDER BY booking_id"):
        extra = charges.get(b["booking_id"], 0)
        writer.writerow([b["booking_id"], b["name"], b["phone"], b["email"],
                         b["room_no"], b["room_type"], b["check_in"],
                         b["check_out"], b["nights"], b["room_charge"], extra,
                         to_int(b["room_charge"]) + extra,
                         paid.get(b["booking_id"], 0), b["status"], b["source"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=hotel_report.csv"})


@app.route("/messages")
@login_required("admin")
def messages():
    rows = db.query("SELECT id, created_at, booking_id, channel, to_addr, "
                    "subject, body_text, mode, (body_html <> '') AS has_html "
                    "FROM messages "
                    "ORDER BY id DESC")
    return render_template("messages.html", rows=rows, email_live=EMAIL_LIVE)


@app.route("/messages/<int:message_id>")
@login_required("admin")
def message_preview(message_id):
    """Show a saved email the way the guest sees it."""
    row = db.one("SELECT * FROM messages WHERE id = ?", (message_id,))
    if row is None or not row["body_html"]:
        abort(404)
    html = row["body_html"]
    import base64
    qr = base64.b64encode(qr_png("HOTEL:" + (row["booking_id"] or ""))).decode()
    html = html.replace("cid:qr-code", "data:image/png;base64," + qr)
    path = logo_path()
    if path:
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            html = html.replace("cid:hotel-logo", "data:{};base64,{}".format(
                mime, base64.b64encode(f.read()).decode()))
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(debug=True)
