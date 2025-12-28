from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response
import os, uuid, sqlite3, random
from datetime import datetime, timedelta
import csv, io, zipfile

# -------------------------
# App setup
# -------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

# ✅ 生产环境建议用环境变量（Railway Variables 里设置 SECRET_KEY）
app.secret_key = os.environ.get("SECRET_KEY", "dev")

print("=== TEMPLATE FOLDER ===")
print(app.template_folder)

# -------------------------
# Experiment constants
# -------------------------
MAX_TURNS = 20
T1_THRESHOLD = 10

# T2 延迟（测试用 0；正式上线改成 7）
T2_DELAY_DAYS = int(os.environ.get("T2_DELAY_DAYS", "0"))

# -------------------------
# DB helpers
# -------------------------
DB_PATH = os.environ.get("DB_PATH", "/data/experiment.db")

def db_conn():
    # ✅ 确保目录存在（没挂载 volume 时至少不崩）
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    conn = db_conn()
    cur = conn.cursor()

    # 1) participants
    cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        participant_id TEXT PRIMARY KEY,
        consent_time   TEXT,
        created_at     TEXT NOT NULL
    );
    """)

    # 2) baseline
    cur.execute("""
    CREATE TABLE IF NOT EXISTS baseline (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        participant_id TEXT NOT NULL UNIQUE,
        grade_major    TEXT NOT NULL,
        culture_course TEXT,
        chatbot_exp    TEXT,
        stress_1w      TEXT,
        created_at     TEXT NOT NULL,
        FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_baseline_pid ON baseline(participant_id);")

    # 3) material_choice
    cur.execute("""
    CREATE TABLE IF NOT EXISTS material_choice (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        participant_id   TEXT NOT NULL UNIQUE,
        chosen_direction TEXT NOT NULL,
        chosen_label     TEXT,
        page_time        TEXT,
        choice_time      TEXT NOT NULL,
        rt_ms            INTEGER,
        user_agent       TEXT,
        FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_material_choice_direction ON material_choice(chosen_direction);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_material_choice_pid ON material_choice(participant_id);")

    # 4) condition_assign
    cur.execute("""
    CREATE TABLE IF NOT EXISTS condition_assign (
        participant_id      TEXT PRIMARY KEY,
        condition_planning  TEXT NOT NULL CHECK(condition_planning IN ('pre','none')),
        condition_feedback  TEXT NOT NULL CHECK(condition_feedback IN ('focused','generic')),
        assigned_at         TEXT NOT NULL,
        FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_condition_assign_pid ON condition_assign(participant_id);")

    # 5) planning_input
    cur.execute("""
    CREATE TABLE IF NOT EXISTS planning_input (
        participant_id          TEXT PRIMARY KEY,
        plan_goal               TEXT NOT NULL,
        plan_audience_context   TEXT NOT NULL,
        plan_elements           TEXT NOT NULL,
        plan_output             TEXT NOT NULL,
        created_at              TEXT NOT NULL,
        FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
    );
    """)

    # 6) chat_log
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        participant_id TEXT NOT NULL,
        turn_id        INTEGER NOT NULL,
        role           TEXT NOT NULL CHECK(role IN ('user','assistant')),
        text           TEXT NOT NULL,
        ts             TEXT NOT NULL,
        FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_pid_turn ON chat_log(participant_id, turn_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_pid_role ON chat_log(participant_id, role);")

    # 7) survey_t1
    cur.execute("""
    CREATE TABLE IF NOT EXISTS survey_t1 (
        participant_id TEXT PRIMARY KEY,

        triggered_interest_1 INTEGER, triggered_interest_2 INTEGER, triggered_interest_3 INTEGER,
        support_1 INTEGER, support_2 INTEGER, support_3 INTEGER, support_4 INTEGER,
        clarity_1 INTEGER, clarity_2 INTEGER, clarity_3 INTEGER, clarity_4 INTEGER,
        task_1 INTEGER, task_2 INTEGER, task_3 INTEGER,
        affect_1 INTEGER, affect_2 INTEGER, affect_3 INTEGER,
        manip_plan INTEGER, manip_feedback INTEGER,

        created_at TEXT NOT NULL,
        FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
    );
    """)

    # 8) survey_t2
    cur.execute("""
    CREATE TABLE IF NOT EXISTS survey_t2 (
        participant_id TEXT PRIMARY KEY,

        maintained_interest_1 INTEGER, maintained_interest_2 INTEGER, maintained_interest_3 INTEGER,
        support_1 INTEGER, support_2 INTEGER, support_3 INTEGER,
        clarity_1 INTEGER, clarity_2 INTEGER, clarity_3 INTEGER,
        cont_intent_1 INTEGER,

        created_at TEXT NOT NULL,
        FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
    );
    """)

    conn.commit()
    conn.close()


# ✅ 只调用一次：在定义后、路由前
init_db()

# -------------------------
# pid helper：URL 优先，其次 session
# -------------------------
def get_pid_from_request():
    pid = (request.args.get("pid") or "").strip()
    if pid:
        return pid
    return (session.get("participant_id") or "").strip()


# -------------------------
# Condition assignment (Quota)
# -------------------------
def get_or_assign_condition(participant_id: str):
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE;")

        cur.execute("""
          INSERT OR IGNORE INTO participants(participant_id, created_at)
          VALUES (?, datetime('now'))
        """, (participant_id,))

        row = cur.execute("""
          SELECT condition_planning, condition_feedback
          FROM condition_assign
          WHERE participant_id=?
        """, (participant_id,)).fetchone()

        if row:
            conn.commit()
            conn.close()
            return row["condition_planning"], row["condition_feedback"]

        cells = [
            ("pre",  "focused"),
            ("pre",  "generic"),
            ("none", "focused"),
            ("none", "generic"),
        ]

        counts = {}
        for p, f in cells:
            c = cur.execute("""
              SELECT COUNT(*) AS c
              FROM condition_assign
              WHERE condition_planning=? AND condition_feedback=?
            """, (p, f)).fetchone()["c"]
            counts[(p, f)] = int(c)

        min_count = min(counts.values())
        candidate_cells = [cell for cell, c in counts.items() if c == min_count]
        planning, feedback = random.choice(candidate_cells)

        cur.execute("""
          INSERT INTO condition_assign(participant_id, condition_planning, condition_feedback, assigned_at)
          VALUES (?, ?, ?, datetime('now'))
        """, (participant_id, planning, feedback))

        conn.commit()
        conn.close()
        return planning, feedback

    except Exception:
        conn.rollback()
        conn.close()
        raise


# -------------------------
# Assistant reply (rule-based)
# -------------------------
def generate_assistant_reply(planning_cond: str, feedback_cond: str, user_text: str, turn_id=None) -> str:
    t = int(turn_id or 1)
    u = (user_text or "").strip()

    focused_1_10 = {
        1: "（聚焦反馈）我们开始吧。我先对齐你的目标：把想法变得更清晰并可推进。\n你想从哪个点切入？（载体 / 文化元素 / 故事氛围 / 符号颜色）",
        2: "（聚焦反馈）继续说说你的选择：你为什么更偏向这个方向？给我 1–2 个关键词就行。",
        3: "（聚焦反馈）你想做的成品更像什么？（海报/包装/空间导视/交互界面/短视频封面…）选一个最像的。",
        4: "（聚焦反馈）你希望面向谁？（同龄人/游客/本地居民/学生/亲子…）给一个目标受众 + 一个使用场景。",
        5: "（聚焦反馈）选 1 个最核心的文化元素：纹样/器物/工艺/仪式/故事。\n你最想用哪个？（写一个即可）",
        6: "（聚焦反馈）为它加 2 个形容词：质朴/精致/热烈/神秘/克制/现代/传统… 你选哪两个？",
        7: "（聚焦反馈）确定视觉锚点：你希望突出“图形符号”还是“故事画面”？二选一。",
        8: "（聚焦反馈）来一句话概念（15字以内）：用“把___变成___”的句式写一下，我帮你润色。",
        9: "（聚焦反馈）最后校验风格：你希望整体更“现代极简”还是“传统丰富”？二选一。",
        10:"（聚焦反馈）总结一下：\n- 载体：{carrier}\n- 受众/场景：{aud}\n- 核心元素：{elem}\n- 气质：{adj}\n- 概念句：{concept}\n\n如果你同意，建议下一步：①列3个参考 ②画2版构图草图。"
    }

    generic_1_10 = {
        1: "（通用反馈）我们开始吧。你想先从哪个点说起：载体 / 文化元素 / 故事氛围 / 符号颜色？",
        2: "（通用反馈）为什么选这个方向？给我 1–2 个关键词就好。",
        3: "（通用反馈）你想做的成品更像什么？（海报/包装/导视/界面/封面…）",
        4: "（通用反馈）给一个目标受众 + 一个使用场景。",
        5: "（通用反馈）选 1 个最核心的文化元素（纹样/器物/工艺/仪式/故事…）。",
        6: "（通用反馈）再加 2 个形容词（比如热烈/克制/现代/传统…）。",
        7: "（通用反馈）更想突出“图形符号”还是“故事画面”？",
        8: "（通用反馈）写一句 15 字以内的概念句（“把___变成___”）。",
        9: "（通用反馈）更偏“现代极简”还是“传统丰富”？",
        10:"（通用反馈）我们把要点收一下：载体/受众/元素/气质/概念句。下一步建议做参考收集+草图。"
    }

    focused_11_20 = {
        11:"（反思阶段）我们退一步看整体：你觉得目前概念里最清晰的一点是什么？（一句话）",
        12:"（反思阶段）那最模糊/最不确定的一点是什么？（一句话）",
        13:"（反思阶段）如果让它更可落地：你愿意优先改“内容表达”还是“形式呈现”？二选一。",
        14:"（反思阶段）给它一个明确的核心信息（10–15字）：你希望观众看完记住什么？",
        15:"（反思阶段）做一次风险检查：最可能被误解的地方是什么？你想怎么避免？",
        16:"（反思阶段）给 3 个关键词作为设计约束（例如：材质/色彩/符号风格）。你给哪 3 个？",
        17:"（反思阶段）请列 2 个你想参考的方向（品牌/作品类型/风格流派都行），为什么？",
        18:"（反思阶段）如果把它做成 A/B 两个版本：A更传统，B更当代。你更想保留哪一点不变？",
        19:"（反思阶段）自评一下：现在你对这个方案的清晰度从 1–7 你给几分？为什么？",
        20:"（反思阶段）最后收束：①你下一步最可执行的一件事是什么？②你希望我继续帮你做“润色概念句”还是“拆成制作清单”？"
    }

    generic_11_20 = {
        11:"（反思）你觉得现在最清楚的一点是什么？（一句话）",
        12:"（反思）你觉得最不确定的一点是什么？（一句话）",
        13:"（反思）想继续完善的话，你更想改内容还是改形式？",
        14:"（反思）用 10–15 字写一句核心信息：你希望观众记住什么？",
        15:"（反思）你担心它会被怎么误解？",
        16:"（反思）给 3 个关键词当作约束（材质/色彩/符号风格）。",
        17:"（反思）列 2 个你想参考的方向，并说原因。",
        18:"（反思）如果做 A/B 两版（传统/当代），你更想保留什么不变？",
        19:"（反思）你对现在方案清晰度 1–7 给几分？为什么？",
        20:"（反思）最后：你下一步最可执行的一件事是什么？"
    }

    # session 记忆（用于第10轮填空）
    try:
        mem = session.setdefault("chat_mem", {})
        if t == 3: mem["carrier"] = u
        if t == 4: mem["aud"] = u
        if t == 5: mem["elem"] = u
        if t == 6: mem["adj"] = u
        if t == 8: mem["concept"] = u
        session["chat_mem"] = mem
    except Exception:
        mem = {}

    if feedback_cond == "focused":
        if t <= 10:
            if t == 10:
                return focused_1_10[10].format(
                    carrier=mem.get("carrier", "（未记录）"),
                    aud=mem.get("aud", "（未记录）"),
                    elem=mem.get("elem", "（未记录）"),
                    adj=mem.get("adj", "（未记录）"),
                    concept=mem.get("concept", "（未记录）"),
                )
            return focused_1_10.get(t, "（聚焦反馈）继续说说你的想法，我来帮你推进。")
        return focused_11_20.get(t, "（反思阶段）你愿意补充一句：你现在最想把哪一点变得更清楚？")

    else:
        if t <= 10:
            return generic_1_10.get(t, "（通用反馈）继续说说你的想法，我来帮你推进。")
        return generic_11_20.get(t, "（反思）你现在最想补充说明哪一点？")


# -------------------------
# T2 eligibility
# -------------------------
def get_t2_eligibility(pid: str):
    conn = db_conn()
    row = conn.execute(
        "SELECT created_at FROM survey_t1 WHERE participant_id=?",
        (pid,)
    ).fetchone()
    conn.close()

    if not row:
        return (False, None, "t1_not_submitted")

    try:
        t1_at = datetime.fromisoformat(row["created_at"])
    except Exception:
        return (False, None, "t1_time_parse_error")

    eligible_at = t1_at + timedelta(days=T2_DELAY_DAYS)
    now = datetime.utcnow()

    if now < eligible_at:
        return (False, eligible_at.isoformat(), "too_early")

    return (True, eligible_at.isoformat(), "ok")


# -------------------------
# Export helpers (token)
# -------------------------
def require_export_token():
    token_env = os.environ.get("EXPORT_TOKEN", "").strip()
    if not token_env:
        return None  # 未设置则不要求（不推荐）
    token = (request.args.get("token") or "").strip()
    if token != token_env:
        return ("Forbidden", 403)
    return None


# -------------------------
# Routes
# -------------------------
@app.route("/")
def home():
    return redirect(url_for("consent"))


@app.route("/consent", methods=["GET", "POST"])
def consent():
    if request.method == "GET":
        return render_template("consent.html")

    participant_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO participants (participant_id, consent_time, created_at)
        VALUES (?, ?, ?)
    """, (participant_id, now, now))
    conn.commit()
    conn.close()

    session["participant_id"] = participant_id
    return redirect(url_for("baseline_page", pid=participant_id))


@app.route("/baseline", methods=["GET", "POST"])
def baseline_page():
    pid = get_pid_from_request()
    if not pid:
        return "Missing pid", 400

    if request.method == "GET":
        return render_template("baseline.html", participant_id=pid)

    grade_major = (request.form.get("grade_major") or "").strip()
    culture_course = (request.form.get("culture_course") or "").strip()
    chatbot_exp = (request.form.get("chatbot_exp") or "").strip()
    stress_1w = (request.form.get("stress_1w") or "").strip()

    if not grade_major:
        return "grade_major required", 400

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO baseline(participant_id, grade_major, culture_course, chatbot_exp, stress_1w, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(participant_id) DO UPDATE SET
        grade_major=excluded.grade_major,
        culture_course=excluded.culture_course,
        chatbot_exp=excluded.chatbot_exp,
        stress_1w=excluded.stress_1w
    """, (pid, grade_major, culture_course, chatbot_exp, stress_1w, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

    return redirect(url_for("material_page", pid=pid))


@app.route("/api/baseline", methods=["POST"])
def api_baseline():
    data = request.get_json(force=True)

    pid = (data.get("participant_id") or "").strip()
    grade_major = (data.get("grade_major") or "").strip()
    culture_course = (data.get("culture_course") or "").strip()
    chatbot_exp = (data.get("chatbot_exp") or "").strip()
    stress_1w = (data.get("stress_1w") or "").strip()

    if not pid or not grade_major:
        return jsonify({"ok": False, "error": "missing participant_id or grade_major"}), 400

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO baseline(participant_id, grade_major, culture_course, chatbot_exp, stress_1w, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(participant_id) DO UPDATE SET
        grade_major=excluded.grade_major,
        culture_course=excluded.culture_course,
        chatbot_exp=excluded.chatbot_exp,
        stress_1w=excluded.stress_1w
    """, (pid, grade_major, culture_course, chatbot_exp, stress_1w, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "next": url_for("material_page", pid=pid)})


@app.route("/material")
def material_page():
    pid = get_pid_from_request()
    if not pid:
        return "Missing pid", 400
    return render_template("material.html", participant_id=pid)


@app.route("/api/material_choice", methods=["POST"])
def api_material_choice():
    data = request.get_json(force=True)
    pid = (data.get("participant_id") or "").strip()
    choice = (data.get("choice") or "").strip()
    label = (data.get("label") or "").strip()
    page_time = (data.get("page_time") or "").strip()
    rt_ms = data.get("rt_ms", None)
    user_agent = request.headers.get("User-Agent", "")

    if not pid or not choice:
        return jsonify({"ok": False, "error": "missing participant_id or choice"}), 400

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO material_choice
      (participant_id, chosen_direction, chosen_label, page_time, choice_time, rt_ms, user_agent)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(participant_id) DO UPDATE SET
      chosen_direction=excluded.chosen_direction,
      chosen_label=excluded.chosen_label,
      page_time=excluded.page_time,
      choice_time=excluded.choice_time,
      rt_ms=excluded.rt_ms,
      user_agent=excluded.user_agent
    """, (pid, choice, label, page_time, datetime.utcnow().isoformat(), rt_ms, user_agent))
    conn.commit()
    conn.close()

    get_or_assign_condition(pid)
    return jsonify({"ok": True})


@app.route("/planning", methods=["GET", "POST"])
def planning_page():
    pid = get_pid_from_request()
    if not pid:
        return "Missing pid", 400

    planning_cond, feedback_cond = get_or_assign_condition(pid)

    if planning_cond != "pre":
        return redirect(url_for("chat_page", pid=pid))

    if request.method == "GET":
        return render_template("planning.html", participant_id=pid)

    plan_goal = (request.form.get("plan_goal") or "").strip()
    plan_audience_context = (request.form.get("plan_audience_context") or "").strip()
    plan_elements = (request.form.get("plan_elements") or "").strip()
    plan_output = (request.form.get("plan_output") or "").strip()

    if not (plan_goal and plan_audience_context and plan_elements and plan_output):
        return "All planning fields required", 400

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO planning_input(participant_id, plan_goal, plan_audience_context, plan_elements, plan_output, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(participant_id) DO UPDATE SET
        plan_goal=excluded.plan_goal,
        plan_audience_context=excluded.plan_audience_context,
        plan_elements=excluded.plan_elements,
        plan_output=excluded.plan_output
    """, (pid, plan_goal, plan_audience_context, plan_elements, plan_output, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

    return redirect(url_for("chat_page", pid=pid))


@app.route("/chat")
def chat_page():
    pid = get_pid_from_request()
    if not pid:
        return "Missing pid", 400

    planning_cond, feedback_cond = get_or_assign_condition(pid)

    if planning_cond == "pre":
        conn = db_conn()
        row = conn.execute(
            "SELECT 1 FROM planning_input WHERE participant_id=?",
            (pid,)
        ).fetchone()
        conn.close()
        if not row:
            return redirect(url_for("planning_page", pid=pid))

    conn = db_conn()
    user_turns = conn.execute("""
      SELECT COUNT(*) AS c FROM chat_log
      WHERE participant_id=? AND role='user'
    """, (pid,)).fetchone()["c"]
    conn.close()

    return render_template(
        "chat.html",
        participant_id=pid,
        planning_cond=planning_cond,
        feedback_cond=feedback_cond,
        user_turns=user_turns
    )


@app.route("/api/chat_send", methods=["POST"])
def api_chat_send():
    try:
        data = request.get_json(force=True)
        pid = (data.get("participant_id") or "").strip()
        user_text = (data.get("text") or "").strip()

        if not pid or not user_text:
            return jsonify({"ok": False, "error": "missing participant_id or text"}), 400

        conn = db_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT OR IGNORE INTO participants(participant_id, created_at)
            VALUES (?, datetime('now'))
        """, (pid,))
        conn.commit()

        planning_cond, feedback_cond = get_or_assign_condition(pid)

        current_user_turns = cur.execute("""
            SELECT COUNT(*) AS c FROM chat_log
            WHERE participant_id=? AND role='user'
        """, (pid,)).fetchone()["c"]
        next_turn_id = int(current_user_turns) + 1

        if next_turn_id > MAX_TURNS:
            conn.close()
            return jsonify({"ok": False, "error": "max_turns_reached"}), 400

        now = datetime.utcnow().isoformat()

        cur.execute("""
            INSERT INTO chat_log(participant_id, turn_id, role, text, ts)
            VALUES (?, ?, 'user', ?, ?)
        """, (pid, next_turn_id, user_text, now))

        assistant_text = generate_assistant_reply(
            planning_cond=planning_cond,
            feedback_cond=feedback_cond,
            user_text=user_text,
            turn_id=next_turn_id
        )

        cur.execute("""
            INSERT INTO chat_log(participant_id, turn_id, role, text, ts)
            VALUES (?, ?, 'assistant', ?, ?)
        """, (pid, next_turn_id, assistant_text, now))

        conn.commit()
        conn.close()

        can_finish = (next_turn_id >= T1_THRESHOLD)

        return jsonify({
            "ok": True,
            "turn_id": next_turn_id,
            "assistant": assistant_text,
            "can_finish": can_finish,
            "t1_threshold": T1_THRESHOLD,
            "max_turns": MAX_TURNS
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)}"}), 500


@app.route("/t1", methods=["GET", "POST"])
def t1_page():
    pid = (request.args.get("pid") or "").strip()
    if not pid:
        return "Missing pid", 400

    if request.method == "GET":
        return render_template("survey_t1.html", participant_id=pid)

    def as_int(name):
        v = request.form.get(name, "")
        return int(v) if str(v).isdigit() else None

    payload = (
        pid,
        as_int("ti1"), as_int("ti2"), as_int("ti3"),
        as_int("s1"), as_int("s2"), as_int("s3"), as_int("s4"),
        as_int("c1"), as_int("c2"), as_int("c3"), as_int("c4"),
        as_int("task1"), as_int("task2"), as_int("task3"),
        as_int("aff1"), as_int("aff2"), as_int("aff3"),
        as_int("mplan"), as_int("mfb"),
        datetime.utcnow().isoformat()
    )

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO survey_t1(
        participant_id,
        triggered_interest_1, triggered_interest_2, triggered_interest_3,
        support_1, support_2, support_3, support_4,
        clarity_1, clarity_2, clarity_3, clarity_4,
        task_1, task_2, task_3,
        affect_1, affect_2, affect_3,
        manip_plan, manip_feedback,
        created_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(participant_id) DO UPDATE SET
        triggered_interest_1=excluded.triggered_interest_1,
        triggered_interest_2=excluded.triggered_interest_2,
        triggered_interest_3=excluded.triggered_interest_3,
        support_1=excluded.support_1,
        support_2=excluded.support_2,
        support_3=excluded.support_3,
        support_4=excluded.support_4,
        clarity_1=excluded.clarity_1,
        clarity_2=excluded.clarity_2,
        clarity_3=excluded.clarity_3,
        clarity_4=excluded.clarity_4,
        task_1=excluded.task_1,
        task_2=excluded.task_2,
        task_3=excluded.task_3,
        affect_1=excluded.affect_1,
        affect_2=excluded.affect_2,
        affect_3=excluded.affect_3,
        manip_plan=excluded.manip_plan,
        manip_feedback=excluded.manip_feedback,
        created_at=excluded.created_at
    """, payload)
    conn.commit()
    conn.close()

    return render_template("done_t1.html", participant_id=pid)


@app.route("/t2", methods=["GET", "POST"])
def t2_page():
    pid = get_pid_from_request()
    if not pid:
        return "Missing pid", 400

    ok, eligible_at_iso, reason = get_t2_eligibility(pid)

    if not ok:
        if reason == "t1_not_submitted":
            return redirect(url_for("t1_page", pid=pid))
        return render_template("t2_locked.html", participant_id=pid, eligible_at=eligible_at_iso)

    if request.method == "GET":
        return render_template("t2.html", participant_id=pid)

    def as_int(name):
        v = request.form.get(name, "")
        return int(v) if str(v).isdigit() else None

    payload = (
        pid,
        as_int("mi1"), as_int("mi2"), as_int("mi3"),
        as_int("s1"), as_int("s2"), as_int("s3"),
        as_int("c1"), as_int("c2"), as_int("c3"),
        as_int("cont1"),
        datetime.utcnow().isoformat()
    )

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO survey_t2(
        participant_id,
        maintained_interest_1, maintained_interest_2, maintained_interest_3,
        support_1, support_2, support_3,
        clarity_1, clarity_2, clarity_3,
        cont_intent_1,
        created_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(participant_id) DO UPDATE SET
        maintained_interest_1=excluded.maintained_interest_1,
        maintained_interest_2=excluded.maintained_interest_2,
        maintained_interest_3=excluded.maintained_interest_3,
        support_1=excluded.support_1,
        support_2=excluded.support_2,
        support_3=excluded.support_3,
        clarity_1=excluded.clarity_1,
        clarity_2=excluded.clarity_2,
        clarity_3=excluded.clarity_3,
        cont_intent_1=excluded.cont_intent_1,
        created_at=excluded.created_at
    """, payload)
    conn.commit()
    conn.close()

    return render_template("done_t2.html", participant_id=pid)


# -------------------------
# Debug counts
# -------------------------
@app.route("/_debug/counts")
def debug_counts():
    conn = db_conn()
    cur = conn.cursor()

    def q(sql):
        cur.execute(sql)
        return cur.fetchone()[0]

    counts = {
        "participants": q("SELECT COUNT(*) FROM participants;"),
        "condition_assign": q("SELECT COUNT(*) FROM condition_assign;"),
        "baseline": q("SELECT COUNT(*) FROM baseline;"),
        "material_choice": q("SELECT COUNT(*) FROM material_choice;"),
        "planning_input": q("SELECT COUNT(*) FROM planning_input;"),
        "chat_log": q("SELECT COUNT(*) FROM chat_log;"),
        "survey_t1": q("SELECT COUNT(*) FROM survey_t1;"),
        "survey_t2": q("SELECT COUNT(*) FROM survey_t2;"),
    }

    conn.close()
    return jsonify(counts)


# -------------------------
# Export: single table (CSV)
#   /_export/survey_t1?token=xxx
# -------------------------
@app.route("/_export/<table_name>")
def export_table(table_name):
    denied = require_export_token()
    if denied:
        return denied

    ALLOWED = {
        "participants",
        "condition_assign",
        "baseline",
        "material_choice",
        "planning_input",
        "survey_t1",
        "survey_t2",
        "chat_log",
    }
    if table_name not in ALLOWED:
        return "Table not allowed", 403

    def generate_csv():
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table_name}")
        cols = [d[0] for d in cur.description]

        output = io.StringIO()
        writer = csv.writer(output)

        # Excel 友好：BOM
        yield "\ufeff"
        writer.writerow(cols)
        yield output.getvalue()
        output.seek(0); output.truncate(0)

        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            for r in rows:
                writer.writerow(list(r))
                yield output.getvalue()
                output.seek(0); output.truncate(0)

        conn.close()

    return Response(
        generate_csv(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={table_name}.csv"},
    )


# -------------------------
# Export: all tables as ZIP
#   /_export_all?token=xxx
# -------------------------
@app.route("/_export_all")
def export_all_tables_zip():
    denied = require_export_token()
    if denied:
        return denied

    tables = [
        "participants",
        "condition_assign",
        "baseline",
        "material_choice",
        "planning_input",
        "chat_log",
        "survey_t1",
        "survey_t2",
    ]

    def table_to_csv_bytes(conn, table_name: str) -> bytes:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table_name}")
        cols = [d[0] for d in cur.description]

        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(cols)

        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            for r in rows:
                w.writerow(list(r))

        # Excel 友好：utf-8-sig
        return s.getvalue().encode("utf-8-sig")

    conn = db_conn()
    mem_zip = io.BytesIO()
    try:
        with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for t in tables:
                try:
                    zf.writestr(f"{t}.csv", table_to_csv_bytes(conn, t))
                except Exception as e:
                    zf.writestr(f"{t}__ERROR.txt", f"{type(e).__name__}: {str(e)}\n".encode("utf-8"))
    finally:
        conn.close()

    mem_zip.seek(0)
    return Response(
        mem_zip.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=experiment_export.zip"},
    )


if __name__ == "__main__":
    # Railway 不走这里，但本地跑会走
    init_db()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )







