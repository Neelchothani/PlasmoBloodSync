# app.py (full file)
from flask import Flask, render_template, request, redirect, url_for, session, flash, render_template_string, jsonify
from markupsafe import Markup
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import os
import re
import json
from typing import List, Dict, Tuple, Optional
import uuid
from geopy.distance import geodesic
import firebase_admin
from firebase_admin import credentials, firestore
import pytz
from PIL import Image, ImageFilter, ImageOps
import pytesseract
from docx import Document
import fitz  # PyMuPDF
from fuzzywuzzy import fuzz
from typing import List, Tuple, Dict, Optional
# === Google OAuth ===
from authlib.integrations.flask_client import OAuth
import math
from firebase_admin import messaging
import smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# Allow HTTP for local OAuth testing (never do this in production)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# -----------------------------------------
# Firebase init
# -----------------------------------------
load_dotenv(dotenv_path="1.env")
firebase_json_str = os.environ.get("FIREBASE_KEY")
if not firebase_json_str:
    raise RuntimeError("FIREBASE_KEY environment variable is not set!")

# Convert string to dict
firebase_json = json.loads(firebase_json_str)

# Initialize Firebase
cred = credentials.Certificate(firebase_json)
firebase_admin.initialize_app(cred)

app = Flask(__name__)
app.secret_key = "plasmo_secret_key"  # consider moving to env var

# Session cookie settings (good defaults for localhost)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # True in production with HTTPS
)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# -----------------------------------------
# Google OAuth config
# -----------------------------------------
app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID")
app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET")
EMAIL_ADDRESS = "neelchothani9417@gmail.com"      # Replace with your sender email
EMAIL_PASSWORD = "kfkq gibg zsis xfao"
# Comma-separated admin email overrides, e.g. "admin@plasmo.com,owner@acme.com"
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "admin@plasmo.com").split(",") if e.strip()}

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# -----------------------------------------
# Demo users (seed-only; not used for auth)
# -----------------------------------------
users = {
    "admin@plasmo.com": {"username": "admin", "password": "admin123", "role": "admin"},
    "user@plasmo.com": {"username": "user123", "password": "user123", "role": "user"},
}

# =========================================
# ---------- ADVANCED HELPERS -------------
# =========================================

DATE_FORMATS = [
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"
]

HOSPITAL_LIKE_WORDS = [
    "hospital", "clinic", "medical center", "medical centre", "diagnostic",
    "pathology", "imaging", "blood bank", "medical college", "institute of medical"
]

DOCTOR_LIKE_WORDS = [
    "dr.", "doctor", "consultant", "pathologist", "mbbs", "md", "dnb",
    "mci", "reg no", "registration no", "regn", "license no", "state council"
]

ACCREDITATION_WORDS = ["nabl", "iso 15189", "cap accredited", "nabld", "nabcb"]

ID_WORDS = ["uhid", "mrn", "crn", "report id", "report no", "sample id", "bill no", "receipt no", "lab no"]

RESULT_WORDS = [
    "result", "reference range", "units", "non-reactive", "non reactive", "reactive",
    "positive", "negative", "within normal limits", "wnl", "interpretation"
]

SIGNATURE_WORDS = ["signature", "authorised sign", "authorized sign", "seal", "stamp", "verified by"]

CONTACT_PATTERNS = [
    r"\b(?:\+?\d{1,4}[- ]?)?\d{10}\b",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"\bhttps?://[^\s]+",
]

SUSPICIOUS_WORDS = ["sample report", "demo", "template", "lorem ipsum", "for training", "specimen"]

INFECTIOUS_MARKERS = [
    "hiv", "hiv 1", "hiv 2", "hiv1", "hiv2",
    "hbsag", "hepatitis b", "hepatitis c", "hcv",
    "syphilis", "vdrl", "malaria", "plasmodium", "tb", "tuberculosis", "covid", "sars-cov-2"
]

NEGATION_PATTERNS = [
    r"no", r"not", r"none", r"without", r"free from", r"negative", r"non[-\s]?reactive",
    r"absent", r"undetected", r"not detected", r"non detected", r"non-detected", r"nonreactive",
    r"nr", r"non reactive for", r"<\s*limit", r"below detection", r"undetectable", r"nil"
]

POSITIVE_WORDS = ["positive", "reactive", "detected", "present"]


def require_login():
    if 'email' not in session:
        return Markup("""<script>alert('Please sign in to continue');window.location.href = '/signin';</script>""")
    return None

def get_patient_collection():
    return f"{session.get('donationType', 'blood')}_donors"

def get_request_collection():
    return f"{session.get('donationType', 'blood')}_requests"

# -------- OCR & TEXT EXTRACTION ----------

