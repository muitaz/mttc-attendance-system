# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response, current_app
from flask_socketio import SocketIO
from datetime import datetime, timedelta
from xhtml2pdf import pisa
from flask import make_response
from flask_socketio import emit, join_room, leave_room
from flask import jsonify
import csv
import io
import sqlite3
import json
import os
import random
import hashlib


# ------------------ Config ------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "mttc.db")
TOKEN_VALIDITY_MINUTES = 30  # token expires after 30 minutes

app = Flask(__name__, template_folder="templates")
app.secret_key = 'your_secret_key'
socketio = SocketIO(app)

@socketio.on('join_room')
def handle_join_room(data):
    room = data.get('room')
    join_room(room)
    print(f"✅ Trainee joined room: {room}")
# ------------------ DB helpers ------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (username TEXT PRIMARY KEY, full_name TEXT, password TEXT, role TEXT,
                  class TEXT, assessment_number TEXT, subjects TEXT)''')
    conn.commit()
    conn.close()

def save_user_to_db(username, userobj):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    subjects_json = json.dumps(userobj.get('subjects', {}))
    c.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?)",
              (username, userobj.get('full_name'), userobj.get('password'),
               userobj.get('role'), userobj.get('class', ''), userobj.get('assessment_number', ''), subjects_json))
    conn.commit()
    conn.close()

def load_users_from_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users")
    users_list = c.fetchall()
    users_dict = {}
    for u in users_list:
        users_dict[u[0]] = {
            'full_name': u[1],
            'password': u[2],
            'role': u[3],
            'class': u[4],
            'assessment_number': u[5],
            'subjects': json.loads(u[6]) if u[6] else {}
        }
    conn.close()
    return users_dict

# ------------------ In-memory structures ------------------
users = {}
marked_students = {}  # key = lesson_id, value = set of trainee names
user_tokens = {}      # username -> {"token": "1234", "expires": datetime_object, "used": False}

students = []
attendance_status = {}
all_subjects = set()
subject_percentages = {}
classes = []
active_lessons = {}
lesson_devices = {}  # lesson_id -> set of device hashes
attendance_history = []

# ----------------- SOCKET.IO HANDLERS -----------------

# Trainee joins their private room for live token updates
@socketio.on('join_room')
def join_personal_room(data):
    room = data.get('room')
    join_room(room)
    print(f"{room} joined their personal room")

# Optional: tutor joins class room to get updates on attendance
@socketio.on('join_class')
def join_class_room(data):
    room = data.get('class')
    join_room(room)
    print(f"Joined class room: {room}")

# Optional: emit tokens to trainees
def emit_tokens(tokens_dict):
    for trainee, token in tokens_dict.items():
        emit('new_token', {'trainee': trainee, 'token': token}, room=trainee)

# ------------------ User memory registration ------------------
def register_user_in_memory(username, userobj):
    users[username] = userobj
    if userobj['role'] == 'trainee':
        existing_names = {s['Name'] for s in students}
        if userobj.get('full_name') not in existing_names:
            students.append({"Name": userobj.get('full_name'),
                             "Assessment": userobj.get('assessment_number'),
                             "Class": userobj.get('class')})
            attendance_status[userobj.get('full_name')] = "Absent"
            for subj in all_subjects:
                subject_percentages.setdefault(userobj.get('full_name'), {})[subj] = 0
            if userobj.get('class') and userobj.get('class') not in classes:
                classes.append(userobj.get('class'))
                classes.sort()
                active_lessons[userobj.get('class')] = {'subject': None, 'tutor': None, 'active': False,
                                                        'session_start': None, 'session_end': None}
    else:
        subj_map = userobj.get('subjects', {}) or {}
        for subj in subj_map:
            if subj not in all_subjects:
                all_subjects.add(subj)
                for t in students:
                    subject_percentages.setdefault(t['Name'], {})[subj] = 0

# ------------------ Utility functions ------------------
def build_summary_for_class(class_name, subject):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for s in students:
        if s["Class"] != class_name:
            continue
        trainee_name = s["Name"]
        rows.append({
            "Name": trainee_name,
            "Assessment": s["Assessment"],
            "Attendance %": subject_percentages.get(trainee_name, {}).get(subject, 0),
            "Date": today,
            "Status": attendance_status.get(trainee_name, "Absent")
        })
    return rows

def auto_expire_if_needed(info, cls):
    if info.get('active') and info.get('session_end'):
        try:
            if datetime.fromisoformat(info['session_end']) < datetime.now():
                active_lessons[cls] = {'subject': None, 'tutor': None, 'active': False,
                                       'session_start': None, 'session_end': None}
                return active_lessons[cls]
        except Exception:
            active_lessons[cls] = {'subject': None, 'tutor': None, 'active': False,
                                   'session_start': None, 'session_end': None}
            return active_lessons[cls]
    return info

def generate_token():
    token = f"{random.randint(1000, 9999)}"
    expires = datetime.now() + timedelta(minutes=TOKEN_VALIDITY_MINUTES)
    return {"token": token, "expires": expires, "used": False}

def generate_device_hash(request):
    raw = (
        request.headers.get('User-Agent', '') +
        request.remote_addr +
        request.headers.get('Accept-Language', '')
    )
    return hashlib.sha256(raw.encode()).hexdigest()


# ------------------ Auth ------------------
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username').strip()
        password = request.form.get('password').strip()

        user = users.get(username)
        if not user or user['password'] != password:
            flash("Invalid username or password")
            return redirect(url_for('login'))

        # Save user info in session
        session['username'] = username
        session['role'] = user['role']
        session['full_name'] = user['full_name']

        # Trainee specific info
        if user['role'] == 'trainee':
            session['trainee_class'] = user['class']
            session['assessment_number'] = user['assessment_number']

            # Check if a valid token exists
            token_data = user_tokens.get(user['full_name'])
            if not token_data or datetime.now() > token_data['expires']:
                # No valid token yet
                return redirect(url_for('trainee_pre_dashboard'))
            else:
                # Token exists, show token page first
                return redirect(url_for('trainee_token_page'))

        # Tutors go straight to dashboard
        else:
            session['subjects'] = user.get('subjects', {})
            return redirect(url_for('tutor_proceed'))

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ------------------ Registration ------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        role = request.form.get('role')
        username = request.form.get('username').strip()
        password = request.form.get('password')
        if not username or not password:
            flash("Username and password are required.")
            return redirect(url_for('register'))
        if username in users:
            flash("Username already exists. Choose a different username.")
            return redirect(url_for('register'))

        if role == 'trainee':
            full_name = request.form.get('full_name', '').strip()
            class_name = request.form.get('class', '').strip()
            assessment_number = request.form.get('assessment_number', '').strip()
            userobj = {
                'password': password,
                'role': 'trainee',
                'full_name': full_name or username,
                'class': class_name,
                'assessment_number': assessment_number
            }
        else:
            full_name = request.form.get('full_name_tutor', '').strip()
            subjects_text = request.form.get('subjects', '')
            classes_text = request.form.get('classes', '')
            cls_list = [c.strip() for c in classes_text.split(',') if c.strip()]
            subj_map = {s.strip(): cls_list.copy() for s in subjects_text.split(',') if s.strip()}
            userobj = {
                'password': password,
                'role': 'tutor',
                'full_name': full_name or username,
                'subjects': subj_map
            }

        save_user_to_db(username, userobj)
        register_user_in_memory(username, userobj)
        flash("Account created successfully! Please login when a token is available.")
        return redirect(url_for('login'))

    return render_template('register.html')

# ------------------ Tutor Routes ------------------
@app.route('/tutor/proceed', methods=['GET', 'POST'])
def tutor_proceed():
    if session.get('role') != 'tutor':
        return redirect(url_for('login'))

    if request.method == 'POST':
        return redirect(url_for('tutor_select_subject'))

    return render_template('tutor_proceed.html', full_name=session.get('full_name'))

@app.route('/tutor/select_subject', methods=['GET', 'POST'])
def tutor_select_subject():
    if session.get('role') != 'tutor':
        return redirect(url_for('login'))
    subjects_map = session.get('subjects', {})
    subjects = sorted(subjects_map.keys())
    if request.method == 'POST':
        chosen = request.form.get('subject')
        if chosen not in subjects_map:
            return render_template('tutor_select_subject.html', subjects=subjects, error='Invalid subject')
        session['chosen_subject'] = chosen
        return redirect(url_for('tutor_select_class'))
    return render_template('tutor_select_subject.html', subjects=subjects)

@app.route('/tutor/select_class', methods=['GET', 'POST'])
def tutor_select_class():
    if session.get('role') != 'tutor':
        return redirect(url_for('login'))

    subj = session.get('chosen_subject')
    if not subj:
        return redirect(url_for('tutor_select_subject'))

    classes_for_subject = session['subjects'][subj]
    generated_tokens = None
    chosen_class = session.get('chosen_class')

    if request.method == 'POST':
        cls = request.form.get('class_name')
        session['chosen_class'] = cls
        chosen_class = cls

        if 'generate_tokens' in request.form:
            generated_tokens = {}

            for s in students:
                if s['Class'] == cls:
                    token_data = generate_token()
                    user_tokens[s['Name']] = token_data
                    generated_tokens[s['Name']] = token_data['token']

                    # 🔑 MATCH ROOM ID WITH TRAINEE
                    trainee_username = next(
                        u for u, obj in users.items()
                        if obj['role'] == 'trainee' and obj['full_name'] == s['Name']
                    )

                    # 🚀 PUSH TOKEN LIVE
                    socketio.emit(
                        'new_token',
                        {'token': token_data['token']},
                        room=trainee_username
                    )

            flash("Tokens generated and sent live to trainees.")

    return render_template(
        'tutor_select_class.html',
        subject=subj,
        classes=classes_for_subject,
        chosen_class=chosen_class,
        generated=generated_tokens
    )
@app.route('/tutor/generate_tokens', methods=['POST'])
def tutor_generate_tokens():
    if session.get('role') != 'tutor':
        return redirect(url_for('login'))

    chosen_class = request.form.get('class_name')
    if not chosen_class:
        flash("Select a class first")
        return redirect(url_for('tutor_select_class'))

    generated_tokens = {}
    for s in students:
        if s['Class'] == chosen_class:
            token_data = generate_token()
            user_tokens[s['Name']] = token_data
            generated_tokens[s['Name']] = token_data['token']

    flash("Tokens generated successfully!")
    subj = session.get('chosen_subject')
    classes_for_subject = session['subjects'][subj]
    return render_template('tutor_select_class.html', subject=subj, classes=classes_for_subject,
                           chosen_class=chosen_class, generated=generated_tokens)

 
# ------------------ Trainee pre-dashboard (friendly page) ------------------
@app.route('/trainee/pre_dashboard')
def trainee_pre_dashboard():
    if session.get('role') != 'trainee':
        return redirect(url_for('login'))

    trainee_name = session.get('full_name')
    token_data = user_tokens.get(trainee_name)

    # ✅ CHECK TOKEN ON EVERY PAGE LOAD
    if token_data and datetime.now() < token_data['expires']:
        return redirect(url_for('trainee_token_page'))

    flash("Your tutor has not generated a token yet. Stay motivated! 💪")
    return render_template(
        'trainee_pre_dashboard.html',
        full_name=trainee_name
    )
# ------------------ Trainee token page ------------------
@app.route('/trainee/token', methods=['GET', 'POST'])
def trainee_token_page():
    if session.get('role') != 'trainee':
        return redirect(url_for('login'))

    trainee_name = session.get('full_name')
    token_data = user_tokens.get(trainee_name)

    # If no token yet, redirect to friendly pre-dashboard
    if not token_data or datetime.now() > token_data['expires']:
        return redirect(url_for('trainee_pre_dashboard'))

    if request.method == 'POST':
        entered_token = request.form.get('token')
        if entered_token == token_data['token']:
            user_tokens.pop(trainee_name)  # token used
            return redirect(url_for('trainee_home'))
        else:
            flash("Invalid token. Please check with your tutor.")
            return redirect(url_for('trainee_token_page'))

    # ✅ Pass current token to template
    return render_template('trainee_token.html', token=token_data['token'])

@app.route('/trainee/latest_token')
def trainee_latest_token():
    if session.get('role') != 'trainee':
        return jsonify({"token": None})

    trainee_name = session.get('full_name')
    token_data = user_tokens.get(trainee_name)

    token = (
        token_data['token']
        if token_data and datetime.now() < token_data['expires']
        else None
    )

    return jsonify({"token": token})

@app.route('/tutor/summary', methods=['GET', 'POST'])
def tutor_summary():
    # ---------------- CHECK ROLE ----------------
    if session.get('role') != 'tutor':
        return redirect(url_for('login'))

    subj = session.get('chosen_subject')
    cls = session.get('chosen_class')
    if not subj or not cls:
        return redirect(url_for('tutor_select_subject'))

    info = active_lessons.get(cls, {})
    info = auto_expire_if_needed(info, cls)
    summary_rows = build_summary_for_class(cls, subj)

    # ---------------- HANDLE FORM POSTS ----------------
    if request.method == 'POST':
        action = request.form.get('action')
        now = datetime.now()

        # ---------------- START LESSON ----------------
        if action == 'start':
            active_lessons[cls] = {
                'subject': subj,
                'tutor': session.get('full_name'),
                'active': True,
                'session_start': now.isoformat(),
                'session_end': (now + timedelta(minutes=60)).isoformat()
            }
            session['active_lesson_id'] = f"{subj}_{cls}"

            socketio.emit(
                'lesson_activated',
                {'class': cls, 'subject': subj, 'tutor': session.get('full_name')}
            )

            flash("Lesson started successfully.")
            return redirect(url_for('tutor_summary'))

        # ---------------- STOP LESSON ----------------
        elif action == 'stop':
            active_lessons[cls] = {
                'subject': None,
                'tutor': None,
                'active': False,
                'session_start': None,
                'session_end': None
            }
            session.pop('active_lesson_id', None)
            flash("Lesson stopped.")
            return redirect(url_for('tutor_summary'))

        # ---------------- EXPORT PDF ----------------
        elif action == 'export_pdf':
            # Absolute path for logo
            logo_path = os.path.join(current_app.root_path, 'static', 'logo.png')
            school_name = "MTTC College"

            rendered_html = render_template(
                'tutor_dashboard_pdf.html',  # separate PDF template for PDF output
                full_name=session.get('full_name'),
                subject=subj,
                class_name=cls,
                current_date=datetime.now().strftime("%Y-%m-%d"),
                summary=summary_rows,
                logo_path=logo_path,
                school_name=school_name
            )

            pdf = io.BytesIO()
            pisa_status = pisa.CreatePDF(rendered_html, dest=pdf)

            if pisa_status.err:
                flash("Failed to generate PDF.")
                return redirect(url_for('tutor_summary'))

            response = make_response(pdf.getvalue())
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'attachment; filename=attendance_{subj}_{cls}.pdf'
            return response

    # ---------------- NORMAL PAGE LOAD ----------------
    return render_template(
        'tutor_dashboard_summary.html',
        full_name=session.get('full_name'),
        subject=subj,
        class_name=cls,
        current_date=datetime.now().strftime("%Y-%m-%d"),
        active=info.get('active', False),
        session_start=info.get('session_start'),
        session_end=info.get('session_end'),
        summary=summary_rows
    )
# ------------------ Tutor Marking ------------------
@app.route('/tutor/mark_present/<name>', methods=['POST'])
def tutor_mark_present(name):
    trainee_name = name
    if session.get('role') != 'tutor':
        return redirect(url_for('login'))
    subj = session.get('chosen_subject'); cls = session.get('chosen_class')
    lesson_id = f"{subj}_{cls}"
    if lesson_id not in marked_students:
        marked_students[lesson_id] = set()
    if name in marked_students[lesson_id]:
        flash(f"{name} has already been marked for this lesson!")
        return redirect(url_for('tutor_summary'))
    attendance_status[name] = "Present"
    current_pct = subject_percentages[name].get(subj, 0)
    subject_percentages[name][subj] = min(100, current_pct + 5)
    assessment = next((s["Assessment"] for s in students if s["Name"] == name), "")
    attendance_history.append({"date": datetime.now().strftime("%Y-%m-%d"), "class": cls, "subject": subj,
                               "trainee": name, "assessment": assessment, "status": "Present"})
    marked_students[lesson_id].add(name)
    flash(f"{name} marked as present.")
    return redirect(url_for('tutor_summary'))

@app.route('/tutor/mark_absent/<name>', methods=['POST'])
def tutor_mark_absent(name):
    if session.get('role') != 'tutor':
        return redirect(url_for('login'))
    subj = session.get('chosen_subject'); cls = session.get('chosen_class')
    lesson_id = f"{subj}_{cls}"
    if lesson_id not in marked_students:
        marked_students[lesson_id] = set()
    if name in marked_students[lesson_id]:
        flash(f"{name} has already been marked for this lesson!")
        return redirect(url_for('tutor_summary'))
    attendance_status[name] = "Absent"
    assessment = next((s["Assessment"] for s in students if s["Name"] == name), "")
    attendance_history.append({"date": datetime.now().strftime("%Y-%m-%d"), "class": cls, "subject": subj,
                               "trainee": name, "assessment": assessment, "status": "Absent"})
    marked_students[lesson_id].add(name)
    flash(f"{name} marked as absent.")
    return redirect(url_for('tutor_summary'))

@app.route('/tutor/history')
def tutor_history():
    if session.get('role') != 'tutor':
        return redirect(url_for('login'))
    subj = session.get('chosen_subject'); cls = session.get('chosen_class')
    records = [r for r in attendance_history if r['subject'] == subj and r['class'] == cls]
    return render_template('tutor_history.html', subject=subj, class_name=cls, records=records)

# ------------------ Trainee Routes ------------------
@app.route('/trainee/home')
def trainee_home():
    if session.get('role') != 'trainee':
        return redirect(url_for('login'))
    trainee_name = session['full_name']
    trainee_class = session.get('trainee_class')
    assessment_number = session['assessment_number']
    info = active_lessons.get(trainee_class, {})
    info = auto_expire_if_needed(info, trainee_class)
    active_lesson = info.get('active', False)
    return render_template('trainee_home.html', full_name=trainee_name, assessment_number=assessment_number,
                           trainee_class=trainee_class, subject_percentages=subject_percentages.get(trainee_name, {}),
                           active_lesson=active_lesson)

@app.route('/trainee/active')
def trainee_active_lesson():
    if session.get('role') != 'trainee':
        return redirect(url_for('login'))
    trainee_class = session.get('trainee_class')
    info = active_lessons.get(trainee_class, {})
    info = auto_expire_if_needed(info, trainee_class)
    if not info.get('active'):
        return redirect(url_for('trainee_home'))
    return render_template('trainee_active_lesson.html', tutor=info.get('tutor'),
                           class_name=trainee_class, subject=info.get('subject'),
                           session_end=info.get('session_end'))

@app.route('/trainee/mark_present', methods=['POST'])
def mark_present_page():
    # --- SECURITY CHECK ---
    if session.get('role') != 'trainee':
        return redirect(url_for('login'))

    trainee_name = session.get('full_name') 
    trainee_class = session.get('trainee_class')
    assessment_number = session.get('assessment_number')

    # --- ACTIVE LESSON CHECK ---
    info = active_lessons.get(trainee_class, {})
    info = auto_expire_if_needed(info, trainee_class)

    if not info.get('active'):
        flash("No active lesson at the moment.")
        return redirect(url_for('trainee_home'))

    subj = info.get('subject')
    lesson_id = f"{subj}_{trainee_class}"

    if lesson_id not in marked_students:
        marked_students[lesson_id] = set()

    if lesson_id not in lesson_devices:
        lesson_devices[lesson_id] = set()

    device_hash = generate_device_hash(request)

    if device_hash in lesson_devices[lesson_id]:
        flash("This device has already been used to mark attendance for this lesson.")
        return redirect(url_for('trainee_home'))

    if trainee_name in marked_students[lesson_id]:
        flash("You have already marked your attendance for this lesson!")
        return redirect(url_for('trainee_home'))

    attendance_status[trainee_name] = "Present"

    current_pct = subject_percentages[trainee_name].get(subj, 0)
    subject_percentages[trainee_name][subj] = min(100, current_pct + 5)

    attendance_history.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "class": trainee_class,
        "subject": subj,
        "trainee": trainee_name,
        "assessment": assessment_number,
        "status": "Present",
        "percentage": subject_percentages[trainee_name][subj]
    })

    marked_students[lesson_id].add(trainee_name)
    lesson_devices[lesson_id].add(device_hash)

    # ✅ ONLY FIX: MOVE THIS INSIDE FUNCTION
    socketio.emit('attendance_marked', {
        'trainee': trainee_name,
        'status': 'Present',
        'subject': subj,
        'class': trainee_class,
        'percentage': subject_percentages[trainee_name][subj]
    }, room=trainee_class)

    flash("Attendance marked successfully!")
    return redirect(url_for('trainee_home'))

# When trainee connects to listen for their class tokens
@socketio.on('join_class')
def handle_join_class(data):
    trainee_class = data.get('class')
    if trainee_class:
        join_room(trainee_class)

# When tutor generates tokens, push to the class
def push_tokens_to_class(cls, tokens):
    socketio.emit('new_tokens', tokens, room=cls)


# ------------------ Run Server ------------------
if __name__ == "__main__":
    init_db()
    db_users = load_users_from_db()
    users.update(db_users)

    students = [{"Name": u["full_name"], "Assessment": u.get("assessment_number", ""), "Class": u.get("class", "")}
                for u in users.values() if u["role"] == "trainee"]
    attendance_status = {s["Name"]: "Absent" for s in students}

    all_subjects = set()
    for u in users.values():
        if u.get('subjects'):
            for subj in u['subjects'].keys():
                all_subjects.add(subj)
    for s in students:
        all_subjects.update(['English', 'Indigenous', 'Mathematics', 'Science'])
    subject_percentages = {s["Name"]: {subj: 0 for subj in all_subjects} for s in students}

    classes = sorted({s["Class"] for s in students})
    active_lessons = {cls: {'subject': None, 'tutor': None, 'active': False,
                            'session_start': None, 'session_end': None} for cls in classes}

    socketio.run(app, host="0.0.0.0", port=5000)
