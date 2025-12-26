import sqlite3
import uuid
import random
from datetime import datetime
from datetime import datetime, timedelta


from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    session
)

import os



BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

print("=== TEMPLATE FOLDER ===")
print(app.template_folder)

app.secret_key = "dev"

DB_PATH = os.path.join(BASE_DIR, "experiment.db")
MAX_TURNS = 20
T1_THRESHOLD = 10





# -------------------------
# DB helpers
# -------------------------
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # ✅ 开启外键约束（如果你表里定义了 FOREIGN KEY）
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
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_baseline_pid
    ON baseline(participant_id);
    """)

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
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_material_choice_direction
    ON material_choice(chosen_direction);
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_material_choice_pid
    ON material_choice(participant_id);
    """)

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
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_condition_assign_pid
    ON condition_assign(participant_id);
    """)

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

    # 6) chat_log (统一 ts)
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
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_chat_pid_turn
    ON chat_log(participant_id, turn_id);
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_chat_pid_role
    ON chat_log(participant_id, role);
    """)

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


# -------------------------
# pid helper：URL 优先，其次 session
# -------------------------
def get_pid_from_request():
    pid = (request.args.get("pid") or "").strip()
    if pid:
        return pid
    pid = (session.get("participant_id") or "").strip()
    return pid


# -------------------------
# Condition assignment
# -------------------------
def get_or_assign_condition(participant_id: str):
    """
    配额随机（Quota randomization）
    - 4 个 cell（pre/none × focused/generic）尽量均衡
    - 每个 participant_id 只分配一次（写入 condition_assign 后固定）
    """
    conn = db_conn()
    cur = conn.cursor()

    try:
        # 让并发写更安全（有多位被试同时进来时不容易撞）
        cur.execute("BEGIN IMMEDIATE;")

        # ✅ 确保 participants 里存在这个 pid（避免外键问题）
        cur.execute("""
          INSERT OR IGNORE INTO participants(participant_id, created_at)
          VALUES (?, datetime('now'))
        """, (participant_id,))

        # 1) 如果已经分配过，直接返回
        row = cur.execute("""
          SELECT condition_planning, condition_feedback
          FROM condition_assign
          WHERE participant_id=?
        """, (participant_id,)).fetchone()

        if row:
            conn.commit()
            conn.close()
            return row["condition_planning"], row["condition_feedback"]

        # 2) 统计当前四个 cell 的人数
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

        # 3) 找到当前人数最少的 cell（可能不止一个），在最少的 cell 中随机挑一个
        min_count = min(counts.values())
        candidate_cells = [cell for cell, c in counts.items() if c == min_count]
        planning, feedback = random.choice(candidate_cells)

        # 4) 写入分配（只写一次）
        cur.execute("""
          INSERT INTO condition_assign(participant_id, condition_planning, condition_feedback, assigned_at)
          VALUES (?, ?, ?, datetime('now'))
        """, (participant_id, planning, feedback))

        conn.commit()
        conn.close()
        return planning, feedback

    except Exception:
        # 出错回滚，避免数据库锁死
        conn.rollback()
        conn.close()
        raise


