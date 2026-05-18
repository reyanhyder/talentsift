from flask import Flask, request, jsonify, render_template, redirect, url_for, session, Response
from groq import Groq
import cloudinary
import cloudinary.uploader
import PyPDF2
import os
from html import escape
from dotenv import load_dotenv
import io
import json
import hmac
import hashlib
import requests
from supabase import create_client, Client
from functools import wraps
from datetime import datetime, timedelta
import time
from jinja2 import Undefined
from flask.json.provider import DefaultJSONProvider

load_dotenv()

DEPLOY_VERSION = "launch-checklist-2026-05-18-1825"
BASE_URL = os.getenv("BASE_URL", "https://talentsift-production.up.railway.app").rstrip("/")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@talentsift.com")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

class SafeJSONProvider(DefaultJSONProvider):
    def default(self, value):
        if isinstance(value, Undefined):
            return None
        return super().default(value)

def sanitize_for_json(value):
    if isinstance(value, Undefined):
        return None
    if isinstance(value, dict):
        return {str(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_for_json(item) for item in value]
    return value

def safe_json_dumps(value, **kwargs):
    return json.dumps(sanitize_for_json(value), **kwargs)

def render_index_html(user):
    template_path = os.path.join(app.root_path, "templates", "index.html")
    with open(template_path, "r", encoding="utf-8") as template_file:
        html = template_file.read()

    display_name = user.get("name") or user.get("email") or "User"
    avatar = (display_name[:1] or "?").upper()
    public_config = json.dumps(get_public_config(user.get("id")), separators=(",", ":"))

    return (
        html
        .replace("{{ (user.name or user.email or '?')[:1] | upper | e }}", escape(avatar))
        .replace("{{ user.name or user.email or 'User' }}", escape(display_name))
        .replace("__SUPPORT_EMAIL__", escape(SUPPORT_EMAIL))
        .replace("__PUBLIC_CONFIG__", public_config.replace("</", "<\\/"))
    )

app = Flask(__name__)
app.json = SafeJSONProvider(app)
app.jinja_env.policies["json.dumps_function"] = safe_json_dumps
app.secret_key = os.getenv("FLASK_SECRET_KEY", "talentsift-secret-key-change-in-prod")

# Groq
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Rate limiting (in-memory, per user per day)
# Format: { user_id: { 'count': N, 'date': 'YYYY-MM-DD' } }
rate_limit_store = {}
auth_attempt_store = {}

# Subscription limits
PLAN_LIMITS = {
    'free': 3,
    'starter': 50,
    'pro': 200,
    'team': 1000
}

PLAN_PRICING = {
    'starter': {'name': 'Starter', 'amount': 49900, 'display': 'Rs. 499', 'limit': 50},
    'pro': {'name': 'Pro', 'amount': 199900, 'display': 'Rs. 1999', 'limit': 200},
    'team': {'name': 'Team', 'amount': 799900, 'display': 'Rs. 7999', 'limit': 1000}
}

# Simple in-memory cache for repeated analysis requests. Railway instances are
# ephemeral, so this is a cost/speed guard rather than durable storage.
analysis_cache = {}

# ─── AUTH HELPERS ────────────────────────────────────────────────────────────

def json_safe(value, fallback=""):
    if isinstance(value, Undefined) or value is None:
        return fallback
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

def build_session_user(user, fallback_name=""):
    raw_metadata = getattr(user, "user_metadata", None)
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    email = json_safe(getattr(user, "email", ""))
    name = json_safe(metadata.get("full_name"), fallback_name or email.split("@")[0])

    return {
        'id': json_safe(getattr(user, "id", "")),
        'email': email,
        'name': name
    }

@app.before_request
def clean_session_user():
    user = session.get('user')
    if not isinstance(user, dict):
        return

    cleaned = {
        'id': json_safe(user.get('id')),
        'email': json_safe(user.get('email')),
        'name': json_safe(user.get('name'), json_safe(user.get('email')).split('@')[0])
    }

    if cleaned != user:
        session['user'] = cleaned

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def check_auth_attempts(email):
    key = f"{request.remote_addr}:{email.lower()}"
    now = time.time()
    window = 15 * 60
    attempts = [ts for ts in auth_attempt_store.get(key, []) if now - ts < window]
    if len(attempts) >= 8:
        auth_attempt_store[key] = attempts
        return False
    attempts.append(now)
    auth_attempt_store[key] = attempts
    return True

def clear_auth_attempts(email):
    auth_attempt_store.pop(f"{request.remote_addr}:{email.lower()}", None)

def get_user_plan(user_id):
    try:
        result = supabase.table('users').select('plan').eq('id', user_id).single().execute()
        return result.data.get('plan', 'free') if result.data else 'free'
    except:
        return 'free'

def check_rate_limit(user_id):
    today = datetime.now().strftime('%Y-%m-%d')
    plan = get_user_plan(user_id)
    limit = PLAN_LIMITS.get(plan, 3)

    if user_id not in rate_limit_store:
        rate_limit_store[user_id] = {'count': 0, 'date': today}

    user_data = rate_limit_store[user_id]

    # Reset if new day
    if user_data['date'] != today:
        rate_limit_store[user_id] = {'count': 0, 'date': today}
        user_data = rate_limit_store[user_id]

    if user_data['count'] >= limit:
        return False, limit, plan

    rate_limit_store[user_id]['count'] += 1
    return True, limit, plan

def save_analysis_history(user_id, job_description, results):
    try:
        supabase.table('analysis_history').insert({
            'user_id': user_id,
            'job_description': (job_description or '')[:500],
            'results': json.dumps(results),
            'resume_count': len(results),
            'created_at': datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print("History save error:", e)

def update_user_plan(user_id, plan):
    if plan not in PLAN_LIMITS:
        return False
    try:
        supabase.table('users').update({
            'plan': plan,
            'updated_at': datetime.utcnow().isoformat()
        }).eq('id', user_id).execute()
        return True
    except Exception as e:
        print("Plan update error:", e)
        return False

def track_event(user_id, event_name, metadata=None):
    try:
        supabase.table('user_events').insert({
            'user_id': user_id,
            'event_name': event_name,
            'metadata': metadata or {},
            'created_at': datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print("Event tracking error:", e)

def cache_key_for(job_description, resume_text):
    raw = (job_description or "") + "\n---resume---\n" + (resume_text or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def get_public_config(user_id=None):
    plan = get_user_plan(user_id) if user_id else "free"
    return {
        "razorpay_key_id": RAZORPAY_KEY_ID,
        "plans": PLAN_PRICING,
        "current_plan": plan,
        "limits": PLAN_LIMITS,
        "support_email": SUPPORT_EMAIL,
        "base_url": BASE_URL
    }

# ─── PDF EXTRACTION ──────────────────────────────────────────────────────────

def extract_text_from_pdf(file_bytes):
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except:
        return ""

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if 'user' not in session:
        return redirect(url_for('login'))
    user = {
        'id': str(session['user'].get('id', '')),
        'email': str(session['user'].get('email', '')),
        'name': str(session['user'].get('name', ''))
    }
    return Response(render_index_html(user), mimetype="text/html")

@app.route("/_version")
def version():
    return jsonify({
        "version": DEPLOY_VERSION,
        "index_uses_jinja": False,
        "payments_ready": bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)
    })

@app.route("/privacy")
def privacy():
    return render_template("legal.html", page="privacy", support_email=SUPPORT_EMAIL, base_url=BASE_URL)

@app.route("/terms")
def terms():
    return render_template("legal.html", page="terms", support_email=SUPPORT_EMAIL, base_url=BASE_URL)

@app.route("/cookies")
def cookies():
    return render_template("legal.html", page="cookies", support_email=SUPPORT_EMAIL, base_url=BASE_URL)

@app.route("/robots.txt")
def robots():
    body = f"User-agent: *\nAllow: /\nSitemap: {BASE_URL}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap():
    pages = ["", "/login", "/signup", "/privacy", "/terms", "/cookies"]
    urls = "\n".join(
        f"<url><loc>{BASE_URL}{path}</loc><changefreq>weekly</changefreq><priority>{'1.0' if path == '' else '0.7'}</priority></url>"
        for path in pages
    )
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        mimetype="application/xml"
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if 'user' in session:
        return redirect(url_for('index'))

    if request.method == "POST":
        data = request.get_json() or {}
        email = data.get("email", "")
        password = data.get("password", "")
        if not check_auth_attempts(email):
            return jsonify({"success": False, "error": "Too many login attempts. Please try again later."}), 429
        try:
            res = supabase.auth.sign_in_with_password({"email": email, "password": password})
            user = res.user
            session['user'] = build_session_user(user, email.split('@')[0])
            clear_auth_attempts(email)
            # Ensure user record exists in users table
            try:
                supabase.table('users').upsert({
                    'id': user.id,
                    'email': user.email,
                    'plan': 'free'
                }, on_conflict='id').execute()
            except:
                pass
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": "Invalid email or password"}), 401

    return render_template("login.html")

@app.route("/auth/google")
def google_login():
    try:
        redirect_to = f"{BASE_URL}/"
        res = supabase.auth.sign_in_with_oauth({
            "provider": "google",
            "options": {"redirect_to": redirect_to}
        })
        oauth_url = getattr(res, "url", None) or (res.get("url") if isinstance(res, dict) else None)
        if oauth_url:
            return redirect(oauth_url)
    except Exception as e:
        print("Google OAuth error:", e)
    return redirect(url_for("login"))

@app.route("/password-reset", methods=["POST"])
def password_reset():
    data = request.get_json() or {}
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"success": False, "error": "Email is required."}), 400
    try:
        supabase.auth.reset_password_email(email, {"redirect_to": f"{BASE_URL}/login"})
    except Exception as e:
        print("Password reset error:", e)
    return jsonify({"success": True, "message": "If the email exists, a reset link has been sent."})

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if 'user' in session:
        return redirect(url_for('index'))

    if request.method == "POST":
        data = request.get_json() or {}
        email = data.get("email", "")
        password = data.get("password", "")
        name = data.get("name", "")
        try:
            res = supabase.auth.sign_up({
                "email": email,
                "password": password,
                "options": {"data": {"full_name": name}}
            })
            user = res.user
            if user:
                # Insert into users table
                try:
                    supabase.table('users').insert({
                        'id': user.id,
                        'email': user.email,
                        'plan': 'free'
                    }).execute()
                except:
                    pass
                return jsonify({"success": True})
            else:
                return jsonify({"success": False, "error": "Signup failed. Try again."}), 400
        except Exception as e:
            err = str(e)
            if "already registered" in err.lower():
                return jsonify({"success": False, "error": "Email already registered. Please log in."}), 400
            return jsonify({"success": False, "error": "Signup failed. Please try again."}), 400

    return render_template("signup.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route("/history")
@login_required
def history():
    user_id = session['user']['id']
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(max(int(request.args.get("per_page", 10)), 1), 50)
    start = (page - 1) * per_page
    end = start + per_page - 1
    try:
        result = supabase.table('analysis_history') \
            .select('*') \
            .eq('user_id', user_id) \
            .order('created_at', desc=True) \
            .range(start, end) \
            .execute()
        return jsonify({
            "items": result.data or [],
            "page": page,
            "per_page": per_page,
            "has_more": len(result.data or []) == per_page
        })
    except Exception as e:
        return jsonify({"items": [], "page": page, "per_page": per_page, "has_more": False})

@app.route("/billing/config")
@login_required
def billing_config():
    return jsonify(get_public_config(session['user']['id']))

@app.route("/billing/create-order", methods=["POST"])
@login_required
def create_order():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return jsonify({"error": "Payments are not configured yet."}), 503

    data = request.get_json() or {}
    plan = data.get("plan")
    if plan not in PLAN_PRICING:
        return jsonify({"error": "Invalid plan."}), 400

    order_payload = {
        "amount": PLAN_PRICING[plan]["amount"],
        "currency": "INR",
        "receipt": f"ts_{session['user']['id'][:8]}_{int(time.time())}",
        "notes": {
            "user_id": session['user']['id'],
            "plan": plan,
            "product": "talentsift"
        }
    }

    try:
        res = requests.post(
            "https://api.razorpay.com/v1/orders",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            json=order_payload,
            timeout=15
        )
        if res.status_code >= 400:
            return jsonify({"error": "Could not create payment order."}), 502
        order = res.json()
        try:
            supabase.table('payments').insert({
                'user_id': session['user']['id'],
                'plan': plan,
                'razorpay_order_id': order.get('id'),
                'amount': PLAN_PRICING[plan]["amount"],
                'status': 'created',
                'created_at': datetime.utcnow().isoformat()
            }).execute()
        except Exception as e:
            print("Payment insert error:", e)
        return jsonify({
            "order": order,
            "key_id": RAZORPAY_KEY_ID,
            "plan": plan,
            "display": PLAN_PRICING[plan]["display"]
        })
    except Exception as e:
        print("Razorpay order error:", e)
        return jsonify({"error": "Payment service unavailable."}), 502

@app.route("/billing/verify-payment", methods=["POST"])
@login_required
def verify_payment():
    data = request.get_json() or {}
    required = ["razorpay_order_id", "razorpay_payment_id", "razorpay_signature", "plan"]
    if any(not data.get(key) for key in required):
        return jsonify({"success": False, "error": "Missing payment verification data."}), 400

    payload = f"{data['razorpay_order_id']}|{data['razorpay_payment_id']}"
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, data["razorpay_signature"]):
        return jsonify({"success": False, "error": "Payment signature verification failed."}), 400

    plan = data["plan"]
    updated = update_user_plan(session['user']['id'], plan)
    try:
        supabase.table('payments').update({
            'razorpay_payment_id': data['razorpay_payment_id'],
            'status': 'paid',
            'verified_at': datetime.utcnow().isoformat()
        }).eq('razorpay_order_id', data['razorpay_order_id']).execute()
    except Exception as e:
        print("Payment verify update error:", e)

    track_event(session['user']['id'], "payment_verified", {"plan": plan})
    return jsonify({"success": updated, "plan": plan, "limit": PLAN_LIMITS.get(plan, 3)})

@app.route("/billing/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    if not RAZORPAY_WEBHOOK_SECRET:
        return jsonify({"ok": True, "ignored": "webhook secret not configured"})

    signature = request.headers.get("X-Razorpay-Signature", "")
    body = request.get_data()
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "Invalid signature"}), 400

    event = request.get_json() or {}
    payload = event.get("payload", {})
    payment = payload.get("payment", {}).get("entity", {})
    order_id = payment.get("order_id")
    status = payment.get("status", event.get("event", "received"))
    if order_id:
        try:
            supabase.table('payments').update({
                'status': status,
                'webhook_event': event.get("event"),
                'updated_at': datetime.utcnow().isoformat()
            }).eq('razorpay_order_id', order_id).execute()
        except Exception as e:
            print("Webhook update error:", e)
    return jsonify({"ok": True})

@app.route("/billing/change-plan", methods=["POST"])
@login_required
def change_plan():
    data = request.get_json() or {}
    plan = data.get("plan", "free")
    if plan != "free":
        return jsonify({"success": False, "error": "Paid plan changes must go through checkout."}), 400
    updated = update_user_plan(session['user']['id'], "free")
    track_event(session['user']['id'], "plan_changed", {"plan": "free"})
    return jsonify({"success": updated, "plan": "free", "limit": PLAN_LIMITS["free"]})

@app.route("/feedback", methods=["POST"])
@login_required
def feedback():
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    kind = data.get("kind", "feedback")
    if not message:
        return jsonify({"success": False, "error": "Message is required."}), 400
    try:
        supabase.table('feedback').insert({
            'user_id': session['user']['id'],
            'kind': kind,
            'message': message[:2000],
            'page': data.get("page", "/"),
            'created_at': datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print("Feedback save error:", e)
    return jsonify({"success": True, "message": "Thanks. Your message was received."})

@app.route("/events", methods=["POST"])
def events():
    data = request.get_json() or {}
    user_id = session.get('user', {}).get('id')
    track_event(user_id, data.get("event_name", "page_view"), data.get("metadata", {}))
    return jsonify({"success": True})

@app.route("/analyze", methods=["POST"])
@login_required
def analyze():
    try:
        user_id = session['user']['id']

        # Rate limit check
        allowed, limit, plan = check_rate_limit(user_id)
        if not allowed:
            return jsonify({
                "error": f"Daily limit reached. Your {plan} plan allows {limit} analyses per day. Upgrade to continue.",
                "limit_reached": True,
                "plan": plan
            }), 429

        job_description = request.form.get("job_description", "")
        resumes = request.files.getlist("resumes")
        results = []

        for resume in resumes:
            resume_bytes = resume.read()

            try:
                cloudinary.uploader.upload(
                    io.BytesIO(resume_bytes),
                    resource_type="raw",
                    folder="talentsift",
                    public_id=resume.filename
                )
            except Exception as e:
                print("Cloudinary error:", e)

            text = extract_text_from_pdf(resume_bytes)
            if not text.strip():
                text = "Could not extract text from this PDF."

            key = cache_key_for(job_description, text[:3000])
            if key in analysis_cache:
                parsed = analysis_cache[key]
            else:
                prompt = (
                    "You are an expert HR recruiter and ATS system.\n\n"
                    "Job Description:\n" + job_description + "\n\n"
                    "Resume Content:\n" + text[:3000] + "\n\n"
                    "Analyze this resume against the job description.\n"
                    "Respond ONLY with a valid raw JSON object. No markdown. No explanation. Just JSON.\n"
                    "Example format:\n"
                    '{"ats_score": 75, "matching_skills": ["Python", "Cloud", "APIs"], '
                    '"missing_skills": ["Kubernetes", "Terraform", "CI/CD"], '
                    '"summary": "Strong backend developer with cloud experience.", '
                    '"recommendation": "Strong Yes"}'
                )

                chat_completion = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                )

                response_text = chat_completion.choices[0].message.content.strip()

                if response_text.startswith("```"):
                    lines = response_text.split("\n")
                    lines = [l for l in lines if not l.startswith("```")]
                    response_text = "\n".join(lines)

                try:
                    parsed = json.loads(response_text)
                    analysis_cache[key] = parsed
                except:
                    parsed = {
                        "ats_score": 0,
                        "matching_skills": [],
                        "missing_skills": [],
                        "summary": "Could not parse response.",
                        "recommendation": "Maybe"
                    }

            results.append({"filename": resume.filename, "analysis": parsed})

        # Save to history
        save_analysis_history(user_id, job_description, results)
        track_event(user_id, "analysis_completed", {"resume_count": len(results), "plan": plan})

        return jsonify(results)

    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
