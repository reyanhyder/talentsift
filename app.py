@app.route("/login", methods=["GET", "POST"])
def login():
    if 'user' in session:
        return redirect(url_for('index'))

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

            # Block unverified users
            if not user.email_confirmed_at:
                return jsonify({"success": False, "error": "Please verify your email before signing in. Check your inbox."}), 401

            session['user'] = {
                'id': user.id,
                'email': user.email,
                'name': user.user_metadata.get('full_name', email.split('@')[0])
            }
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
            err = str(e).lower()
            if "invalid" in err or "credentials" in err or "not found" in err or "wrong" in err:
                return jsonify({"success": False, "error": "Incorrect email or password. Please try again."}), 401
            if "email not confirmed" in err:
                return jsonify({"success": False, "error": "Please verify your email before signing in. Check your inbox."}), 401
            return jsonify({"success": False, "error": "Incorrect email or password. Please try again."}), 401

    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if 'user' in session:
        return redirect(url_for('index'))

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

            # Supabase returns a user with identities=[] if email already exists
            if hasattr(user, 'identities') and user.identities is not None and len(user.identities) == 0:
                return jsonify({"success": False, "error": "This email is already registered. Please sign in instead."}), 400

            # Insert into users table
            try:
                supabase.table('users').insert({
                    'id': user.id,
                    'email': user.email,
                    'plan': 'free'
                }).execute()
            except:
                pass

            # Don't log them in yet — make them verify email first
            return jsonify({
                "success": True,
                "message": "Account created! Please check your email to verify your account before signing in."
            })

        except Exception as e:
            err = str(e).lower()
            if "already registered" in err or "already exists" in err:
                return jsonify({"success": False, "error": "This email is already registered. Please sign in instead."}), 400
            return jsonify({"success": False, "error": "Signup failed. Please try again."}), 400

    return render_template("signup.html")