python3 << 'EOF'
content = '''from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from groq import Groq
import cloudinary
import cloudinary.uploader
import PyPDF2
import os
from dotenv import load_dotenv
import io
import json
from supabase import create_client, Client
from functools import wraps
from datetime import datetime

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "talentsift-secret-key-change-in-prod")

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

rate_limit_store = {}
PLAN_LIMITS = {"free": 3, "starter": 50, "pro": 200, "team": 1000}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def get_user_plan(user_id):
    try:
        result = supabase.table("users").select("plan").eq("id", user_id).single().execute()
        return result.data.get("plan", "free") if result.data else "free"
    except:
        return "free"

def check_rate_limit(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    plan = get_user_plan(user_id)
    limit = PLAN_LIMITS.get(plan, 3)
    if user_id not in rate_limit_store:
        rate_limit_store[user_id] = {"count": 0, "date": today}
    user_data = rate_limit_store[user_id]
    if user_data["date"] != today:
        rate_limit_store[user_id] = {"count": 0, "date": today}
        user_data = rate_limit_store[user_id]
    if user_data["count"] >= limit:
        return False, limit, plan
    rate_limit_store[user_id]["count"] += 1
    return True, limit, plan

def save_analysis_history(user_id, job_description, results):
    try:
        supabase.table("analysis_history").insert({
            "user_id": user_id,
            "job_description": job_description[:500],
            "results": json.dumps(results),
            "resume_count": len(results),
            "created_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print("History save error:", e)

def extract_text_from_pdf(file_bytes):
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except:
        return ""

@app.route("/")
def index():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("index.html", user=session["user"])

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        data = request.get_json()
        email = data.get("email", "").strip()
        password = data.get("password", "")
        if not email or not password:
            return jsonify({"success": False, "error": "Please enter your email and password."}), 400
        try:
            res = supabase.auth.sign_in_with_password({"email": email, "password": password})
            user = res.user
            if not user:
                return jsonify({"success": False, "error": "Incorrect email or password. Please try again."}), 401
            if not user.email_confirmed_at:
                return jsonify({"success": False, "error": "Please verify your email before signing in. Check your inbox."}), 401
            session["user"] = {
                "id": user.id,
                "email": user.email,
                "name": user.user_metadata.get("full_name", email.split("@")[0])
            }
            try:
                supabase.table("users").upsert({"id": user.id, "email": user.email, "plan": "free"}, on_conflict="id").execute()
            except:
                pass
            return jsonify({"success": True})
        except Exception as e:
            err = str(e).lower()
            if "email not confirmed" in err:
                return jsonify({"success": False, "error": "Please verify your email before signing in. Check your inbox."}), 401
            return jsonify({"success": False, "error": "Incorrect email or password. Please try again."}), 401
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "user" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        data = request.get_json()
        email = data.get("email", "").strip()
        password = data.get("password", "")
        name = data.get("name", "").strip()
        if not email or not password or not name:
            return jsonify({"success": False, "error": "Please fill in all fields."}), 400
        if len(password) < 8:
            return jsonify({"success": False, "error": "Password must be at least 8 characters."}), 400
        try:
            res = supabase.auth.sign_up({
                "email": email,
                "password": password,
                "options": {"data": {"full_name": name}}
            })
            user = res.user
            if not user:
                return jsonify({"success": False, "error": "Signup failed. Please try again."}), 400
            if hasattr(user, "identities") and user.identities is not None and len(user.identities) == 0:
                return jsonify({"success": False, "error": "This email is already registered. Please sign in instead."}), 400
            try:
                supabase.table("users").insert({"id": user.id, "email": user.email, "plan": "free"}).execute()
            except:
                pass
            return jsonify({"success": True, "message": "Account created! Please check your email to verify your account."})
        except Exception as e:
            err = str(e).lower()
            if "already registered" in err or "already exists" in err:
                return jsonify({"success": False, "error": "This email is already registered. Please sign in instead."}), 400
            return jsonify({"success": False, "error": "Signup failed. Please try again."}), 400
    return render_template("signup.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/history")
@login_required
def history():
    user_id = session["user"]["id"]
    try:
        result = supabase.table("analysis_history").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(20).execute()
        return jsonify(result.data)
    except:
        return jsonify([])

@app.route("/analyze", methods=["POST"])
@login_required
def analyze():
    try:
        user_id = session["user"]["id"]
        allowed, limit, plan = check_rate_limit(user_id)
        if not allowed:
            return jsonify({"error": f"Daily limit reached. Your {plan} plan allows {limit} analyses per day.", "limit_reached": True, "plan": plan}), 429
        job_description = request.form.get("job_description")
        resumes = request.files.getlist("resumes")
        results = []
        for resume in resumes:
            resume_bytes = resume.read()
            try:
                cloudinary.uploader.upload(io.BytesIO(resume_bytes), resource_type="raw", folder="talentsift", public_id=resume.filename)
            except Exception as e:
                print("Cloudinary error:", e)
            text = extract_text_from_pdf(resume_bytes)
            if not text.strip():
                text = "Could not extract text from this PDF."
            prompt = (
                "You are an expert HR recruiter and ATS system.\\n\\n"
                "Job Description:\\n" + job_description + "\\n\\n"
                "Resume Content:\\n" + text[:3000] + "\\n\\n"
                "Analyze this resume against the job description.\\n"
                "Respond ONLY with a valid raw JSON object. No markdown. No explanation. Just JSON.\\n"
                "Example format:\\n"
                \'{"ats_score": 75, "matching_skills": ["Python", "Cloud", "APIs"], "missing_skills": ["Kubernetes"], "summary": "Strong backend developer.", "recommendation": "Strong Yes"}\'
            )
            chat_completion = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
            )
            response_text = chat_completion.choices[0].message.content.strip()
            if response_text.startswith("```"):
                lines = [l for l in response_text.split("\\n") if not l.startswith("```")]
                response_text = "\\n".join(lines)
            try:
                parsed = json.loads(response_text)
            except:
                parsed = {"ats_score": 0, "matching_skills": [], "missing_skills": [], "summary": "Could not parse response.", "recommendation": "Maybe"}
            results.append({"filename": resume.filename, "analysis": parsed})
        save_analysis_history(user_id, job_description, results)
        return jsonify(results)
    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
'''
with open("app.py", "w") as f:
    f.write(content)
print("Done!")
EOF