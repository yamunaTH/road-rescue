"""ROAD RESCUE - On Road Assistance System (Flask + MySQL).

Run:  python app.py      then open  http://127.0.0.1:5000
"""
import csv
import io
import os
import re
import sys
import uuid
from datetime import datetime
from functools import wraps

import mysql.connector
from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(BASE_DIR, "database", "schema.sql")

STATUSES = ["Pending", "Assigned", "Accepted", "On the Way", "In Progress", "Completed", "Cancelled"]
ACTIVE_STATUSES = ("Pending", "Assigned", "Accepted", "On the Way", "In Progress")
# status flow a service provider may follow
NEXT_STATUS = {
    "Assigned": ["Accepted"],
    "Accepted": ["On the Way"],
    "On the Way": ["In Progress"],
    "In Progress": ["Completed"],
}
VEHICLE_TYPES = ["Car", "Bike", "Scooter", "Auto", "Truck", "Bus", "Other"]
FUEL_TYPES = ["Petrol", "Diesel", "CNG", "Electric", "Hybrid"]
SERVICE_ICONS = ["fa-gas-pump", "fa-circle-dot", "fa-car-battery", "fa-truck-pickup",
                 "fa-wrench", "fa-screwdriver-wrench", "fa-oil-can", "fa-key", "fa-bolt", "fa-gear"]
DB_TABLES = ["users", "vehicles", "services", "service_providers",
             "assistance_requests", "request_logs", "feedback"]

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\d{10}$")
REG_RE = re.compile(r"^[A-Z0-9][A-Z0-9 -]{3,18}$")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def db_connect(with_db=True):
    cfg = app.config
    params = dict(host=cfg["DB_HOST"], port=cfg["DB_PORT"], user=cfg["DB_USER"],
                  password=cfg["DB_PASSWORD"], charset="utf8mb4", use_pure=True,
                  connection_timeout=8)
    if with_db:
        params["database"] = cfg["DB_NAME"]
    return mysql.connector.connect(**params)


def get_db():
    if "db" not in g:
        g.db = db_connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def query(sql, args=None, one=False):
    cur = get_db().cursor(dictionary=True)
    try:
        cur.execute(sql, args or ())
        rows = cur.fetchall()
    finally:
        cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=None):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(sql, args or ())
        db.commit()
        return cur.lastrowid
    except mysql.connector.Error:
        db.rollback()
        raise
    finally:
        cur.close()


def count(sql, args=None):
    row = query(sql, args, one=True)
    return int(list(row.values())[0]) if row else 0


def bootstrap_database():
    """Create database, tables, default services, admin and demo provider."""
    cfg = app.config
    conn = db_connect(with_db=False)
    cur = conn.cursor()
    cur.execute("CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_unicode_ci" % cfg["DB_NAME"])
    cur.execute("USE `%s`" % cfg["DB_NAME"])
    with open(SCHEMA_FILE, encoding="utf-8") as fh:
        script = fh.read()
    for chunk in script.split(";"):
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if not stmt or stmt.upper().startswith(("CREATE DATABASE", "USE ")):
            continue
        cur.execute(stmt)
    conn.commit()

    cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
    if cur.fetchone() is None:
        cur.execute("INSERT INTO users (name,email,phone,password_hash,role) VALUES (%s,%s,%s,%s,'admin')",
                    (cfg["ADMIN_NAME"], cfg["ADMIN_EMAIL"].lower(), cfg["ADMIN_PHONE"],
                     generate_password_hash(cfg["ADMIN_PASSWORD"])))
    cur.execute("SELECT id FROM service_providers LIMIT 1")
    if cur.fetchone() is None:
        cur.execute("INSERT INTO service_providers (name,email,phone,city,specialization,password_hash) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    ("Demo Rescue Team", "provider@roadrescue.com", "9876543210", "Bengaluru",
                     "All services", generate_password_hash("Provider@123")))
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_float(value):
    try:
        text = str(value).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def valid_password(pw):
    return len(pw) >= 6 and re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)


def safe_next(default):
    nxt = request.form.get("next") or request.args.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return default


def home_for(role):
    return url_for({"user": "user_dashboard", "admin": "admin_dashboard",
                    "provider": "provider_dashboard"}.get(role, "index"))


def login_required(role):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if "uid" not in session:
                flash("Please login to continue.", "warning")
                return redirect(url_for("login", role=role))
            if session.get("role") != role:
                flash("You are not allowed to open that page.", "danger")
                return redirect(home_for(session.get("role")))
            return func(*args, **kwargs)
        return wrapper
    return decorator


def add_log(request_id, status, note):
    execute("INSERT INTO request_logs (request_id,status,note) VALUES (%s,%s,%s)",
            (request_id, status, note))


REQ_SELECT = """
SELECT r.*, s.name AS service_name, s.icon AS service_icon, s.base_price,
       v.make_model, v.reg_number, v.vehicle_type, v.color, v.fuel_type,
       u.name AS user_name, u.phone AS user_phone, u.email AS user_email,
       p.name AS provider_name, p.phone AS provider_phone,
       (SELECT COUNT(*) FROM feedback f WHERE f.request_id = r.id) AS has_feedback
FROM assistance_requests r
JOIN services s ON s.id = r.service_id
JOIN vehicles v ON v.id = r.vehicle_id
JOIN users u ON u.id = r.user_id
LEFT JOIN service_providers p ON p.id = r.provider_id
"""


# ---------------------------------------------------------------------------
# Template filters / globals / security
# ---------------------------------------------------------------------------
@app.template_filter("badge")
def badge_filter(status):
    status = status or ""
    slug = status.lower().replace(" ", "-")
    return Markup('<span class="badge b-%s">%s</span>') % (slug, status)


@app.template_filter("stars")
def stars_filter(value):
    try:
        n = int(round(float(value or 0)))
    except (TypeError, ValueError):
        n = 0
    out = "".join('<i class="fa-%s fa-star star"></i>' % ("solid" if i <= n else "regular")
                  for i in range(1, 6))
    return Markup('<span class="stars">%s</span>' % out)


@app.template_filter("dt")
def dt_filter(value, fmt="%d %b %Y, %I:%M %p"):
    if not value:
        return "-"
    try:
        return value.strftime(fmt)
    except AttributeError:
        return str(value)