def preprocess_image_for_ocr(img: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray

def extract_text_from_image(path: str) -> str:
    try:
        image = Image.open(path)
        image = preprocess_image_for_ocr(image)
        return pytesseract.image_to_string(image, config="--psm 6")
    except Exception as e:
        return f"ERROR: {str(e)}"

def extract_text_from_pdf(path: str) -> Tuple[str, Dict[str, str]]:
    text_blocks: List[str] = []
    meta = {}
    try:
        with fitz.open(path) as doc:
            meta = doc.metadata or {}
            for page in doc:
                blocks = page.get_text("blocks")
                for b in blocks:
                    if len(b) >= 5 and isinstance(b[4], str):
                        blk_txt = b[4].strip()
                        if blk_txt:
                            text_blocks.append(blk_txt)
        return "\n\n".join(text_blocks), meta
    except Exception as e:
        return f"ERROR: {str(e)}", meta

def extract_text_from_docx(path: str) -> str:
    try:
        doc = Document(path)
        return "\n".join([para.text for para in doc.paragraphs])
    except Exception as e:
        return f"ERROR: {str(e)}"

def extract_text_from_file(file_path: str) -> Tuple[str, Dict[str, str]]:
    ext = os.path.splitext(file_path)[-1].lower()
    text = ""
    meta = {}
    if ext in ['.jpg', '.jpeg', '.png']:
        text = extract_text_from_image(file_path)
    elif ext == '.docx':
        text = extract_text_from_docx(file_path)
    elif ext == '.pdf':
        text, meta = extract_text_from_pdf(file_path)
    else:
        text = ""
    if text is None:
        text = ""
    return text.lower(), meta

# -------- AUTHENTICITY ASSESSMENT --------

def find_dates(text: str) -> List[datetime]:
    patterns = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{2}[/-]\d{2}[/-]\d{4}\b",
        r"\b\d{2}\s+[A-Za-z]{3,}\s+\d{4}\b",
        r"\b[A-Za-z]{3,}\s+\d{1,2},\s+\d{4}\b",
        r"\b\d{1,2}\.\d{1,2}\.\d{4}\b",
    ]
    raw = []
    for p in patterns:
        raw.extend(re.findall(p, text, flags=re.IGNORECASE))
    dates = []
    for token in raw:
        for fmt in DATE_FORMATS:
            try:
                dates.append(datetime.strptime(token, fmt))
                break
            except Exception:
                continue
    return dates

def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[\s\-_]+", " ", text)
    return text

def fuzzy_has_any(text: str, words: List[str], threshold: int = 80) -> bool:
    for w in words:
        if fuzz.partial_ratio(w.lower(), text.lower()) >= threshold:
            return True
    return False

def count_matches(text: str, words: List[str]) -> int:
    return sum(1 for w in words if w in text.lower())

def any_regex(text: str, patterns: List[str]) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)

def tokenize_sentences(text: str) -> List[str]:
    return re.split(r"[.\n]+", text)

def has_negation_near_extended(line: str, term: str, window: int = 7) -> bool:
    words = re.findall(r"[a-zA-Z0-9\-\+/]+", line.lower())
    term_positions = [i for i, w in enumerate(words) if term in w]
    neg_positions = [i for i, w in enumerate(words) if any(re.fullmatch(p, w) for p in NEGATION_PATTERNS)]
    for tp in term_positions:
        for np in neg_positions:
            if abs(tp - np) <= window:
                return True
    return False

def has_positive_marker(term_line: str, term: str) -> bool:
    return re.search(rf"{term}[^a-zA-Z0-9]+(?:reactive|positive|detected)", term_line, re.IGNORECASE) is not None

def authenticity_score(full_text: str, pdf_meta: Optional[Dict[str, str]] = None) -> Tuple[str, int, Dict[str, int]]:
    score = 0
    reasons: Dict[str, int] = {}

    hospital_hits = count_matches(full_text, HOSPITAL_LIKE_WORDS)
    score += hospital_hits * 2
    reasons["facility"] = hospital_hits * 2

    doctor_hits = count_matches(full_text, DOCTOR_LIKE_WORDS)
    score += doctor_hits * 2
    reasons["doctor"] = doctor_hits * 2

    id_hits = count_matches(full_text, ID_WORDS)
    score += id_hits * 2
    reasons["ids"] = id_hits * 2

    res_hits = count_matches(full_text, RESULT_WORDS)
    score += res_hits
    reasons["results"] = res_hits

    sig_hits = count_matches(full_text, SIGNATURE_WORDS)
    score += sig_hits
    reasons["signature"] = sig_hits

    contact_hits = sum(1 for p in CONTACT_PATTERNS if re.search(p, full_text))
    score += contact_hits
    reasons["contact"] = contact_hits

    acc_hits = count_matches(full_text, ACCREDITATION_WORDS)
    score += acc_hits * 2
    reasons["accreditation"] = acc_hits * 2

    meta_bonus = 0
    if pdf_meta:
        for k in ["producer", "creator", "author", "title"]:
            if pdf_meta.get(k):
                meta_bonus += 1
        meta_json = json.dumps(pdf_meta).lower()
        if any(sw in meta_json for sw in ["template", "sample", "demo"]):
            meta_bonus -= 2
    score += meta_bonus
    reasons["pdf_meta"] = meta_bonus

    sus_penalty = 0
    for sw in SUSPICIOUS_WORDS:
        if sw in full_text:
            sus_penalty -= 3
    score += sus_penalty
    reasons["suspicious"] = sus_penalty

    if score >= 10:
        label = "authentic"
    elif score >= 7:
        label = "likely authentic"
    elif score >= 4:
        label = "uncertain"
    else:
        label = "suspicious"

    return label, score, reasons

# -------- ELIGIBILITY (CONTEXT-AWARE) ----


def send_request_notification(user, patient_info):
    message = messaging.Message(
        notification=messaging.Notification(
            title="New Blood Request Nearby",
            body=f"Patient: {patient_info['name']}, Blood Group: {patient_info['blood_group']}"
        ),
        token=user['fcm_token']
    )
    response = messaging.send(message)
    print('Successfully sent message:', response)