def generate_assistant_reply(planning_cond: str, feedback_cond: str, user_text: str, turn_id=None) -> str:
    """
    规则型对话状态机（1–20轮）
    - 1–10：结构化构思
    - 11–20：反思与深化
    - turn_id: 由后端根据 user 发言次数计算
    - feedback_cond: "focused" / "generic"
    """
    t = int(turn_id or 1)
    u = (user_text or "").strip()

    # =========================
    # 1) 1–10 结构化构思
    # =========================
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
        10: "（聚焦反馈）总结一下：\n- 载体：{carrier}\n- 受众/场景：{aud}\n- 核心元素：{elem}\n- 气质：{adj}\n- 概念句：{concept}\n\n如果你同意，建议下一步：①列3个参考 ②画2版构图草图。"
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
        10: "（通用反馈）我们把要点收一下：载体/受众/元素/气质/概念句。下一步建议做参考收集+草图。"
    }

    # =========================
    # 2) 11–20 反思与深化
    # =========================
    focused_11_20 = {
        11: "（反思阶段）我们退一步看整体：你觉得目前概念里**最清晰**的一点是什么？（一句话）",
        12: "（反思阶段）那**最模糊/最不确定**的一点是什么？（一句话）",
        13: "（反思阶段）如果让它更可落地：你愿意优先改“内容表达”还是“形式呈现”？二选一。",
        14: "（反思阶段）给它一个明确的“核心信息”（10–15字）：你希望观众看完记住什么？",
        15: "（反思阶段）做一次风险检查：最可能被误解的地方是什么？你想怎么避免？",
        16: "（反思阶段）给 3 个关键词作为设计约束（例如：材质/色彩/符号风格）。你给哪 3 个？",
        17: "（反思阶段）请列 2 个你想参考的方向（品牌/作品类型/风格流派都行），为什么？",
        18: "（反思阶段）如果把它做成 A/B 两个版本：A更传统，B更当代。你更想保留哪一点不变？",
        19: "（反思阶段）自评一下：现在你对这个方案的“清晰度”从 1–7 你给几分？为什么？",
        20: "（反思阶段）最后收束：\n①你下一步最可执行的一件事是什么？\n②你希望我继续帮你做“润色概念句”还是“拆成制作清单”？"
    }

    generic_11_20 = {
        11: "（反思）你觉得现在最清楚的一点是什么？（一句话）",
        12: "（反思）你觉得最不确定的一点是什么？（一句话）",
        13: "（反思）想继续完善的话，你更想改内容还是改形式？",
        14: "（反思）用 10–15 字写一句核心信息：你希望观众记住什么？",
        15: "（反思）你担心它会被怎么误解？",
        16: "（反思）给 3 个关键词当作约束（材质/色彩/符号风格）。",
        17: "（反思）列 2 个你想参考的方向，并说原因。",
        18: "（反思）如果做 A/B 两版（传统/当代），你更想保留什么不变？",
        19: "（反思）你对现在方案清晰度 1–7 给几分？为什么？",
        20: "（反思）最后：你下一步最可执行的一件事是什么？"
    }

    # =========================
    # 3) session 记忆（记录用户输入，便于第10轮填空）
    # =========================
    try:
        mem = session.setdefault("chat_mem", {})

        # 1–10：结构化信息
        if t == 3: mem["carrier"] = u
        if t == 4: mem["aud"] = u
        if t == 5: mem["elem"] = u
        if t == 6: mem["adj"] = u
        if t == 8: mem["concept"] = u

        # 11–20：反思信息
        if t == 11: mem["best_clear"] = u
        if t == 12: mem["most_unclear"] = u
        if t == 14: mem["core_msg"] = u
        if t == 16: mem["constraints"] = u
        if t == 19: mem["clarity_selfrate"] = u

        session["chat_mem"] = mem
    except Exception:
        mem = {}

    # =========================
    # 4) 选择脚本并返回（关键：真正把 11–20 接入）
    # =========================
    if feedback_cond == "focused":
        if t <= 10:
            # 第10轮做填空
            if t == 10:
                return focused_1_10[10].format(
                    carrier=mem.get("carrier", "（未记录）"),
                    aud=mem.get("aud", "（未记录）"),
                    elem=mem.get("elem", "（未记录）"),
                    adj=mem.get("adj", "（未记录）"),
                    concept=mem.get("concept", "（未记录）"),
                )
            return focused_1_10.get(t, "（聚焦反馈）继续说说你的想法，我来帮你推进。")

        # 11–20：反思脚本
        return focused_11_20.get(t, "（反思阶段）你愿意补充一句：你现在最想把哪一点变得更清楚？")

    else:
        if t <= 10:
            return generic_1_10.get(t, "（通用反馈）继续说说你的想法，我来帮你推进。")
        return generic_11_20.get(t, "（反思）你现在最想补充说明哪一点？")


# -------------------------
# Minimal assistant placeholder
# -------------------------


T2_DELAY_DAYS = 7

def get_t2_eligibility(pid: str):
    """返回 (ok, eligible_at_iso, reason)"""
    conn = db_conn()
    row = conn.execute(
        "SELECT created_at FROM survey_t1 WHERE participant_id=?",
        (pid,)
    ).fetchone()
    conn.close()

    if not row:
        return (False, None, "t1_not_submitted")

    t1_at_str = row["created_at"]
    try:
        t1_at = datetime.fromisoformat(t1_at_str)
    except Exception:
        # 兜底：如果格式异常，就直接不放行，避免误填
        return (False, None, "t1_time_parse_error")

    eligible_at = t1_at + timedelta(days=T2_DELAY_DAYS)
    now = datetime.utcnow()

    if now < eligible_at:
        return (False, eligible_at.isoformat(), "too_early")

    return (True, eligible_at.isoformat(), "ok")


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

    # 进入 baseline（如果你不需要 baseline，可以改成 material_page）
    return redirect(url_for("baseline_page", pid=participant_id))


@app.route("/baseline", methods=["GET", "POST"])
def baseline_page():
    """
    兼容两种提交方式：
    1) GET: 展示 baseline.html
    2) POST: 表单提交（action="/baseline?pid=xxx"）
    如果你的前端是 fetch JSON 提交，请用下面的 /api/baseline
    """
    pid = get_pid_from_request()
    if not pid:
        return "Missing pid", 400

    if request.method == "GET":
        return render_template("baseline.html", participant_id=pid)

    # POST: 表单提交
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