@app.template_filter("money")
def money_filter(value):
    try:
        return "₹{:,.0f}".format(float(value or 0))
    except (TypeError, ValueError):
        return "₹0"


def csrf_field():
    if "_csrf" not in session:
        session["_csrf"] = uuid.uuid4().hex
    return Markup('<input type="hidden" name="csrf_token" value="%s">') % session["_csrf"]


app.jinja_env.globals["csrf_field"] = csrf_field


@app.context_processor
def inject_globals():
    return dict(STATUSES=STATUSES, VEHICLE_TYPES=VEHICLE_TYPES, FUEL_TYPES=FUEL_TYPES,
                SERVICE_ICONS=SERVICE_ICONS, NEXT_STATUS=NEXT_STATUS,
                current_year=datetime.now().year)


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("_csrf")
        if not token or token != request.form.get("csrf_token"):
            abort(400)


@app.after_request
def security_headers(resp):
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store" if "uid" in session else resp.headers.get("Cache-Control", "no-cache")
    return resp


@app.errorhandler(mysql.connector.Error)
def handle_db_error(err):
    app.logger.error("Database error: %s", err)
    return render_template("error.html", code=500, title="Database problem",
                           message="We could not complete that action. Make sure MySQL is running in "
                                   "XAMPP and the details in config.py are correct."), 500


@app.errorhandler(400)
def handle_400(_e):
    return render_template("error.html", code=400, title="Session expired",
                           message="Your form session expired. Please go back, refresh the page and try again."), 400


@app.errorhandler(403)
def handle_403(_e):
    return render_template("error.html", code=403, title="Access denied",
                           message="You do not have permission to open this page."), 403


@app.errorhandler(404)
def handle_404(_e):
    return render_template("error.html", code=404, title="Page not found",
                           message="The page you are looking for does not exist."), 404


@app.errorhandler(500)
def handle_500(_e):
    return render_template("error.html", code=500, title="Something went wrong",
                           message="An unexpected error occurred. Please try again."), 500


# ---------------------------------------------------------------------------
# Public pages & authentication
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    services = query("SELECT * FROM services WHERE is_active=1 ORDER BY id")
    stats = dict(users=count("SELECT COUNT(*) FROM users WHERE role='user'"),
                 providers=count("SELECT COUNT(*) FROM service_providers WHERE status='active'"),
                 done=count("SELECT COUNT(*) FROM assistance_requests WHERE status='Completed'"))
    return render_template("index.html", services=services, stats=stats)