def has_red_flags_context_aware(full_text: str) -> Tuple[bool, List[str]]:
    reasons = []
    sentences = tokenize_sentences(full_text)

    for sent in sentences:
        line = sent.strip().lower()
        if not line:
            continue

        for term in INFECTIOUS_MARKERS:
            if term in line:
                negated = has_negation_near_extended(line, term)
                pos = has_positive_marker(line, term)
                if pos and not negated:
                    reasons.append(f"{term.upper()} marked positive/reactive/detected.")
                    return True, reasons
                if not negated and not re.search(r"(screening|rule out|history)", line):
                    reasons.append(f"{term.upper()} mentioned without negation.")
                    return True, reasons

    return False, reasons

def extract_basic_fields(full_text: str) -> Dict[str, Optional[str]]:
    """
    Extracts basic patient info from report text in a robust way.
    Handles OCR artifacts, line breaks, and multiple formats.
    """
    # Normalize text: lowercase, replace newlines, remove extra spaces
    text = full_text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()

    fields = {"patient_name": None, "age": None, "gender": None, "blood_group": None}

    # --- Patient Name ---
    # Matches: Patient Name: John Doe OR Name - John Doe OR Patient Name John Doe
    name_match = re.search(
        r"(?:patient\s*name|name)\s*[:\-]?\s*([A-Za-z\s\.\-']{2,50})",
        text,
        re.IGNORECASE
    )
    if name_match:
        name = name_match.group(1).strip()
        # Fix multiple spaces, title case
        name = re.sub(r"\s+", " ", name).title()
        fields["patient_name"] = name

    # --- Age ---
    # Matches: Age: 25 OR Age - 25 OR Age 25 yrs
    age_match = re.search(r"\bage\s*[:\-]?\s*(\d{1,3})\b", text, re.IGNORECASE)
    if age_match:
        fields["age"] = age_match.group(1)

    # --- Gender ---
    # Matches: Gender: Male / Female / M / F / Other
    gender_match = re.search(r"\b(sex|gender)\s*[:\-]?\s*(male|female|m|f|other)\b", text, re.IGNORECASE)
    if gender_match:
        gender = gender_match.group(2).lower()
        if gender in ["m", "male"]:
            fields["gender"] = "MALE"
        elif gender in ["f", "female"]:
            fields["gender"] = "FEMALE"
        else:
            fields["gender"] = gender.upper()

    # --- Blood Group ---
    # Matches: Blood Group: A+ / B- / O + / AB- etc.
    bg_match = re.search(r"\b(blood\s*group|blood\s*type)\s*[:\-]?\s*([ABO]{1,2}\s*[\+\-])\b", text, re.IGNORECASE)
    if bg_match:
        bg = bg_match.group(2).replace(" ", "").upper()
        fields["blood_group"] = bg

    return fields