# ✅ 关键新增：兼容你 baseline.html 里 fetch("/api/baseline") 的提交
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

    # 前端如果拿 next 跳转就用 next
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
        data = request.get_json(force=True)  # 强制按 JSON 解析
        pid = (data.get("participant_id") or "").strip()
        user_text = (data.get("text") or "").strip()

        if not pid or not user_text:
            return jsonify({"ok": False, "error": "missing participant_id or text"}), 400

        conn = db_conn()
        cur = conn.cursor()

        # ✅ 确保 pid 存在（防外键炸）
        cur.execute("""
            INSERT OR IGNORE INTO participants(participant_id, created_at)
            VALUES (?, datetime('now'))
        """, (pid,))
        conn.commit()

        # ✅ 条件分配
        planning_cond, feedback_cond = get_or_assign_condition(pid)

        # ✅ next turn_id = 已有 user 条数 + 1
        current_user_turns = cur.execute("""
            SELECT COUNT(*) AS c FROM chat_log
            WHERE participant_id=? AND role='user'
        """, (pid,)).fetchone()["c"]
        next_turn_id = int(current_user_turns) + 1

        # ✅ 最多 20 轮
        if next_turn_id > MAX_TURNS:
            conn.close()
            return jsonify({"ok": False, "error": "max_turns_reached"}), 400

        # ✅ 同一轮统一时间戳
        now = datetime.utcnow().isoformat()

        # ✅ 写入 user
        cur.execute("""
            INSERT INTO chat_log(participant_id, turn_id, role, text, ts)
            VALUES (?, ?, 'user', ?, ?)
        """, (pid, next_turn_id, user_text, now))

        # ✅ 生成 assistant（10轮脚本 + 10轮后反思）
        assistant_text = generate_assistant_reply(
            planning_cond=planning_cond,
            feedback_cond=feedback_cond,
            user_text=user_text,
            turn_id=next_turn_id
        )

        # ✅ 写入 assistant（同一轮同一 now）
        cur.execute("""
            INSERT INTO chat_log(participant_id, turn_id, role, text, ts)
            VALUES (?, ?, 'assistant', ?, ?)
        """, (pid, next_turn_id, assistant_text, now))

        conn.commit()
        conn.close()

        can_finish = (next_turn_id >= T1_THRESHOLD)  # 门槛仍 10

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
        # ✅ 关键：即使 500 也返回 JSON，前端不会再解析成 HTML
        return jsonify({
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)}"
        }), 500



        # ✅ 写入 user
        cur.execute("""
            INSERT INTO chat_log(participant_id, turn_id, role, text, ts)
            VALUES (?, ?, 'user', ?, ?)
        """, (pid, next_turn_id, user_text, now))

        # ✅ 生成 assistant（最容易 500 的点：参数签名不匹配）
        assistant_text = generate_assistant_reply(
            planning_cond, feedback_cond, user_text, turn_id=next_turn_id
        )

        # ✅ 写入 assistant
        cur.execute("""
            INSERT INTO chat_log(participant_id, turn_id, role, text, ts)
            VALUES (?, ?, 'assistant', ?, ?)
        """, (pid, next_turn_id, assistant_text, datetime.utcnow().isoformat()))

        conn.commit()
        conn.close()

        can_finish = (next_turn_id >= 10)
        return jsonify({
            "ok": True,
            "turn_id": next_turn_id,
            "assistant": assistant_text,
            "can_finish": can_finish
        })

    except Exception as e:
        # ✅ 关键：即使 500 也返回 JSON，这样前端不会再 “Unexpected token <”
        import traceback
        traceback.print_exc()
        return jsonify({
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)}"
        }), 500


@app.route("/t1", methods=["GET", "POST"])
def t1_page():
    pid = request.args.get("pid", "").strip()
    if not pid:
        return "Missing pid", 400

    # ---------- GET：显示 T1 问卷 ----------
    if request.method == "GET":
        return render_template("survey_t1.html", participant_id=pid)

    # ---------- POST：保存 T1 并进入完成页 ----------
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
        manip_feedback=excluded.manip_feedback
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

    # ✅ 无论 GET/POST，都先拦截（防止用户直接 POST 绕过）
    if not ok:
        # 如果 T1 还没做完，直接引导回去
        if reason == "t1_not_submitted":
            return redirect(url_for("t1_page", pid=pid))

        # 还没到 7 天：显示锁定页
        return render_template("t2_locked.html", participant_id=pid, eligible_at=eligible_at_iso)

    # 到时间了：正常显示/提交
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
        cont_intent_1=excluded.cont_intent_1
    """, payload)
    conn.commit()
    conn.close()

    return render_template("done_t2.html", participant_id=pid)


    now = datetime.now(timezone.utc)
    elapsed_seconds = (now - t1_time).total_seconds()
    if elapsed_seconds < delay_hours * 3600:
        remaining = int((delay_hours * 3600 - elapsed_seconds) // 3600) + 1
        conn.close()
        # 你也可以改成 render_template 一个更友好的 locked 页面
        return f"T2 is locked: please return after 7 days. (Remaining ~{remaining} hours)", 403

    conn.close()

    # ---------- 正常 T2 流程 ----------
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
        cont_intent_1=excluded.cont_intent_1
    """, payload)
    conn.commit()
    conn.close()

    return render_template("done_t2.html", participant_id=pid)



if __name__ == "__main__":
    init_db()
    app.run(
        host="0.0.0.0",
        port=5001,
        debug=False   # ✅ 正式投放
    )