@app.route("/register", methods=["GET", "POST"])
def register():
    if "uid" in session:
        return redirect(home_for(session.get("role")))
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        email = f.get("email", "").strip().lower()
        phone = f.get("phone", "").strip()
        address = f.get("address", "").strip()
        pw = f.get("password", "")
        errors = []
        if len(name) < 3 or len(name) > 100:
            errors.append("Name must be 3 to 100 characters.")
        if not EMAIL_RE.match(email):
            errors.append("Enter a valid email address.")
        if not PHONE_RE.match(phone):
            errors.append("Phone number must be exactly 10 digits.")
        if not valid_password(pw):
            errors.append("Password must be at least 6 characters with letters and numbers.")
        if pw != f.get("confirm", ""):
            errors.append("Passwords do not match.")
        if not errors and query("SELECT id FROM users WHERE email=%s", (email,), one=True):
            errors.append("This email is already registered. Please login.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("register.html")
        try:
            execute("INSERT INTO users (name,email,phone,address,password_hash) VALUES (%s,%s,%s,%s,%s)",
                    (name, email, phone, address or None, generate_password_hash(pw)))
        except mysql.connector.IntegrityError:
            flash("This email is already registered. Please login.", "danger")
            return render_template("register.html")
        flash("Registration successful! Please login.", "success")
        return redirect(url_for("login", role="user"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "uid" in session:
        return redirect(home_for(session.get("role")))
    role = request.values.get("role", "user")
    if role not in ("user", "provider", "admin"):
        role = "user"
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template("login.html", role=role)
        if role == "provider":
            acct = query("SELECT * FROM service_providers WHERE email=%s", (email,), one=True)
        else:
            acct = query("SELECT * FROM users WHERE email=%s AND role=%s", (email, role), one=True)
        if not acct or not check_password_hash(acct["password_hash"], password):
            flash("Invalid email or password for this account type.", "danger")
        elif acct["status"] in ("blocked", "inactive"):
            flash("Your account has been disabled. Please contact the administrator.", "danger")
        else:
            session.clear()
            session.permanent = True
            session["uid"] = acct["id"]
            session["role"] = role
            session["name"] = acct["name"]
            flash("Welcome back, %s!" % acct["name"], "success")
            return redirect(home_for(role))
    return render_template("login.html", role=role)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# USER MODULE
# ---------------------------------------------------------------------------
@app.route("/user/dashboard")
@login_required("user")
def user_dashboard():
    uid = session["uid"]
    stats = dict(
        total=count("SELECT COUNT(*) FROM assistance_requests WHERE user_id=%s", (uid,)),
        active=count("SELECT COUNT(*) FROM assistance_requests WHERE user_id=%s AND status IN "
                     "('Pending','Assigned','Accepted','On the Way','In Progress')", (uid,)),
        completed=count("SELECT COUNT(*) FROM assistance_requests WHERE user_id=%s AND status='Completed'", (uid,)),
        vehicles=count("SELECT COUNT(*) FROM vehicles WHERE user_id=%s", (uid,)),
    )
    active_req = query(REQ_SELECT + " WHERE r.user_id=%s AND r.status IN "
                       "('Pending','Assigned','Accepted','On the Way','In Progress') "
                       "ORDER BY r.id DESC LIMIT 1", (uid,), one=True)
    history = query(REQ_SELECT + " WHERE r.user_id=%s ORDER BY r.id DESC LIMIT 5", (uid,))
    vehicles = query("SELECT * FROM vehicles WHERE user_id=%s ORDER BY id DESC LIMIT 4", (uid,))
    pending_fb = count("SELECT COUNT(*) FROM assistance_requests r WHERE r.user_id=%s AND r.status='Completed' "
                       "AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.request_id=r.id)", (uid,))
    return render_template("user/dashboard.html", stats=stats, active_req=active_req,
                           history=history, vehicles=vehicles, pending_fb=pending_fb)


def validate_vehicle(f):
    errors = []
    vtype = f.get("vehicle_type", "")
    model = f.get("make_model", "").strip()
    reg = f.get("reg_number", "").strip().upper()
    color = f.get("color", "").strip()
    fuel = f.get("fuel_type", "")
    if vtype not in VEHICLE_TYPES:
        errors.append("Select a valid vehicle type.")
    if len(model) < 2 or len(model) > 100:
        errors.append("Enter the vehicle make and model.")
    if not REG_RE.match(reg):
        errors.append("Enter a valid registration number (e.g. KA01AB1234).")
    if fuel not in FUEL_TYPES:
        errors.append("Select a valid fuel type.")
    if len(color) > 30:
        errors.append("Colour is too long.")
    return errors, (vtype, model, reg, color or None, fuel)


@app.route("/user/vehicles", methods=["GET", "POST"])
@login_required("user")
def user_vehicles():
    uid = session["uid"]
    if request.method == "POST":
        errors, data = validate_vehicle(request.form)
        if not errors and query("SELECT id FROM vehicles WHERE user_id=%s AND reg_number=%s",
                                (uid, data[2]), one=True):
            errors.append("You have already added this registration number.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            execute("INSERT INTO vehicles (user_id,vehicle_type,make_model,reg_number,color,fuel_type) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", (uid,) + data)
            flash("Vehicle added successfully.", "success")
            return redirect(url_for("user_vehicles"))
    vehicles = query("SELECT * FROM vehicles WHERE user_id=%s ORDER BY id DESC", (uid,))
    return render_template("user/vehicles.html", vehicles=vehicles)


@app.route("/user/vehicles/<int:vid>/edit", methods=["GET", "POST"])
@login_required("user")
def user_vehicle_edit(vid):
    vehicle = query("SELECT * FROM vehicles WHERE id=%s AND user_id=%s", (vid, session["uid"]), one=True)
    if not vehicle:
        abort(404)
    if request.method == "POST":
        errors, data = validate_vehicle(request.form)
        if not errors and query("SELECT id FROM vehicles WHERE user_id=%s AND reg_number=%s AND id<>%s",
                                (session["uid"], data[2], vid), one=True):
            errors.append("You already have another vehicle with this registration number.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            execute("UPDATE vehicles SET vehicle_type=%s,make_model=%s,reg_number=%s,color=%s,fuel_type=%s "
                    "WHERE id=%s AND user_id=%s", data + (vid, session["uid"]))
            flash("Vehicle updated.", "success")
            return redirect(url_for("user_vehicles"))
    return render_template("user/vehicle_form.html", vehicle=vehicle)


@app.route("/user/vehicles/<int:vid>/delete", methods=["POST"])
@login_required("user")
def user_vehicle_delete(vid):
    vehicle = query("SELECT id FROM vehicles WHERE id=%s AND user_id=%s", (vid, session["uid"]), one=True)
    if not vehicle:
        abort(404)
    try:
        execute("DELETE FROM vehicles WHERE id=%s AND user_id=%s", (vid, session["uid"]))
        flash("Vehicle removed.", "success")
    except mysql.connector.IntegrityError:
        flash("This vehicle is linked to assistance requests and cannot be deleted.", "warning")
    return redirect(url_for("user_vehicles"))


@app.route("/user/request/new", methods=["GET", "POST"])
@login_required("user")
def user_request_new():
    uid = session["uid"]
    vehicles = query("SELECT * FROM vehicles WHERE user_id=%s ORDER BY id DESC", (uid,))
    services = query("SELECT * FROM services WHERE is_active=1 ORDER BY id")
    if not vehicles:
        flash("Please add your vehicle first, then request assistance.", "warning")
        return redirect(url_for("user_vehicles"))
    selected_service = to_int(request.args.get("service"))
    if request.method == "POST":
        f = request.form
        service_id = to_int(f.get("service_id"))
        vehicle_id = to_int(f.get("vehicle_id"))
        location = f.get("location", "").strip()
        landmark = f.get("landmark", "").strip()
        description = f.get("description", "").strip()
        lat, lng = to_float(f.get("latitude")), to_float(f.get("longitude"))
        selected_service = service_id
        errors = []
        if not query("SELECT id FROM services WHERE id=%s AND is_active=1", (service_id,), one=True):
            errors.append("Please select a service type.")
        if not query("SELECT id FROM vehicles WHERE id=%s AND user_id=%s", (vehicle_id, uid), one=True):
            errors.append("Please select one of your vehicles.")
        if len(location) < 5 or len(location) > 255:
            errors.append("Enter your current location (5 to 255 characters).")
        if len(description) < 10 or len(description) > 1000:
            errors.append("Describe the problem in at least 10 characters.")
        if len(landmark) > 150:
            errors.append("Landmark is too long.")
        if (lat is not None and not -90 <= lat <= 90) or (lng is not None and not -180 <= lng <= 180):
            lat = lng = None
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            rid = execute(
                "INSERT INTO assistance_requests (request_code,user_id,vehicle_id,service_id,location,landmark,"
                "latitude,longitude,description,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pending')",
                ("TMP" + uuid.uuid4().hex[:12], uid, vehicle_id, service_id, location, landmark or None,
                 lat, lng, description))
            code = "RR%s%04d" % (datetime.now().strftime("%y%m%d"), rid)
            execute("UPDATE assistance_requests SET request_code=%s WHERE id=%s", (code, rid))
            add_log(rid, "Pending", "Request submitted by customer.")
            flash("Request submitted! Your request ID is %s." % code, "success")
            return redirect(url_for("user_request_details", rid=rid))
    return render_template("user/request_new.html", vehicles=vehicles, services=services,
                           selected_service=selected_service)


@app.route("/user/requests")
@login_required("user")
def user_requests():
    status = request.args.get("status", "")
    sql = REQ_SELECT + " WHERE r.user_id=%s"
    args = [session["uid"]]
    if status in STATUSES:
        sql += " AND r.status=%s"
        args.append(status)
    rows = query(sql + " ORDER BY r.id DESC", tuple(args))
    return render_template("user/requests.html", requests=rows, status=status)


@app.route("/user/requests/<int:rid>")
@login_required("user")
def user_request_details(rid):
    r = query(REQ_SELECT + " WHERE r.id=%s AND r.user_id=%s", (rid, session["uid"]), one=True)
    if not r:
        abort(404)
    logs = query("SELECT * FROM request_logs WHERE request_id=%s ORDER BY id", (rid,))
    fb = query("SELECT * FROM feedback WHERE request_id=%s", (rid,), one=True)
    return render_template("user/request_details.html", r=r, logs=logs, fb=fb)


@app.route("/user/requests/<int:rid>/cancel", methods=["POST"])
@login_required("user")
def user_request_cancel(rid):
    r = query("SELECT id,status FROM assistance_requests WHERE id=%s AND user_id=%s",
              (rid, session["uid"]), one=True)
    if not r:
        abort(404)
    if r["status"] not in ("Pending", "Assigned"):
        flash("This request can no longer be cancelled.", "warning")
    else:
        execute("UPDATE assistance_requests SET status='Cancelled' WHERE id=%s", (rid,))
        add_log(rid, "Cancelled", "Cancelled by customer.")
        flash("Request cancelled.", "success")
    return redirect(url_for("user_request_details", rid=rid))


@app.route("/user/feedback")
@login_required("user")
def user_feedback_list():
    uid = session["uid"]
    pending = query(REQ_SELECT + " WHERE r.user_id=%s AND r.status='Completed' AND NOT EXISTS "
                    "(SELECT 1 FROM feedback f WHERE f.request_id=r.id) ORDER BY r.id DESC", (uid,))
    given = query("SELECT f.*, r.request_code, s.name AS service_name, p.name AS provider_name "
                  "FROM feedback f JOIN assistance_requests r ON r.id=f.request_id "
                  "JOIN services s ON s.id=r.service_id "
                  "LEFT JOIN service_providers p ON p.id=f.provider_id "
                  "WHERE f.user_id=%s ORDER BY f.id DESC", (uid,))
    return render_template("user/feedback_list.html", pending=pending, given=given)


@app.route("/user/feedback/<int:rid>", methods=["GET", "POST"])
@login_required("user")
def user_feedback(rid):
    r = query(REQ_SELECT + " WHERE r.id=%s AND r.user_id=%s", (rid, session["uid"]), one=True)
    if not r:
        abort(404)
    if r["status"] != "Completed":
        flash("You can give feedback only after the service is completed.", "warning")
        return redirect(url_for("user_request_details", rid=rid))
    if query("SELECT id FROM feedback WHERE request_id=%s", (rid,), one=True):
        flash("You have already submitted feedback for this request.", "info")
        return redirect(url_for("user_request_details", rid=rid))
    if request.method == "POST":
        rating = to_int(request.form.get("rating"))
        comments = request.form.get("comments", "").strip()
        if rating is None or not 1 <= rating <= 5:
            flash("Please select a rating from 1 to 5 stars.", "danger")
        elif len(comments) > 500:
            flash("Comments must be within 500 characters.", "danger")
        else:
            try:
                execute("INSERT INTO feedback (request_id,user_id,provider_id,rating,comments) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (rid, session["uid"], r["provider_id"], rating, comments or None))
                flash("Thank you! Your feedback has been submitted.", "success")
                return redirect(url_for("user_request_details", rid=rid))
            except mysql.connector.IntegrityError:
                flash("Feedback already exists for this request.", "info")
                return redirect(url_for("user_request_details", rid=rid))
    return render_template("user/feedback.html", r=r)


@app.route("/user/profile", methods=["GET", "POST"])
@login_required("user")
def user_profile():
    uid = session["uid"]
    user = query("SELECT * FROM users WHERE id=%s", (uid,), one=True)
    if request.method == "POST":
        f = request.form
        if f.get("form") == "password":
            errors = []
            if not check_password_hash(user["password_hash"], f.get("current", "")):
                errors.append("Current password is incorrect.")
            if not valid_password(f.get("new", "")):
                errors.append("New password must be at least 6 characters with letters and numbers.")
            if f.get("new", "") != f.get("confirm", ""):
                errors.append("New passwords do not match.")
            if errors:
                for e in errors:
                    flash(e, "danger")
            else:
                execute("UPDATE users SET password_hash=%s WHERE id=%s",
                        (generate_password_hash(f["new"]), uid))
                flash("Password changed successfully.", "success")
                return redirect(url_for("user_profile"))
        else:
            name, phone, address = f.get("name", "").strip(), f.get("phone", "").strip(), f.get("address", "").strip()
            if len(name) < 3 or len(name) > 100:
                flash("Name must be 3 to 100 characters.", "danger")
            elif not PHONE_RE.match(phone):
                flash("Phone number must be exactly 10 digits.", "danger")
            else:
                execute("UPDATE users SET name=%s,phone=%s,address=%s WHERE id=%s",
                        (name, phone, address or None, uid))
                session["name"] = name
                flash("Profile updated.", "success")
                return redirect(url_for("user_profile"))
    stats = dict(total=count("SELECT COUNT(*) FROM assistance_requests WHERE user_id=%s", (uid,)),
                 vehicles=count("SELECT COUNT(*) FROM vehicles WHERE user_id=%s", (uid,)))
    user = query("SELECT * FROM users WHERE id=%s", (uid,), one=True)
    return render_template("user/profile.html", user=user, stats=stats)


# ---------------------------------------------------------------------------
# SERVICE PROVIDER MODULE
# ---------------------------------------------------------------------------
@app.route("/provider/dashboard")
@login_required("provider")
def provider_dashboard():
    pid = session["uid"]
    stats = dict(
        assigned=count("SELECT COUNT(*) FROM assistance_requests WHERE provider_id=%s AND status='Assigned'", (pid,)),
        active=count("SELECT COUNT(*) FROM assistance_requests WHERE provider_id=%s AND status IN "
                     "('Accepted','On the Way','In Progress')", (pid,)),
        completed=count("SELECT COUNT(*) FROM assistance_requests WHERE provider_id=%s AND status='Completed'", (pid,)),
        rating=query("SELECT ROUND(AVG(rating),1) AS a, COUNT(*) AS c FROM feedback WHERE provider_id=%s",
                     (pid,), one=True),
    )
    jobs = query(REQ_SELECT + " WHERE r.provider_id=%s AND r.status IN "
                 "('Assigned','Accepted','On the Way','In Progress') ORDER BY r.updated_at DESC", (pid,))
    recent = query(REQ_SELECT + " WHERE r.provider_id=%s AND r.status='Completed' "
                   "ORDER BY r.completed_at DESC LIMIT 5", (pid,))
    return render_template("provider/dashboard.html", stats=stats, jobs=jobs, recent=recent)


@app.route("/provider/requests")
@login_required("provider")
def provider_requests():
    status = request.args.get("status", "")
    sql = REQ_SELECT + " WHERE r.provider_id=%s AND r.status IN ('Assigned','Accepted','On the Way','In Progress')"
    args = [session["uid"]]
    if status in ("Assigned", "Accepted", "On the Way", "In Progress"):
        sql += " AND r.status=%s"
        args.append(status)
    rows = query(sql + " ORDER BY r.updated_at DESC", tuple(args))
    return render_template("provider/requests.html", requests=rows, status=status)


@app.route("/provider/requests/<int:rid>")
@login_required("provider")
def provider_request_details(rid):
    r = query(REQ_SELECT + " WHERE r.id=%s AND r.provider_id=%s", (rid, session["uid"]), one=True)
    if not r:
        abort(404)
    logs = query("SELECT * FROM request_logs WHERE request_id=%s ORDER BY id", (rid,))
    fb = query("SELECT * FROM feedback WHERE request_id=%s", (rid,), one=True)
    return render_template("provider/request_details.html", r=r, logs=logs, fb=fb)


@app.route("/provider/requests/<int:rid>/update", methods=["POST"])
@login_required("provider")
def provider_request_update(rid):
    pid = session["uid"]
    r = query("SELECT id,status FROM assistance_requests WHERE id=%s AND provider_id=%s", (rid, pid), one=True)
    if not r:
        abort(404)
    action = request.form.get("action", "")
    note = request.form.get("note", "").strip()[:200]
    if action == "decline" and r["status"] == "Assigned":
        execute("UPDATE assistance_requests SET provider_id=NULL, status='Pending' WHERE id=%s", (rid,))
        add_log(rid, "Pending", "Provider declined the job. Waiting for re-assignment.")
        flash("Request declined and returned to the admin.", "info")
        return redirect(url_for("provider_requests"))
    new_status = request.form.get("status", "")
    if new_status not in NEXT_STATUS.get(r["status"], []):
        flash("That status change is not allowed.", "danger")
        return redirect(url_for("provider_request_details", rid=rid))
    if new_status == "Completed":
        execute("UPDATE assistance_requests SET status='Completed', completed_at=NOW() WHERE id=%s", (rid,))
    else:
        execute("UPDATE assistance_requests SET status=%s WHERE id=%s", (new_status, rid))
    add_log(rid, new_status, note or "Status updated by service provider.")
    flash("Status updated to %s." % new_status, "success")
    if new_status == "Completed":
        return redirect(url_for("provider_completed"))
    return redirect(url_for("provider_request_details", rid=rid))


@app.route("/provider/completed")
@login_required("provider")
def provider_completed():
    rows = query(REQ_SELECT + " WHERE r.provider_id=%s AND r.status='Completed' "
                 "ORDER BY r.completed_at DESC", (session["uid"],))
    fb = {x["request_id"]: x for x in query("SELECT * FROM feedback WHERE provider_id=%s", (session["uid"],))}
    return render_template("provider/completed.html", requests=rows, fb=fb)


@app.route("/provider/profile", methods=["GET", "POST"])
@login_required("provider")
def provider_profile():
    pid = session["uid"]
    prov = query("SELECT * FROM service_providers WHERE id=%s", (pid,), one=True)
    if request.method == "POST":
        f = request.form
        if f.get("form") == "password":
            errors = []
            if not check_password_hash(prov["password_hash"], f.get("current", "")):
                errors.append("Current password is incorrect.")
            if not valid_password(f.get("new", "")):
                errors.append("New password must be at least 6 characters with letters and numbers.")
            if f.get("new", "") != f.get("confirm", ""):
                errors.append("New passwords do not match.")
            if errors:
                for e in errors:
                    flash(e, "danger")
            else:
                execute("UPDATE service_providers SET password_hash=%s WHERE id=%s",
                        (generate_password_hash(f["new"]), pid))
                flash("Password changed successfully.", "success")
                return redirect(url_for("provider_profile"))
        else:
            name, phone = f.get("name", "").strip(), f.get("phone", "").strip()
            city, spec = f.get("city", "").strip(), f.get("specialization", "").strip()
            if len(name) < 3 or len(name) > 100:
                flash("Name must be 3 to 100 characters.", "danger")
            elif not PHONE_RE.match(phone):
                flash("Phone number must be exactly 10 digits.", "danger")
            else:
                execute("UPDATE service_providers SET name=%s,phone=%s,city=%s,specialization=%s WHERE id=%s",
                        (name, phone, city[:80] or None, spec[:120] or None, pid))
                session["name"] = name
                flash("Profile updated.", "success")
                return redirect(url_for("provider_profile"))
    prov = query("SELECT * FROM service_providers WHERE id=%s", (pid,), one=True)
    stats = dict(completed=count("SELECT COUNT(*) FROM assistance_requests WHERE provider_id=%s AND status='Completed'", (pid,)),
                 rating=query("SELECT ROUND(AVG(rating),1) AS a, COUNT(*) AS c FROM feedback WHERE provider_id=%s",
                              (pid,), one=True))
    return render_template("provider/profile.html", prov=prov, stats=stats)


# ---------------------------------------------------------------------------
# ADMIN MODULE
# ---------------------------------------------------------------------------
def status_counts():
    data = {s: 0 for s in STATUSES}
    for row in query("SELECT status, COUNT(*) AS c FROM assistance_requests GROUP BY status"):
        data[row["status"]] = row["c"]
    return data


@app.route("/admin/dashboard")
@login_required("admin")
def admin_dashboard():
    sc = status_counts()
    stats = dict(
        users=count("SELECT COUNT(*) FROM users WHERE role='user'"),
        providers=count("SELECT COUNT(*) FROM service_providers"),
        total=sum(sc.values()), pending=sc["Pending"], accepted=sc["Accepted"],
        completed=sc["Completed"],
        active=sc["Assigned"] + sc["Accepted"] + sc["On the Way"] + sc["In Progress"],
        cancelled=sc["Cancelled"],
        rating=query("SELECT ROUND(AVG(rating),1) AS a FROM feedback", one=True)["a"],
    )
    recent = query(REQ_SELECT + " ORDER BY r.id DESC LIMIT 6")
    latest_fb = query("SELECT f.*, u.name AS user_name, r.request_code FROM feedback f "
                      "JOIN users u ON u.id=f.user_id JOIN assistance_requests r ON r.id=f.request_id "
                      "ORDER BY f.id DESC LIMIT 4")
    return render_template("admin/dashboard.html", stats=stats, sc=sc, recent=recent, latest_fb=latest_fb)


@app.route("/admin/users")
@login_required("admin")
def admin_users():
    rows = query("SELECT u.*, (SELECT COUNT(*) FROM assistance_requests r WHERE r.user_id=u.id) AS req_count, "
                 "(SELECT COUNT(*) FROM vehicles v WHERE v.user_id=u.id) AS veh_count "
                 "FROM users u WHERE u.role='user' ORDER BY u.id DESC")
    return render_template("admin/users.html", users=rows)


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@login_required("admin")
def admin_user_toggle(uid):
    u = query("SELECT id,status FROM users WHERE id=%s AND role='user'", (uid,), one=True)
    if not u:
        abort(404)
    new = "blocked" if u["status"] == "active" else "active"
    execute("UPDATE users SET status=%s WHERE id=%s", (new, uid))
    flash("User account is now %s." % new, "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@login_required("admin")
def admin_user_delete(uid):
    if not query("SELECT id FROM users WHERE id=%s AND role='user'", (uid,), one=True):
        abort(404)
    execute("DELETE FROM users WHERE id=%s AND role='user'", (uid,))
    flash("User and all linked records were deleted.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/providers")
@login_required("admin")
def admin_providers():
    rows = query("SELECT p.*, "
                 "(SELECT COUNT(*) FROM assistance_requests r WHERE r.provider_id=p.id AND r.status='Completed') AS done, "
                 "(SELECT COUNT(*) FROM assistance_requests r WHERE r.provider_id=p.id AND r.status IN "
                 "('Assigned','Accepted','On the Way','In Progress')) AS busy, "
                 "(SELECT ROUND(AVG(rating),1) FROM feedback f WHERE f.provider_id=p.id) AS rating "
                 "FROM service_providers p ORDER BY p.id DESC")
    return render_template("admin/providers.html", providers=rows)


def validate_provider(f, pid=None):
    errors = []
    name, email, phone = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("phone", "").strip()
    city, spec, pw = f.get("city", "").strip(), f.get("specialization", "").strip(), f.get("password", "")
    if len(name) < 3 or len(name) > 100:
        errors.append("Name must be 3 to 100 characters.")
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if not PHONE_RE.match(phone):
        errors.append("Phone number must be exactly 10 digits.")
    if pid is None and not valid_password(pw):
        errors.append("Password must be at least 6 characters with letters and numbers.")
    if pid is not None and pw and not valid_password(pw):
        errors.append("New password must be at least 6 characters with letters and numbers.")
    if not errors and query("SELECT id FROM service_providers WHERE email=%s AND id<>%s",
                            (email, pid or 0), one=True):
        errors.append("A provider with this email already exists.")
    return errors, (name, email, phone, city[:80] or None, spec[:120] or None, pw)


@app.route("/admin/providers/add", methods=["GET", "POST"])
@login_required("admin")
def admin_provider_add():
    if request.method == "POST":
        errors, d = validate_provider(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            execute("INSERT INTO service_providers (name,email,phone,city,specialization,password_hash) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", d[:5] + (generate_password_hash(d[5]),))
            flash("Service provider added.", "success")
            return redirect(url_for("admin_providers"))
    return render_template("admin/provider_form.html", prov=None)


@app.route("/admin/providers/<int:pid>/edit", methods=["GET", "POST"])
@login_required("admin")
def admin_provider_edit(pid):
    prov = query("SELECT * FROM service_providers WHERE id=%s", (pid,), one=True)
    if not prov:
        abort(404)
    if request.method == "POST":
        errors, d = validate_provider(request.form, pid)
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            execute("UPDATE service_providers SET name=%s,email=%s,phone=%s,city=%s,specialization=%s WHERE id=%s",
                    d[:5] + (pid,))
            if d[5]:
                execute("UPDATE service_providers SET password_hash=%s WHERE id=%s",
                        (generate_password_hash(d[5]), pid))
            flash("Service provider updated.", "success")
            return redirect(url_for("admin_providers"))
    return render_template("admin/provider_form.html", prov=prov)


@app.route("/admin/providers/<int:pid>/toggle", methods=["POST"])
@login_required("admin")
def admin_provider_toggle(pid):
    p = query("SELECT id,status FROM service_providers WHERE id=%s", (pid,), one=True)
    if not p:
        abort(404)
    new = "inactive" if p["status"] == "active" else "active"
    execute("UPDATE service_providers SET status=%s WHERE id=%s", (new, pid))
    flash("Provider is now %s." % new, "success")
    return redirect(url_for("admin_providers"))


@app.route("/admin/providers/<int:pid>/delete", methods=["POST"])
@login_required("admin")
def admin_provider_delete(pid):
    if not query("SELECT id FROM service_providers WHERE id=%s", (pid,), one=True):
        abort(404)
    execute("UPDATE assistance_requests SET status='Pending' WHERE provider_id=%s AND status IN "
            "('Assigned','Accepted','On the Way','In Progress')", (pid,))
    execute("DELETE FROM service_providers WHERE id=%s", (pid,))
    flash("Provider deleted. Their open requests returned to Pending.", "success")
    return redirect(url_for("admin_providers"))


@app.route("/admin/services")
@login_required("admin")
def admin_services():
    rows = query("SELECT s.*, (SELECT COUNT(*) FROM assistance_requests r WHERE r.service_id=s.id) AS req_count "
                 "FROM services s ORDER BY s.id")
    return render_template("admin/services.html", services=rows)


def validate_service(f, sid=None):
    errors = []
    name, desc = f.get("name", "").strip(), f.get("description", "").strip()
    icon, price = f.get("icon", ""), to_float(f.get("base_price"))
    if len(name) < 3 or len(name) > 80:
        errors.append("Service name must be 3 to 80 characters.")
    if len(desc) > 255:
        errors.append("Description must be within 255 characters.")
    if icon not in SERVICE_ICONS:
        errors.append("Select a valid icon.")
    if price is None or price < 0 or price > 1000000:
        errors.append("Enter a valid base price.")
    if not errors and query("SELECT id FROM services WHERE name=%s AND id<>%s", (name, sid or 0), one=True):
        errors.append("A service with this name already exists.")
    return errors, (name, desc or None, icon, price, 1 if f.get("is_active") else 0)


@app.route("/admin/services/add", methods=["GET", "POST"])
@login_required("admin")
def admin_service_add():
    if request.method == "POST":
        errors, d = validate_service(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            execute("INSERT INTO services (name,description,icon,base_price,is_active) VALUES (%s,%s,%s,%s,%s)", d)
            flash("Service added.", "success")
            return redirect(url_for("admin_services"))
    return render_template("admin/service_form.html", svc=None)


@app.route("/admin/services/<int:sid>/edit", methods=["GET", "POST"])
@login_required("admin")
def admin_service_edit(sid):
    svc = query("SELECT * FROM services WHERE id=%s", (sid,), one=True)
    if not svc:
        abort(404)
    if request.method == "POST":
        errors, d = validate_service(request.form, sid)
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            execute("UPDATE services SET name=%s,description=%s,icon=%s,base_price=%s,is_active=%s WHERE id=%s",
                    d + (sid,))
            flash("Service updated.", "success")
            return redirect(url_for("admin_services"))
    return render_template("admin/service_form.html", svc=svc)


@app.route("/admin/services/<int:sid>/toggle", methods=["POST"])
@login_required("admin")
def admin_service_toggle(sid):
    s = query("SELECT id,is_active FROM services WHERE id=%s", (sid,), one=True)
    if not s:
        abort(404)
    execute("UPDATE services SET is_active=%s WHERE id=%s", (0 if s["is_active"] else 1, sid))
    flash("Service visibility changed.", "success")
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:sid>/delete", methods=["POST"])
@login_required("admin")
def admin_service_delete(sid):
    if not query("SELECT id FROM services WHERE id=%s", (sid,), one=True):
        abort(404)
    try:
        execute("DELETE FROM services WHERE id=%s", (sid,))
        flash("Service deleted.", "success")
    except mysql.connector.IntegrityError:
        flash("This service already has requests. Disable it instead of deleting.", "warning")
    return redirect(url_for("admin_services"))


@app.route("/admin/requests")
@login_required("admin")
def admin_requests():
    status = request.args.get("status", "")
    sql, args = REQ_SELECT, ()
    if status in STATUSES:
        sql, args = sql + " WHERE r.status=%s", (status,)
    rows = query(sql + " ORDER BY r.id DESC", args)
    return render_template("admin/requests.html", requests=rows, status=status, sc=status_counts())


@app.route("/admin/requests/<int:rid>")
@login_required("admin")
def admin_request_details(rid):
    r = query(REQ_SELECT + " WHERE r.id=%s", (rid,), one=True)
    if not r:
        abort(404)
    logs = query("SELECT * FROM request_logs WHERE request_id=%s ORDER BY id", (rid,))
    fb = query("SELECT * FROM feedback WHERE request_id=%s", (rid,), one=True)
    providers = query("SELECT id,name,city,specialization FROM service_providers WHERE status='active' ORDER BY name")
    return render_template("admin/request_details.html", r=r, logs=logs, fb=fb, providers=providers)


@app.route("/admin/requests/<int:rid>/assign", methods=["POST"])
@login_required("admin")
def admin_assign(rid):
    r = query("SELECT id,status FROM assistance_requests WHERE id=%s", (rid,), one=True)
    if not r:
        abort(404)
    pid = to_int(request.form.get("provider_id"))
    prov = query("SELECT id,name FROM service_providers WHERE id=%s AND status='active'", (pid,), one=True)
    if r["status"] not in ("Pending", "Assigned"):
        flash("Only Pending or Assigned requests can be (re)assigned.", "warning")
    elif not prov:
        flash("Please select an active service provider.", "danger")
    else:
        execute("UPDATE assistance_requests SET provider_id=%s, status='Assigned' WHERE id=%s", (pid, rid))
        add_log(rid, "Assigned", "Assigned to %s by admin." % prov["name"])
        flash("Request assigned to %s." % prov["name"], "success")
    return redirect(safe_next(url_for("admin_request_details", rid=rid)))


@app.route("/admin/requests/<int:rid>/cancel", methods=["POST"])
@login_required("admin")
def admin_request_cancel(rid):
    r = query("SELECT id,status FROM assistance_requests WHERE id=%s", (rid,), one=True)
    if not r:
        abort(404)
    if r["status"] in ("Completed", "Cancelled"):
        flash("This request is already closed.", "warning")
    else:
        execute("UPDATE assistance_requests SET status='Cancelled' WHERE id=%s", (rid,))
        add_log(rid, "Cancelled", "Cancelled by admin.")
        flash("Request cancelled.", "success")
    return redirect(url_for("admin_request_details", rid=rid))


@app.route("/admin/assignments")
@login_required("admin")
def admin_assignments():
    unassigned = query(REQ_SELECT + " WHERE r.status='Pending' ORDER BY r.id")
    assigned = query(REQ_SELECT + " WHERE r.provider_id IS NOT NULL AND r.status IN "
                     "('Assigned','Accepted','On the Way','In Progress') ORDER BY r.updated_at DESC")
    providers = query("SELECT id,name,city FROM service_providers WHERE status='active' ORDER BY name")
    return render_template("admin/assignments.html", unassigned=unassigned, assigned=assigned, providers=providers)


@app.route("/admin/feedback")
@login_required("admin")
def admin_feedback():
    rating = to_int(request.args.get("rating"))
    sql = ("SELECT f.*, u.name AS user_name, r.request_code, s.name AS service_name, p.name AS provider_name "
           "FROM feedback f JOIN users u ON u.id=f.user_id "
           "JOIN assistance_requests r ON r.id=f.request_id JOIN services s ON s.id=r.service_id "
           "LEFT JOIN service_providers p ON p.id=f.provider_id")
    args = ()
    if rating and 1 <= rating <= 5:
        sql, args = sql + " WHERE f.rating=%s", (rating,)
    rows = query(sql + " ORDER BY f.id DESC", args)
    summary = query("SELECT COUNT(*) AS total, ROUND(AVG(rating),1) AS avg_rating FROM feedback", one=True)
    dist = {i: 0 for i in range(1, 6)}
    for row in query("SELECT rating, COUNT(*) AS c FROM feedback GROUP BY rating"):
        dist[row["rating"]] = row["c"]
    awaiting = count("SELECT COUNT(*) FROM assistance_requests r WHERE r.status='Completed' "
                     "AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.request_id=r.id)")
    return render_template("admin/feedback.html", feedback=rows, summary=summary, dist=dist,
                           awaiting=awaiting, rating=rating)


@app.route("/admin/database")
@login_required("admin")
def admin_database():
    table = request.args.get("table", "")
    overview = [dict(name=t, rows=count("SELECT COUNT(*) FROM `%s`" % t)) for t in DB_TABLES]
    info = dict(version=query("SELECT VERSION() AS v", one=True)["v"], name=app.config["DB_NAME"],
                host=app.config["DB_HOST"], port=app.config["DB_PORT"])
    columns, rows = [], []
    if table in DB_TABLES:
        columns = [c["Field"] for c in query("SHOW COLUMNS FROM `%s`" % table) if c["Field"] != "password_hash"]
        rows = query("SELECT %s FROM `%s` ORDER BY id DESC LIMIT 200" % (", ".join("`%s`" % c for c in columns), table))
    else:
        table = ""
    return render_template("admin/database.html", overview=overview, info=info,
                           table=table, columns=columns, rows=rows)


@app.route("/admin/database/delete", methods=["POST"])
@login_required("admin")
def admin_database_delete():
    table, rec = request.form.get("table", ""), to_int(request.form.get("id"))
    if table not in DB_TABLES or rec is None:
        abort(400)
    if table == "users":
        u = query("SELECT role FROM users WHERE id=%s", (rec,), one=True)
        if u and u["role"] == "admin":
            flash("The administrator account cannot be deleted.", "danger")
            return redirect(url_for("admin_database", table=table))
    try:
        execute("DELETE FROM `%s` WHERE id=%%s" % table, (rec,))
        flash("Record #%d deleted from %s." % (rec, table), "success")
    except mysql.connector.IntegrityError:
        flash("Record is referenced by other tables and cannot be deleted.", "warning")
    return redirect(url_for("admin_database", table=table))


@app.route("/admin/database/export/<table>")
@login_required("admin")
def admin_database_export(table):
    if table not in DB_TABLES:
        abort(404)
    cols = [c["Field"] for c in query("SHOW COLUMNS FROM `%s`" % table) if c["Field"] != "password_hash"]
    rows = query("SELECT %s FROM `%s` ORDER BY id" % (", ".join("`%s`" % c for c in cols), table))
    return csv_response(table, cols, rows)


def csv_response(name, cols, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for row in rows:
        writer.writerow([row[c] for c in cols])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=%s_%s.csv" % (
                        name, datetime.now().strftime("%Y%m%d"))})


@app.route("/admin/reports")
@login_required("admin")
def admin_reports():
    sc = status_counts()
    per_service = query("SELECT s.name, COUNT(r.id) AS c FROM services s "
                        "LEFT JOIN assistance_requests r ON r.service_id=s.id GROUP BY s.id, s.name ORDER BY s.id")
    monthly = query("SELECT YEAR(created_at) AS y, MONTH(created_at) AS m, COUNT(*) AS c "
                    "FROM assistance_requests WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH) "
                    "GROUP BY YEAR(created_at), MONTH(created_at) ORDER BY y, m")
    top = query("SELECT p.name, p.city, COUNT(r.id) AS done, "
                "(SELECT ROUND(AVG(f.rating),1) FROM feedback f WHERE f.provider_id=p.id) AS rating "
                "FROM service_providers p "
                "LEFT JOIN assistance_requests r ON r.provider_id=p.id AND r.status='Completed' "
                "GROUP BY p.id, p.name, p.city ORDER BY done DESC LIMIT 5")
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    monthly_labels = ["%s %d" % (month_names[m["m"] - 1], m["y"]) for m in monthly]
    totals = dict(requests=sum(sc.values()),
                  users=count("SELECT COUNT(*) FROM users WHERE role='user'"),
                  providers=count("SELECT COUNT(*) FROM service_providers"),
                  rating=query("SELECT ROUND(AVG(rating),1) AS a FROM feedback", one=True)["a"])
    return render_template("admin/reports.html", sc=sc, per_service=per_service, monthly=monthly,
                           monthly_labels=monthly_labels, top=top, totals=totals)


@app.route("/admin/reports/export")
@login_required("admin")
def admin_reports_export():
    cols = ["request_code", "user_name", "service_name", "make_model", "reg_number", "location",
            "provider_name", "status", "created_at", "completed_at"]
    rows = query(REQ_SELECT + " ORDER BY r.id")
    return csv_response("requests", cols, rows)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        bootstrap_database()
        print("Database ready. Admin: %s / %s | Demo provider: provider@roadrescue.com / Provider@123" % (
            app.config["ADMIN_EMAIL"], app.config["ADMIN_PASSWORD"]))
    except mysql.connector.Error as exc:
        print("\nCould not connect to MySQL: %s" % exc)
        print("1) Open XAMPP Control Panel and START MySQL.")
        print("2) Check DB_HOST / DB_USER / DB_PASSWORD in config.py.\n")
        sys.exit(1)
    app.run(debug=True, use_reloader=False)