def evaluate_non_document_criteria(age: int, weight: int, days_since: int) -> Tuple[bool, List[str]]:
    reasons = []
    ok = True
    if age < 18:
        ok = False
        reasons.append("Age < 18.")
    if weight < 50:
        ok = False
        reasons.append("Weight < 50 kg.")
    if days_since < 90:
        ok = False
        reasons.append(f"Last donation only {days_since} days ago (< 90).")
    return ok, reasons

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(delta_lambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def get_verified_users_from_firestore():
    users_list = []
    for doc in db.collection("users").stream():
        data = doc.to_dict()
        if data.get("fcm_token") and data.get("role") == "user":
            location = data.get("location", {"lat":0, "lng":0})
            data["location"] = location
            users_list.append(data)
    return users_list

# =========================================
# ---------------- ROUTES -----------------
# =========================================

@app.route("/")
def home():
    if 'role' in session and session['role'] == 'admin':
        return redirect(url_for('admin_home'))
    return render_template("index.html")


@app.route("/eligibility")
def eligibility():
    login_check = require_login()
    if login_check:
        return login_check
    return render_template("eligibility.html")


@app.route('/check-eligibility', methods=['POST'])
def check_eligibility():
    login_check = require_login()
    if login_check:
        return login_check

    try:
        # --- Basic criteria ---
        age = int(request.form['age'])
        weight = int(request.form['weight'])
        last_donation = datetime.strptime(request.form['last_donation'], '%Y-%m-%d')
        days_since = (datetime.today() - last_donation).days

        document = request.files.get('document')
        citizenship = request.files.get('citizenship_proof')

        doc_text, doc_meta = ("", {})
        id_text, id_meta = ("", {})

        # --- Extract text from uploaded files ---
        if document and document.filename:
            doc_path = os.path.join(app.config['UPLOAD_FOLDER'], f"doc_{datetime.now().timestamp()}_{document.filename}")
            document.save(doc_path)
            doc_text, doc_meta = extract_text_from_file(doc_path)

        if citizenship and citizenship.filename:
            id_path = os.path.join(app.config['UPLOAD_FOLDER'], f"id_{datetime.now().timestamp()}_{citizenship.filename}")
            citizenship.save(id_path)
            id_text, id_meta = extract_text_from_file(id_path)

        full_text = normalize_text((doc_text or "") + "\n" + (id_text or ""))

        # --- Authenticity ---
        label, score, _ = authenticity_score(full_text, doc_meta if doc_meta else id_meta)
        authenticity_ok = label in ["authentic", "likely authentic", "uncertain"]

        # --- Red flags ---
        has_red, _ = has_red_flags_context_aware(full_text)

        # --- Non-document criteria ---
        base_ok, _ = evaluate_non_document_criteria(age, weight, days_since)

        # --- Final Eligibility ---
        if base_ok and authenticity_ok and not has_red:
            flash("✅ Eligible", "success")
            return redirect(url_for('eligibility'))
        else:
            # Flash ineligible message
            flash("❌ You are not eligible to donate. Redirecting shortly...", "error")
            return render_template(url_for('not_eligibile'))


    except Exception as e:
        return render_template("not_eligible.html")

# ---------------- Donor/Register ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    login_check = require_login()
    if login_check: return login_check

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        phone = request.form["phone"]
        blood_group = request.form["blood_group"]
        last_donation = request.form["last_donation"]

        collection = get_patient_collection()
        db.collection(collection).add({
            "name": name,
            "email": email,
            "phone": phone,
            "blood_group": blood_group,
            "last_donation": last_donation
        })

        return redirect(url_for("admin" if session.get("role") == "admin" else "dashboard"))

    return render_template("register.html")

@app.route("/request-blood", methods=["GET", "POST"])
def request_blood():
    login_check = require_login()
    if login_check: return login_check

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        phone = request.form["phone"]
        blood_type = request.form["blood_type"]
        location = request.form["location"]

        collection = get_request_collection()
        db.collection(collection).add({
            "name": name,
            "email": email,
            "phone": phone,
            "blood_type": blood_type,
            "location": location,
            "date": datetime.now().strftime("%Y-%m-%d")
        })

        return redirect(url_for("admin" if session.get("role") == "admin" else "dashboard"))

    return render_template("request_blood.html")

from datetime import datetime, timezone

@app.route("/admin")
def admin_home():
    login_check = require_login()
    if login_check:
        return login_check

    if session.get("role") != "admin":
        flash("Access denied. Redirecting to dashboard.", "error")
        return redirect(url_for("dashboard"))

    request_list = []
    accepted_donors = []
    urgent_count = 0
    blood_counts = {}
    daily_counts = defaultdict(int)

    try:
        users = db.collection("users").where("role", "==", "user").get()
        total_donors_count = len(users)

        for doc in db.collection(get_request_collection()).stream():
            r = doc.to_dict()

            # Skip incomplete records
            if not r.get("patient_name") or not r.get("blood_group"):
                continue

            # ✅ Fix: use Firestore timestamp or parse saved string
            created_at = r.get("created_at")
            if created_at:
                try:
                    if hasattr(created_at, "timestamp"):  # Firestore Timestamp
                        r["created_at_dt"] = created_at.replace(tzinfo=timezone.utc)
                    else:
                        r["created_at_dt"] = datetime.fromisoformat(str(created_at).replace("Z", "")).replace(tzinfo=timezone.utc)
                except Exception:
                    r["created_at_dt"] = None
            else:
                r["created_at_dt"] = None

            # formatted string
            r["created_at_str"] = r["created_at_dt"].strftime("%Y-%m-%d")

            request_list.append(r)

            bt = r.get("blood_group") or r.get("blood_type")
            if bt:
                blood_counts[bt] = blood_counts.get(bt, 0) + 1
                if bt == "A+":
                    urgent_count += 1

            if r["created_at_dt"]:
                daily_counts[r["created_at_dt"].strftime("%a")] += 1

            # ✅ Fix: accepted donor blood group and timestamp
            accepted = r.get("accepted_donor")
            if accepted:
                donor_blood_group = accepted.get("blood_group")
                if not donor_blood_group or donor_blood_group.lower() == "unknown":
                    donor_blood_group = r.get("blood_group")

                accepted_donors.append({
                    "name": accepted.get("name"),
                    "blood_group": donor_blood_group,
                    "phone": accepted.get("phone"),
                    "patient_name": r.get("patient_name"),
                    "created_at_dt": r["created_at_dt"],
                    "created_at_str": r["created_at_str"]
                })

    except Exception as e:
        print("Error fetching admin data:", e)

    # ✅ Sort both lists by date (latest first)
    request_list = sorted(
        [r for r in request_list if r.get("created_at_dt")],
        key=lambda x: x["created_at_dt"],
        reverse=True
    )

    accepted_donors = sorted(
        [a for a in accepted_donors if a.get("created_at_dt")],
        key=lambda x: x["created_at_dt"],
        reverse=True
    )

    # Keep just top 4 for preview widgets
    latest_requests = request_list[:4]
    latest_accepted = accepted_donors[:4]

    stats = {
        "total_donors": total_donors_count,
        "total_requests": len(request_list),
        "urgent_requests": urgent_count
    }

    return render_template(
        "admin.html",
        stats=stats,
        requests=request_list,
        latest_requests=latest_requests,
        accepted_donors=accepted_donors,
        latest_accepted=latest_accepted,
        blood_counts=blood_counts,
        daily_counts=daily_counts
    )




@app.route("/dashboard")
def dashboard():
    login_check = require_login()
    if login_check:
        return login_check

    role = session.get("role")
    if role in ["user", "donor"]:
        return render_template("dashboard.html", request=request)
    elif role == "admin":
        return redirect(url_for("admin"))

    return redirect(url_for("signin"))


# ---------------- GOOGLE SIGN-IN ----------------

# Dev fallback routes (helpful when GOOGLE_CLIENT_ID / SECRET are not set during local testing)
@app.route("/dev-login")
def dev_login():
    """
    Development-only page (visible when app.debug is True) that lets you
    quickly sign in as a demo admin or demo user. This helps when you
    haven't set up Google credentials locally.
    """
    if not app.debug:
        flash("Dev login is disabled.", "error")
        return redirect(url_for("signin"))

    html = """
    <!doctype html>
    <html>
      <head><title>Dev Login - PlasmoBlood Sync</title></head>
      <body style="font-family: sans-serif; padding: 20px;">
        <h2>Development Login (debug only)</h2>
        <p>Click to sign in as a demo user:</p>
        <ul>
          <li><a href="/dev-auth?email=admin@plasmo.com">Sign in as admin@plasmo.com (admin)</a></li>
          <li><a href="/dev-auth?email=user@plasmo.com">Sign in as user@plasmo.com (user)</a></li>
          <li><a href="/dev-auth?email=custom@example.com">Sign in as custom@example.com (defaults to user)</a></li>
        </ul>
        <p><a href="/signin">Back to Sign In</a></p>
      </body>
    </html>
    """
    return render_template_string(html)

@app.route("/dev-auth")
def dev_auth():
    """
    Create a session for the provided ?email=... (debug only).
    This simulates a Google sign-in locally so you can test flows without real OAuth.
    """
    if not app.debug:
        flash("Dev auth is disabled.", "error")
        return redirect(url_for("signin"))

    email = (request.args.get("email") or "").lower()
    if not email:
        flash("No email provided for dev auth.", "error")
        return redirect(url_for("dev_login"))

    # Determine role from demo users or ADMIN_EMAILS
    if email in users:
        role = users[email].get("role", "user")
        name = users[email].get("username", email.split("@")[0])
    else:
        role = "admin" if email in ADMIN_EMAILS else "user"
        name = email.split("@")[0]

    # Persist minimal profile to Firestore (mirror real flow)
    try:
        db.collection("users").document(email).set({
            "email": email,
            "name": name,
            "picture": "",
            "role": role,
            "last_login": datetime.utcnow().isoformat() + "Z"
        }, merge=True)
    except Exception as e:
        # If Firestore fails in dev (e.g. quota), still allow local session
        print("Warning: Firestore write failed during dev-auth:", e)

    session["email"] = email
    session["username"] = name
    session["role"] = role

    flash(f"Dev-signed in as {email} ({role}).", "success")
    if role == "admin":
        return redirect(url_for("admin_home"))
    return redirect(url_for("dashboard"))

# ---------------- AUTH ROUTES ----------------

@app.route("/signin", methods=["GET", "POST"])
def signin():
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Please provide both email and password.", "error")
            return redirect(url_for("signin"))

        user_ref = db.collection("users").document(email)
        user_doc = user_ref.get()
        if not user_doc.exists:
            flash("Email not found. Please sign up first.", "error")
            return redirect(url_for("signin"))

        user = user_doc.to_dict()
        if password != user.get("password"):  # ⚠️ hash in production
            flash("Incorrect password.", "error")
            return redirect(url_for("signin"))

        # Set session
        session["email"] = email
        session["username"] = user.get("name", email.split("@")[0])
        session["role"] = user.get("role", "user")
        session["picture"] = user.get("picture")

        flash("Signed in successfully ✔️", "success")

        if session["role"] == "admin":
            return redirect(url_for("admin_home"))
        else:
            return redirect(url_for("dashboard"))

    return render_template("signin.html")






@app.route("/signup", methods=["GET", "POST"])
def signup():
    """
    GET  -> show signup page
    POST -> handle manual signup
    """
    if request.method == "POST":
        name = request.form.get("name", "")
        email = request.form.get("email", "").lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")  # <-- NEW: capture selected role

        if not name or not email or not password or not role:
            flash("All fields are required, including role.", "error")
            return redirect(url_for("signup"))

        # Check if user already exists
        snap = db.collection("users").document(email).get()
        if snap.exists:
            flash("Email already registered. Please sign in.", "error")
            return redirect(url_for("signin"))

        # Save user to Firestore (manual signup)
        try:
            db.collection("users").document(email).set({
                "name": name,
                "email": email,
                "password": password,  # ⚠️ use hashing in production
                "role": role,  # <-- save role
                "last_login": datetime.utcnow().isoformat() + "Z"
            })
            session["email"] = email
            session["username"] = name
            session["role"] = role
            flash("Account created successfully ✔️", "success")

            # Redirect by role
            if role == "admin":
                return redirect(url_for("admin_home"))
            else:
                return redirect(url_for("dashboard"))

        except Exception as e:
            flash(f"Error during sign up: {str(e)}", "error")
            return redirect(url_for("signup"))

    # GET request
    return render_template("signup.html")




# ---------------- GOOGLE AUTH ----------------

# ---------------- GOOGLE AUTH ----------------

@app.route("/google_login")
def google_login():
    # Get role from dropdown query param (default user)
    role = request.args.get("role", "user")
    
    # Convert donor to user (as per your new rule)
    if role.lower() == "donor":
        role = "user"

    # Store temporarily for callback
    session["pending_role"] = role

    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(
        redirect_uri,
        prompt="select_account"
    )


@app.route("/google_callback")
def google_callback():
    try:
        token = oauth.google.authorize_access_token()
        if not token:
            flash("Failed to receive OAuth token from Google.", "error")
            return redirect(url_for("signin"))

        resp = oauth.google.get("https://www.googleapis.com/oauth2/v2/userinfo", token=token)
        if not resp.ok:
            raise Exception(f"Failed to fetch user info: {resp.text}")
        userinfo = resp.json()

        email = (userinfo.get("email") or "").lower()
        name = userinfo.get("name") or email.split("@")[0]
        picture = userinfo.get("picture")

        if not email:
            flash("Google account has no email.", "error")
            return redirect(url_for("signin"))

        users_ref = db.collection("users").document(email)
        snap = users_ref.get()

        if snap.exists:
            # Existing user → reuse stored role
            role = snap.to_dict().get("role", "user")
        else:
            # First-time user → assign from dropdown (no restriction)
            role = session.pop("pending_role", "user")

        # 🔥 Convert donor → user for Firestore consistency
        if role.lower() == "donor":
            role = "user"

        # Save or update user in Firestore
        users_ref.set({
            "email": email,
            "name": name,
            "picture": picture,
            "role": role,
            "last_login": datetime.utcnow().isoformat() + "Z"
        }, merge=True)

        # Set session
        session["email"] = email
        session["username"] = name
        session["role"] = role
        session["picture"] = picture

        flash("Signed in successfully ✔️", "success")

        # Redirect by role
        if role == "admin":
            return redirect(url_for("admin_home"))
        else:
            return redirect(url_for("dashboard"))

    except Exception as e:
        print("[Google Callback] Exception:", e)
        flash(f"Google sign-in failed: {str(e)}", "error")
        return redirect(url_for("signin"))



@app.route("/signout")
def signout():
    session.clear()
    return render_template("index.html")

@app.route("/edit-profile", methods=["GET", "POST"])
def edit_profile():
    login_check = require_login()
    if login_check: return login_check

    email = session.get("email")
    if not email:
        return redirect(url_for("signin"))

    snap = db.collection("users").document(email).get()
    user = snap.to_dict() if snap.exists else {
        "name": session.get("username", ""),
        "role": session.get("role", "user")
    }

    if request.method == "POST":
        new_username = request.form["username"]
        new_role = request.form["role"]

        db.collection("users").document(email).set({
            "name": new_username,
            "role": new_role
        }, merge=True)

        session["username"] = new_username
        session["role"] = new_role

        flash("✅ Profile updated successfully!", "success")
        return redirect(url_for("dashboard") if new_role == "user" else url_for("admin"))

    user_template_adapter = {
        "username": user.get("name", ""),
        "password": "",
        "role": user.get("role", "user")
    }

    return render_template("edit_profile.html", user=user_template_adapter)

@app.route("/settings", methods=["GET", "POST"])
def settings():
    login_check = require_login()
    if login_check: return login_check

    default_settings = {
        "notifications": session.get("notifications", "on"),
        "theme": session.get("theme", "dark"),
        "donationType": session.get("donationType", "blood")
    }

    if request.method == "POST":
        session["notifications"] = request.form["notifications"]
        session["theme"] = request.form["theme"]
        session["donationType"] = request.form["donationType"]
        flash("✅ Settings updated successfully!", "success")
        return redirect(url_for("settings"))

    return render_template("settings.html", settings=default_settings)

@app.route("/set-donation-type/<type>")
def set_donation_type(type):
    if type in ["blood", "plasma"]:
        session["donationType"] = type
    return "", 204
@app.route("/donor-requests")
def donor_requests():
    login_check = require_login()
    if login_check: return login_check
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))
    requests_list = []
    for doc in db.collection(get_request_collection()).stream():
        r = doc.to_dict()
        r["id"] = doc.id
        requests_list.append(r)
    return render_template("donor_requests.html", requests=requests_list)

