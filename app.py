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
from supabase import create_client, Client
from functools import wraps
from datetime import datetime, timedelta
import time
from jinja2 import Undefined
from flask.json.provider import DefaultJSONProvider

load_dotenv()

DEPLOY_VERSION = "homepage-no-jinja-a8076b1"

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

    return (
        html
        .replace("{{ (user.name or user.email or '?')[:1] | upper | e }}", escape(avatar))
        .replace("{{ user.name or user.email or 'User' }}", escape(display_name))
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

# Subscription limits
PLAN_LIMITS = {
    'free': 3,
    'starter': 50,
    'pro': 200,
    'team': 1000
}

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
        "index_uses_jinja": False
    })

@app.route("/login", methods=["GET", "POST"])
def login():
    if 'user' in session:
        return redirect(url_for('index'))

    if request.method == "POST":
        data = request.get_json() or {}
        email = data.get("email", "")
        password = data.get("password", "")
        try:
            res = supabase.auth.sign_in_with_password({"email": email, "password": password})
            user = res.user
            session['user'] = build_session_user(user, email.split('@')[0])
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
                session['user'] = build_session_user(user, name or email.split('@')[0])
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
    try:
        result = supabase.table('analysis_history') \
            .select('*') \
            .eq('user_id', user_id) \
            .order('created_at', desc=True) \
            .limit(20) \
            .execute()
        return jsonify(result.data or [])
    except Exception as e:
        return jsonify([])

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

        return jsonify(results)

    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
