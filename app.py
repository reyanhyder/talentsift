from flask import Flask, request, jsonify, render_template
from groq import Groq
import cloudinary
import cloudinary.uploader
import PyPDF2
import os
from dotenv import load_dotenv
import io
import json

load_dotenv()

app = Flask(__name__)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

def extract_text_from_pdf(file_bytes):
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        return ""

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        job_description = request.form.get("job_description")
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
                results.append({
                    "filename": resume.filename,
                    "analysis": {
                        "ats_score": 0,
                        "matching_skills": [],
                        "missing_skills": [],
                        "summary": "This file could not be read. It may be a scanned or image-based PDF. Please upload a text-based resume.",
                        "recommendation": "Unable to Process"
                    }
                })
                continue

            resume_keywords = ["experience", "education", "skills", "work", "university", "college", "degree", "project", "internship", "certification"]
            if not any(word in text.lower() for word in resume_keywords):
                results.append({
                    "filename": resume.filename,
                    "analysis": {
                        "ats_score": 0,
                        "matching_skills": [],
                        "missing_skills": [],
                        "summary": "This does not appear to be a resume. Please upload a valid resume in PDF format.",
                        "recommendation": "Not a Resume"
                    }
                })
                continue

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
            except Exception:
                parsed = {
                    "ats_score": 0,
                    "matching_skills": [],
                    "missing_skills": [],
                    "summary": "Could not parse response.",
                    "recommendation": "Maybe"
                }

            results.append({
                "filename": resume.filename,
                "analysis": parsed
            })

        return jsonify(results)

    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