@app.route("/scanner/<request_id>")
def scanner_page(request_id):
    try:
        request_doc = db.collection(get_request_collection()).document(request_id).get()
        if not request_doc.exists:
            return "Request not found", 404

        request_data = request_doc.to_dict()
        accepted_donor = request_data.get("accepted_donor", {})

        return render_template(
            "scanner.html",
            request_id=request_id,
            accepted_donor=accepted_donor,
            request=request_data   # <-- Pass the full request data
        )
    except Exception as e:
        return str(e), 500





@app.route("/accept-request/<request_id>")
def accept_request(request_id):
    login_check = require_login()
    if login_check: return login_check
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))
    req_ref = db.collection(get_request_collection()).document(request_id)
    req_ref.update({"status": "accepted", "accepted_by": session.get("email")})
    flash("Request accepted successfully!", "success")
    return redirect(url_for("donor_requests"))

from flask import request, jsonify, url_for
from geopy.distance import geodesic
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
import uuid
from datetime import datetime
import random
from flask import request, jsonify, url_for
from geopy.distance import geodesic
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
import uuid
from datetime import datetime

@app.route("/submit_request", methods=["POST"])
def submit_request():
    try:
        # Support both JSON and form submissions
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form

        # Extract fields safely
        patient_name = data.get("patient_name", "").strip()
        blood_group = data.get("blood_group", "").strip()
        details = data.get("details", "").strip()
        email = data.get("email", "").strip()
        phone = data.get("phone", "").strip()

        # Convert lat/lng safely
        try:
            lat = float(data.get("lat"))
            lng = float(data.get("lng"))
        except (TypeError, ValueError):
            lat = None
            lng = None

        # Validate required fields
        if not patient_name or not blood_group or lat is None or lng is None:
            return jsonify({
                "status": "error",
                "message": "Patient name, blood/plasma group, and location are required"
            }), 400

        # Generate request ID and timestamp
        request_id = str(uuid.uuid4())
        created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        # Prepare request data
        request_data = {
            "patient_name": patient_name,
            "blood_group": blood_group,
            "details": details,
            "lat": lat,
            "lng": lng,
            "email": email,
            "phone": phone,
            "status": "pending",
            "created_at": created_at
        }

        # Save the request in Firestore
        db.collection(get_request_collection()).document(request_id).set(request_data)

        # Find eligible donors
        users = db.collection("users").where("role", "==", "user").get()
        eligible_users = []
        for user_doc in users:
            user = user_doc.to_dict()
            donor_lat, donor_lng = user.get("lat"), user.get("lng")
            if donor_lat is None or donor_lng is None:
                continue
            distance_km = geodesic((lat, lng), (donor_lat, donor_lng)).km
            if distance_km <= 10:
                user["id"] = user_doc.id
                eligible_users.append(user)

        if eligible_users:
            accepted_donor = random.choice(eligible_users)

            # Update request as accepted
            db.collection(get_request_collection()).document(request_id).update({
                "status": "accepted",
                "accepted_donor": accepted_donor
            })

            # Send donor notification emails in threads
            import threading

            def send_donor_email():
                try:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = "🩸 New Blood/Plasma Request"
                    msg["From"] = EMAIL_ADDRESS
                    msg["To"] = accepted_donor["email"]

                    html = f"""
                    <html>
                    <body>
                        <h2>New Blood/Plasma Request</h2>
                        <p><strong>Patient:</strong> {patient_name}</p>
                        <p><strong>Blood/Plasma Group:</strong> {blood_group}</p>
                        <p>Please respond:</p>
                        <a href="{request.host_url}donor_response/{request_id}/{accepted_donor.get('id')}/accept">✅ Accept</a>
                        <a href="{request.host_url}donor_response/{request_id}/{accepted_donor.get('id')}/reject">❌ Reject</a>
                    </body>
                    </html>
                    """
                    msg.attach(MIMEText(html, "html"))

                    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                        server.sendmail(EMAIL_ADDRESS, accepted_donor["email"], msg.as_string())
                except Exception as e:
                    print("Error sending donor email:", e)

            def send_confirmation_email():
                import time
                time.sleep(5)
                try:
                    FEEDBACK_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfiyiQmI3xUZc1zHrUGHJQsYOVB_JGAox4mDMnDYUHA2xxZYQ/viewform?usp=header"

                    msg2 = MIMEMultipart("alternative")
                    msg2["Subject"] = "✅ Donation Confirmed"
                    msg2["From"] = EMAIL_ADDRESS
                    msg2["To"] = accepted_donor["email"]

                    html2 = f"""
                    <html>
                    <body>
                        <h2>Thank You, {accepted_donor.get('name', 'Donor')} ❤️</h2>
                        <p>You have <strong>accepted</strong> the blood/plasma request.</p>
                        <p><strong>Patient:</strong> {patient_name}</p>
                        <p><strong>Blood/Plasma Group Needed:</strong> {blood_group}</p>
                        <br>
                        <p>We would love your feedback:</p>
                        <table cellspacing="0" cellpadding="0">
                          <tr>
                            <td align="center" bgcolor="#28a745" style="border-radius:5px;">
                              <a href="{FEEDBACK_FORM_URL}" target="_blank" 
                                 style="font-size:16px; font-family:Arial,sans-serif; color:#ffffff; 
                                        text-decoration:none; padding:12px 25px; display:inline-block; font-weight:bold;">
                                 📝 Give Feedback
                              </a>
                            </td>
                          </tr>
                        </table>
                        <p style="font-size:12px; color:#555555; margin-top:10px;">Your feedback helps us improve the donation process.</p>
                    </body>
                    </html>
                    """
                    msg2.attach(MIMEText(html2, "html"))

                    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                        server.sendmail(EMAIL_ADDRESS, accepted_donor["email"], msg2.as_string())
                except Exception as e:
                    print("Error sending confirmation email:", e)

            threading.Thread(target=send_donor_email).start()
            threading.Thread(target=send_confirmation_email).start()

        # Always return JSON, never HTML
        return jsonify({
            "status": "success",
            "message": "Request submitted successfully",
            "request_id": request_id,
            "redirect_url": url_for("scanner_page", request_id=request_id)
        })

    except Exception as e:
        print("Error in /submit_request:", e)
        return jsonify({"status": "error", "message": str(e)}), 500









@app.route("/donor_response/<request_id>/<donor_id>/<action>", methods=["POST"])
def donor_response(request_id, donor_id, action):
    try:
        # Fetch request
        request_ref = db.collection("blood_requests").document(request_id)
        request_doc = request_ref.get()

        if not request_doc.exists:
            return jsonify({"status": "error", "message": "Request not found"}), 404

        request_data = request_doc.to_dict()

        # Fetch donor
        donor_ref = db.collection("users").document(donor_id)
        donor_doc = donor_ref.get()

        if not donor_doc.exists:
            return jsonify({"status": "error", "message": "Donor not found"}), 404

        donor_data = donor_doc.to_dict()

        # Common patient/admin email
        patient_email = request_data.get("patient_email", None)
        admin_email = EMAIL_ADDRESS

        FEEDBACK_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfiyiQmI3xUZc1zHrUGHJQsYOVB_JGAox4mDMnDYUHA2xxZYQ/viewform?usp=header"

        if action == "accept":
            # Update request as accepted
            request_ref.update({
                "status": "accepted",
                "accepted_by": donor_id,
                "accepted_at": datetime.utcnow()
            })

            # Donor email with feedback button
            donor_msg = MIMEMultipart("alternative")
            donor_msg["Subject"] = "✅ Donation Confirmed"
            donor_msg["From"] = EMAIL_ADDRESS
            donor_msg["To"] = donor_data["email"]

            html_content = f"""
            <html>
            <body>
                <h2>Thank You, {donor_data.get("name", "Donor")} ❤️</h2>
                <p>You have <strong>accepted</strong> the blood donation request.</p>
                <p><strong>Patient:</strong> {request_data.get("patient_name")}</p>
                <p><strong>Blood Group Needed:</strong> {request_data.get("blood_group")}</p>
                <br>
                <p>We would love your feedback:</p>
                <table cellspacing="0" cellpadding="0">
                  <tr>
                    <td align="center" bgcolor="#28a745" style="border-radius:5px;">
                      <a href="{FEEDBACK_FORM_URL}" target="_blank" 
                         style="font-size:16px; font-family:Arial,sans-serif; color:#ffffff; 
                                text-decoration:none; padding:12px 25px; display:inline-block; font-weight:bold;">
                         📝 Give Feedback
                      </a>
                    </td>
                  </tr>
                </table>
                <p style="font-size:12px; color:#555555; margin-top:10px;">Your feedback helps us improve the donation process.</p>
            </body>
            </html>
            """

            donor_msg.attach(MIMEText(html_content, "html"))

            # Email to patient/admin notifying acceptance
            notify_msg = MIMEMultipart("alternative")
            notify_msg["Subject"] = f"🩸 Request Accepted by Donor: {donor_data.get('name', 'Donor')}"
            notify_msg["From"] = EMAIL_ADDRESS
            notify_msg["To"] = patient_email if patient_email else admin_email

            notify_html = f"""
            <html>
            <body>
                <h2>Your blood/plasma request has been accepted!</h2>
                <p><strong>Patient:</strong> {request_data.get("patient_name")}</p>
                <p><strong>Blood/Plasma Group:</strong> {request_data.get("blood_group")}</p>
                <p><strong>Donor:</strong> {donor_data.get('name', 'Donor')}</p>
                <p>Contact Donor: {donor_data.get('phone', 'N/A')} | Email: {donor_data.get('email', 'N/A')}</p>
            </body>
            </html>
            """
            notify_msg.attach(MIMEText(notify_html, "html"))

            # Send both emails
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                server.sendmail(EMAIL_ADDRESS, donor_data["email"], donor_msg.as_string())
                server.sendmail(EMAIL_ADDRESS, notify_msg["To"], notify_msg.as_string())

            return jsonify({"status": "success", "message": "Donor accepted, emails sent with feedback button"})

        elif action == "reject":
            # Keep your existing rejection logic (no feedback button)
            request_ref.update({
                "status": "rejected",
                "rejected_by": donor_id,
                "rejected_at": datetime.utcnow()
            })
            # send rejection emails as you already have
            # ...
            return jsonify({"status": "success", "message": "Donor rejected and emails sent"})

        else:
            return jsonify({"status": "error", "message": "Invalid action"}), 400

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500





@app.route("/update_location", methods=["POST"])
def update_location():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 403

    data = request.get_json()
    lat = data.get("lat")
    lng = data.get("lng")

    if not lat or not lng:
        return jsonify({"status": "error", "message": "Invalid location"}), 400

    user_id = session["user"]["uid"]  # or however you track users in session

    db.collection("users").document(user_id).set({
        "lat": lat,
        "lng": lng
    }, merge=True)

    return jsonify({"status": "success", "message": "Location updated"})

# Add this route to app.py (dev / one-time use)
@app.route("/admin_hardcode_users_to_admin/<admin_email>", methods=["POST"])
def hardcode_users_to_admin(admin_email):
    try:
        # lookup admin user doc to get their stored location
        admin_q = db.collection("users").where("email", "==", admin_email).limit(1).get()
        if not admin_q:
            return jsonify({"status":"error","message":"Admin not found"}), 404
        admin_doc = admin_q[0]
        admin_data = admin_doc.to_dict()
        admin_lat = admin_data.get("lat")
        admin_lng = admin_data.get("lng")

        if admin_lat is None or admin_lng is None:
            return jsonify({"status":"error","message":"Admin has no lat/lng stored"}), 400

        users = db.collection("users").where("role", "==", "user").get()
        count = 0
        for u in users:
            db.collection("users").document(u.id).set({
                "lat": admin_lat,
                "lng": admin_lng
            }, merge=True)
            count += 1

        return jsonify({"status":"success", "updated_users": count, "lat": admin_lat, "lng": admin_lng})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

@app.route("/get_request/<request_id>")
def get_request(request_id):
    req_doc = db.collection("blood_requests").document(request_id).get()
    if not req_doc.exists:
        return jsonify({"status": "error", "message": "Request not found"})
    return jsonify({"status": "success", "request": req_doc.to_dict()})

@app.route("/send_confirmation_email/<request_id>", methods=["POST"])
def send_confirmation_email(request_id):
    try:
        request_ref = db.collection(get_request_collection()).document(request_id)
        request_doc = request_ref.get()
        if not request_doc.exists:
            return jsonify({"status": "error", "message": "Request not found"}), 404

        request_data = request_doc.to_dict()
        donor = request_data.get("accepted_donor")
        if not donor:
            return jsonify({"status": "error", "message": "No donor assigned"}), 400

        # Send email
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "✅ Donation Confirmed"
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = donor["email"]

        html = f"""
        <html>
        <body>
            <h2>Thank You, {donor.get('name', 'Donor')} ❤️</h2>
            <p>You have <strong>accepted</strong> the blood/plasma request.</p>
            <p><strong>Patient:</strong> {request_data.get('patient_name')}</p>
            <p><strong>Blood/Plasma Group Needed:</strong> {request_data.get('blood_group')}</p>
        </body>
        </html>
        """
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, donor["email"], msg.as_string())

        return jsonify({"status": "success", "message": "Confirmation email sent"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
