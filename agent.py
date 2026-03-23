#!/usr/bin/env python3
"""
Helion AI — Personal Execution Engine (v4 with Artemis AI)
==============================================================
A multi-agent AI system that acts as your personal chief of staff.
Breaks down goals, generates daily action plans, tracks progress,
and now includes Artemis — a personalized AI assistant.

New in v4:
  ArtemisAgent         -> Personalized AI chatbot for goals & plans
  /api/goals/add       -> Add goals via UI
  /api/goals/remove    -> Remove goals via UI
  /api/plan/generate   -> Generate AI action plan from goals
  /api/chat            -> Artemis multi-turn chat
"""

import json
import os
import sys
import argparse
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
import http.server
import socketserver
import http.cookies
import secrets
import threading
import urllib.parse


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DATA_DIR  = Path(os.environ.get("DATA_PATH", str(BASE_DIR / "data")))

GOALS_FILE     = BASE_DIR / "goals.json"
MEMORY_FILE    = BASE_DIR / "memory.json"
HABITS_FILE    = BASE_DIR / "habits.json"
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
API_KEY_FILE   = BASE_DIR / ".api_key"

USERS_FILE    = DATA_DIR / "users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"

def user_goals_file(u):  return DATA_DIR / f"goals_{u}.json"
def user_memory_file(u): return DATA_DIR / f"memory_{u}.json"
def user_habits_file(u): return DATA_DIR / f"habits_{u}.json"
def user_plan_file(u):   return DATA_DIR / f"plan_{u}.json"
def user_coach_file(u):  return DATA_DIR / f"coach_{u}.json"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-opus-4-6"


def get_api_key() -> str:
    """Resolve Anthropic API key: file first, then environment variable."""
    if API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text().strip()
        if key:
            return key
    return os.environ.get("ANTHROPIC_API_KEY", "")


# ─────────────────────────────────────────────
# ANTHROPIC CLIENT
# ─────────────────────────────────────────────

class AnthropicClient:
    """Minimal Anthropic API client using Python stdlib only."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _request(self, payload_dict: dict) -> str:
        payload = json.dumps(payload_dict).encode("utf-8")
        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["content"][0]["text"]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            raise RuntimeError(f"API error {e.code}: {body}")

    def message(self, system: str, user: str, max_tokens: int = 1500) -> str:
        return self._request({
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })

    def chat(self, system: str, messages: list, max_tokens: int = 600) -> str:
        """Multi-turn chat for Artemis."""
        return self._request({
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        })


# ─────────────────────────────────────────────
# GOAL MANAGER
# ─────────────────────────────────────────────

class GoalManager:
    def __init__(self, goals_file=None):
        self._file = goals_file or GOALS_FILE
        self.data = self._load()

    def _load(self) -> dict:
        if self._file.exists():
            with open(self._file) as f:
                return json.load(f)
        return {"goals": [], "context": {}}

    def save(self):
        with open(self._file, "w") as f:
            json.dump(self.data, f, indent=2)

    def get_goals_summary(self) -> str:
        goals = self.data.get("goals", [])
        ctx = self.data.get("context", {})
        lines = []
        if ctx:
            lines.append(f"User context: {json.dumps(ctx)}")
        for g in goals:
            status = g.get("status", "active")
            deadline = g.get("deadline", "no deadline")
            lines.append(f"- [{status.upper()}] {g['title']} | Deadline: {deadline}")
            for sg in g.get("sub_goals", []):
                lines.append(f"    - Sub-goal: {sg['title']} | Priority: {sg.get('priority','medium')}")
        return "\n".join(lines) if lines else "No goals configured yet."

    def add_goal(self, title: str, deadline: str = None, sub_goals: list = None):
        self.data["goals"].append({
            "id": hashlib.md5((title + datetime.now().isoformat()).encode()).hexdigest()[:8],
            "title": title,
            "deadline": deadline or "open-ended",
            "status": "active",
            "created": datetime.now().isoformat(),
            "sub_goals": [{"title": sg, "priority": "medium"} for sg in (sub_goals or [])],
        })
        self.save()

    def remove_goal(self, goal_id: str):
        self.data["goals"] = [g for g in self.data["goals"] if g.get("id") != goal_id]
        self.save()

    def mark_complete(self, goal_id: str):
        for g in self.data["goals"]:
            if g.get("id") == goal_id:
                g["status"] = "complete"
        self.save()


# ─────────────────────────────────────────────
# MEMORY SYSTEM
# ─────────────────────────────────────────────

class MemorySystem:
    def __init__(self, memory_file=None):
        self._file = memory_file or MEMORY_FILE
        self.data = self._load()

    def _load(self) -> dict:
        if self._file.exists():
            with open(self._file) as f:
                return json.load(f)
        return {
            "sessions": [],
            "habit_log": {},
            "streak": 0,
            "total_tasks_completed": 0,
            "last_run": None,
        }

    def save(self):
        with open(self._file, "w") as f:
            json.dump(self.data, f, indent=2)

    def log_session(self, session: dict):
        self.data["sessions"].append(session)
        self.data["sessions"] = self.data["sessions"][-30:]
        self.data["last_run"] = datetime.now().isoformat()
        if session.get("tasks_completed"):
            self.data["total_tasks_completed"] += len(session["tasks_completed"])
        self.save()

    def get_recent_sessions(self, n: int = 3) -> list:
        return self.data["sessions"][-n:]

    def get_completion_rate(self) -> float:
        recent = self.get_recent_sessions(7)
        if not recent:
            return 0.0
        rates = []
        for s in recent:
            total = len(s.get("daily_plan", []))
            done = len(s.get("tasks_completed", []))
            if total > 0:
                rates.append(done / total)
        return round(sum(rates) / len(rates), 2) if rates else 0.0

    def update_streak(self):
        last = self.data.get("last_run")
        if last:
            last_dt = datetime.fromisoformat(last)
            if (datetime.now() - last_dt).days <= 1:
                self.data["streak"] += 1
            else:
                self.data["streak"] = 1
        else:
            self.data["streak"] = 1
        self.save()


# ─────────────────────────────────────────────
# HABIT TRACKER
# ─────────────────────────────────────────────

class HabitTracker:
    def __init__(self, memory, habits_file=None):
        self._file = habits_file or HABITS_FILE
        self.memory = memory
        self.data = self._load()

    def _load(self) -> dict:
        if self._file.exists():
            with open(self._file) as f:
                return json.load(f)
        return {"habits": []}

    def save(self):
        with open(self._file, "w") as f:
            json.dump(self.data, f, indent=2)

    def add_habit(self, title: str, frequency: str = "daily"):
        self.data["habits"].append({
            "id": hashlib.md5(title.encode()).hexdigest()[:8],
            "title": title,
            "frequency": frequency,
            "created": datetime.now().isoformat(),
        })
        self.save()

    def get_habit_rate(self, habit_id: str, days: int = 7) -> float:
        habit_log = self.memory.data.get("habit_log", {})
        if habit_id not in habit_log:
            return 0.0
        completions = habit_log[habit_id]
        if not completions:
            return 0.0
        recent = [c for c in completions if (datetime.now() - datetime.fromisoformat(c)).days < days]
        return len(recent) / days if days > 0 else 0.0

    def log_completion(self, habit_id: str):
        if "habit_log" not in self.memory.data:
            self.memory.data["habit_log"] = {}
        if habit_id not in self.memory.data["habit_log"]:
            self.memory.data["habit_log"][habit_id] = []
        self.memory.data["habit_log"][habit_id].append(datetime.now().isoformat())
        self.memory.save()


# ─────────────────────────────────────────────
# PLANNER AGENT
# ─────────────────────────────────────────────

class PlannerAgent:
    SYSTEM = """You are a world-class executive coach and productivity strategist.
Your job: given someone's goals, context, and recent progress, generate a focused
daily action plan. Be specific, actionable, and realistic (3-6 tasks max).

Rules:
- Each task must be completable in under 2 hours
- Be specific (not "work on project" but "write 300-word intro for Section 2")
- Prioritize by impact and urgency
- Include one 'momentum task' that is easy to start to beat procrastination
- Output ONLY valid JSON, no commentary

Output format:
{
  "date": "YYYY-MM-DD",
  "focus_theme": "One-sentence theme for the day",
  "tasks": [
    {
      "id": "t1",
      "title": "Specific task title",
      "goal_link": "Which goal this serves",
      "estimated_minutes": 45,
      "priority": "high|medium|low",
      "is_momentum_task": true,
      "why_today": "One sentence on why this matters today"
    }
  ],
  "daily_intention": "One motivating sentence for the day"
}"""

    def __init__(self, client: AnthropicClient):
        self.client = client

    def generate_plan(self, goals_summary: str, recent_sessions: list, today: str) -> dict:
        recent_str = json.dumps(recent_sessions[-2:], indent=2) if recent_sessions else "No prior sessions."
        user_msg = f"""Today's date: {today}

GOALS:
{goals_summary}

RECENT HISTORY (last 2 sessions):
{recent_str}

Generate today's focused action plan."""
        raw = self.client.message(self.SYSTEM, user_msg, max_tokens=1000)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{[\s\S]+\}', raw)
            if match:
                return json.loads(match.group())
            raise ValueError(f"Planner returned non-JSON: {raw[:200]}")


# ─────────────────────────────────────────────
# COACH AGENT
# ─────────────────────────────────────────────

class CoachAgent:
    SYSTEM = """You are a direct, data-driven personal coach.
No fluff, no empty praise. Analyze the user's completion data and goals, then deliver
a coaching message that:
1. Opens with a direct assessment (2-3 sentences) of what's working and what's not
2. Identifies the primary pattern holding them back (be specific and honest)
3. Offers one insight they may not have considered
4. Closes with an energizing, specific challenge for the next 24 hours

Tone: Direct, warm, and intelligent. Like a mentor who wants you to win.
Length: 200-250 words max. Write in natural paragraphs, no bullet points."""

    def __init__(self, client: AnthropicClient):
        self.client = client

    def generate_coaching(self, goals_summary: str, recent_sessions: list,
                          completion_rate: float) -> str:
        user_msg = f"""GOALS:
{goals_summary}

COMPLETION RATE (7-day avg): {int(completion_rate * 100)}%

RECENT SESSIONS:
{json.dumps(recent_sessions, indent=2)}

Deliver my coaching message."""
        return self.client.message(self.SYSTEM, user_msg, max_tokens=400)


# ─────────────────────────────────────────────
# REPLANNER AGENT
# ─────────────────────────────────────────────

class ReplannerAgent:
    SYSTEM = """You are a tactical replanning agent. When someone is falling behind
on their goals, you restructure their plan to get them back on track.

Analyze the situation and output a replanning decision as JSON:

{
  "status": "on_track|slightly_behind|significantly_behind|critical",
  "assessment": "2 sentences on what's happening",
  "dropped_tasks": ["task titles to defer or drop entirely"],
  "escalated_tasks": ["task titles that must happen in the next 48h"],
  "adjusted_plan_note": "One paragraph on how to approach the next 3 days",
  "hard_truth": "One sentence of honest accountability (optional)"
}

Output ONLY valid JSON."""

    def __init__(self, client: AnthropicClient):
        self.client = client

    def analyze_and_replan(self, goals_summary: str, recent_sessions: list,
                           completion_rate: float) -> dict:
        user_msg = f"""GOALS:
{goals_summary}

7-DAY COMPLETION RATE: {int(completion_rate * 100)}%

RECENT SESSIONS:
{json.dumps(recent_sessions, indent=2)}

Assess status and replan."""
        raw = self.client.message(self.SYSTEM, user_msg, max_tokens=600)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{[\s\S]+\}', raw)
            if match:
                return json.loads(match.group())
            return {"status": "unknown", "assessment": raw[:200], "hard_truth": ""}


# ─────────────────────────────────────────────
# ARTEMIS AGENT (v4 new)
# ─────────────────────────────────────────────

class ArtemisAgent:
    """Artemis — the personalized Helion AI assistant."""

    BASE_SYSTEM = """You are Artemis, an intelligent and personalized AI assistant built into Helion AI — a personal productivity and goal execution platform.

Your role is to help users:
- Understand and make progress on their active goals
- Break through blockers in their action plans
- Think clearly about priorities and next steps
- Reflect on patterns in their productivity data

Personality: Precise, warm, and direct. You give actionable guidance, not vague motivation. You speak like a trusted advisor — concise, honest, and genuinely invested in the user's success. You never use filler phrases like "Great question!" or "Certainly!". You get straight to what matters.

When you have context about the user's goals and action plans, reference them specifically. If asked something outside productivity and goals, gently redirect while still being helpful.

Keep responses under 180 words unless the user specifically asks for more detail. Write in clear, flowing sentences — no markdown headers or excessive bullet points."""

    def __init__(self, client: AnthropicClient):
        self.client = client

    def chat(self, messages: list, goals_summary: str = "", plan_summary: str = "",
             display_name: str = "there") -> str:
        system = self.BASE_SYSTEM
        system += f"\n\nThe user's name is {display_name}."
        if goals_summary and goals_summary != "No goals configured yet.":
            system += f"\n\nUser's active goals:\n{goals_summary}"
        if plan_summary:
            system += f"\n\nUser's current action plan:\n{plan_summary}"
        if not goals_summary or goals_summary == "No goals configured yet.":
            system += "\n\nThe user has not set up any goals yet. Encourage them to add goals using the Goals panel so you can give more personalized guidance."
        try:
            return self.client.chat(system, messages, max_tokens=600)
        except Exception as e:
            return f"I'm having trouble connecting right now. Please try again in a moment."


# ─────────────────────────────────────────────
# USER MANAGER
# ─────────────────────────────────────────────

class UserManager:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        self.users = self._load()

    def _load(self):
        if USERS_FILE.exists():
            with open(USERS_FILE) as f:
                return json.load(f)
        return {}

    def _save(self):
        with open(USERS_FILE, "w") as f:
            json.dump(self.users, f, indent=2)

    def _hash(self, password, salt):
        return hashlib.sha256(f"{salt}{password}{salt}".encode()).hexdigest()

    def create_user(self, username, password, display_name="", email=""):
        username = username.lower().strip()
        if len(username) < 3:
            return False, "Username must be at least 3 characters"
        if not username.replace("_", "").replace("-", "").isalnum():
            return False, "Letters, numbers, - and _ only"
        if username in self.users:
            return False, "Username already taken"
        if len(password) < 6:
            return False, "Password must be at least 6 characters"
        salt = secrets.token_hex(16)
        self.users[username] = {
            "password_hash": self._hash(password, salt),
            "salt": salt,
            "display_name": display_name.strip() or username.title(),
            "email": email.strip(),
            "created_at": datetime.now().isoformat(),
        }
        self._save()
        for fpath, default in [
            (user_goals_file(username),  {"goals": [], "context": {}}),
            (user_memory_file(username), {"sessions": [], "habit_log": {}, "streak": 0,
                                          "total_tasks_completed": 0, "last_run": None}),
            (user_habits_file(username), {"habits": []}),
        ]:
            if not fpath.exists():
                fpath.write_text(json.dumps(default, indent=2))
        return True, "Account created"

    def verify(self, username, password):
        u = self.users.get(username.lower().strip())
        if not u:
            return False
        return u["password_hash"] == self._hash(password, u["salt"])

    def get_user(self, username):
        u = self.users.get(username.lower().strip())
        if not u:
            return None
        return {"username": username, "display_name": u["display_name"],
                "created_at": u["created_at"]}


# ─────────────────────────────────────────────
# SESSION MANAGER
# ─────────────────────────────────────────────

class SessionManager:
    TTL = 86400

    def __init__(self):
        self._s = {}
        self._load()

    def _load(self):
        if SESSIONS_FILE.exists():
            try:
                raw = json.loads(SESSIONS_FILE.read_text())
                now = datetime.now().timestamp()
                self._s = {k: v for k, v in raw.items() if v.get("exp", 0) > now}
            except:
                self._s = {}

    def _save(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            SESSIONS_FILE.write_text(json.dumps(self._s))
        except:
            pass

    def create(self, username):
        token = secrets.token_urlsafe(32)
        self._s[token] = {
            "user": username,
            "exp": (datetime.now() + timedelta(seconds=self.TTL)).timestamp()
        }
        self._save()
        return token

    def get_user(self, token):
        if not token:
            return None
        sess = self._s.get(token)
        if not sess or sess.get("exp", 0) < datetime.now().timestamp():
            self._s.pop(token, None)
            return None
        return sess["user"]

    def delete(self, token):
        self._s.pop(token, None)
        self._save()


# ─────────────────────────────────────────────
# LOGIN PAGE GENERATOR
# ─────────────────────────────────────────────

class LoginPageGenerator:
    def generate(self):
        return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Helion AI — Sign In</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
    :root{--bg:#eaf3ff;--surface:rgba(255,255,255,0.88);--border:rgba(90,160,230,.12);
          --border-m:rgba(90,160,230,.25);--text:#1a2e4a;--muted:#333333;
          --accent:#5aabdf;--serif:'Playfair Display',Georgia,serif;--sans:'Inter',system-ui,sans-serif;}
    body{font-family:var(--sans);background:var(--bg);color:var(--text);
         min-height:100vh;display:flex;align-items:center;justify-content:center;}
    .atm{position:fixed;inset:0;pointer-events:none;}
    .orb{position:absolute;border-radius:50%;filter:blur(90px);opacity:.38;}
    .o1{width:700px;height:500px;top:-150px;left:-100px;background:radial-gradient(ellipse,#90c8f0,transparent 70%);}
    .o2{width:600px;height:400px;bottom:-100px;right:-100px;background:radial-gradient(ellipse,#b0d9ff,transparent 70%);}
    .card{position:relative;z-index:1;width:100%;max-width:420px;background:var(--surface);
          border:1px solid var(--border);border-radius:4px;padding:44px 40px 40px;backdrop-filter:blur(24px);}
    .logo{display:flex;align-items:center;gap:10px;margin-bottom:36px;}
    .logo-text{font-size:13px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:#1a2e4a;}
    .logo-text strong{color:var(--text);}
    .card-title{font-family:var(--serif);font-size:28px;font-weight:700;color:#1a2e4a;margin-bottom:8px;}
    .card-sub{font-size:13px;color:var(--muted);font-weight:300;margin-bottom:32px;line-height:1.5;}
    .tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:28px;}
    .tab{flex:1;text-align:center;padding:10px 0;font-size:13px;font-weight:500;color:var(--muted);
         cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;letter-spacing:.04em;}
    .tab.active{color:var(--text);border-bottom-color:var(--accent);}
    .field{margin-bottom:18px;}
    .field label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.1em;
                  color:var(--muted);margin-bottom:8px;font-weight:500;}
    .field input{width:100%;background:rgba(255,255,255,.04);border:1px solid var(--border);
                  border-radius:2px;padding:11px 14px;font-size:14px;color:var(--text);
                  font-family:var(--sans);outline:none;transition:border-color .2s;}
    .field input:focus{border-color:var(--border-m);}
    .field input::placeholder{color:#111111;}
    .btn{width:100%;padding:13px;background:rgba(90,171,223,.15);border:1px solid rgba(90,171,223,.3);
         border-radius:2px;color:var(--text);font-size:13px;font-weight:600;letter-spacing:.06em;
         text-transform:uppercase;cursor:pointer;font-family:var(--sans);transition:all .2s;margin-top:8px;}
    .btn:hover{background:rgba(90,171,223,.25);border-color:rgba(90,171,223,.5);}
    .btn:disabled{opacity:.5;cursor:not-allowed;}
    .err{font-size:12px;color:#c97b7b;margin-top:14px;text-align:center;min-height:18px;}
    .signup-only{display:none;}
  </style>
</head>
<body>
<div class="atm"><div class="orb o1"></div><div class="orb o2"></div></div>
<div class="card">
  <div class="logo">
    <svg width="50" height="32" viewBox="0 0 100 62" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="35" cy="11" r="5.5" stroke="#1a2e4a" stroke-width="1.9"/>
      <path d="M1,58 C8,44 20,31 35,24 C43,28 51,34 57,38 C61,32 65,27 69,27 C77,33 88,40 99,50"
            stroke="#1a2e4a" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <span class="logo-text">Helion <strong>AI</strong></span>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="sw('login')">Sign In</div>
    <div class="tab" onclick="sw('register')">Create Account</div>
  </div>
  <div id="tl" class="card-title">Welcome back.</div>
  <div id="tr" class="card-title" style="display:none">Get started.</div>
  <div id="sl" class="card-sub">Your goals and plans are waiting.</div>
  <div id="sr" class="card-sub" style="display:none">Create your personal Helion AI account.</div>
  <form onsubmit="go(event)">
    <div class="field signup-only" id="fn">
      <label>Your Name</label>
      <input type="text" id="display_name" placeholder="e.g. Akshat" autocomplete="name"/>
    </div>
    <div class="field signup-only" id="ef">
      <label>Email</label>
      <input type="email" id="email" placeholder="e.g. akshat@example.com" autocomplete="email"/>
    </div>
    <div class="field">
      <label>Username</label>
      <input type="text" id="username" placeholder="e.g. akshat" required autocomplete="username" autocapitalize="none"/>
    </div>
    <div class="field">
      <label>Password</label>
      <input type="password" id="password" placeholder="Min. 6 characters" required/>
    </div>
    <button class="btn" type="submit" id="sb">Sign In</button>
  </form>
  <div class="err" id="em"></div>
    <div class="google-divider" id="gd">or</div>
    <a class="btn-google" id="gb" href="/auth/google">
      <svg viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.33 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.67 14.62 48 24 48z"/></svg>
      Continue with Google
    </a>
</div>

<canvas id="snowMtn" style="position:fixed;bottom:0;left:0;width:100%;height:280px;pointer-events:none;z-index:0;"></canvas>
<script>
(function(){
var cvs=document.getElementById('snowMtn');
var dpr=window.devicePixelRatio||1;
var ctx=cvs.getContext('2d');
var W,H,flakes=[],sunT=0;
function resize(){
  W=cvs.width=Math.round(window.innerWidth*dpr);
  H=cvs.height=Math.round(280*dpr);
  cvs.style.width=window.innerWidth+'px';
  cvs.style.height='280px';
  initFlakes();
}
function initFlakes(){
  flakes=[];
  var n=Math.min(900,Math.floor(W/2.2));
  for(var i=0;i<n;i++){
    flakes.push({x:Math.random()*W,y:Math.random()*H,r:(0.4+Math.random()*2.2)*dpr,
      speed:(0.4+Math.random()*1.1)*dpr,drift:(Math.random()-0.5)*0.5*dpr,op:0.35+Math.random()*0.65});
  }
}
var l0=[[0,0],[0.05,28],[0.12,55],[0.18,38],[0.25,70],[0.30,52],[0.38,65],[0.45,42],[0.52,78],[0.58,55],[0.65,68],[0.72,44],[0.80,72],[0.88,50],[0.95,60],[1,0]];
var l1=[[0,0],[0.04,35],[0.09,62],[0.15,48],[0.20,80],[0.27,60],[0.32,200],[0.37,68],[0.43,85],[0.50,55],[0.56,90],[0.63,62],[0.70,95],[0.77,58],[0.84,80],[0.91,65],[0.97,42],[1,0]];
var l2=[[0,0],[0.06,42],[0.11,72],[0.17,55],[0.22,95],[0.28,75],[0.32,210],[0.36,78],[0.41,110],[0.48,68],[0.54,100],[0.60,72],[0.67,108],[0.74,70],[0.81,90],[0.88,68],[0.94,50],[1,0]];
function mtnPath(pts){
  ctx.beginPath();ctx.moveTo(0,H);
  for(var i=0;i<pts.length;i++) ctx.lineTo(pts[i][0]*W,H-pts[i][1]*dpr);
  ctx.lineTo(W,H);ctx.closePath();
}
function drawMtn(){
  mtnPath(l0);ctx.fillStyle='rgba(6,14,40,0.93)';ctx.fill();
  mtnPath(l1);ctx.fillStyle='rgba(9,20,55,0.96)';ctx.fill();
  mtnPath(l2);ctx.fillStyle='rgba(11,26,65,1.0)';ctx.fill();
}
function drawSun(){
  var cx=0.32*W, cy=H-210*dpr-12*dpr, cr=9*dpr;
  sunT+=0.018;
  var grd=ctx.createRadialGradient(cx,cy,cr*0.5,cx,cy,cr*3.5);
  grd.addColorStop(0,'rgba(160,210,255,0.28)');grd.addColorStop(1,'rgba(160,210,255,0)');
  ctx.beginPath();ctx.arc(cx,cy,cr*3.5,0,Math.PI*2);ctx.fillStyle=grd;ctx.fill();
  for(var i=0;i<16;i++){
    var a=i*(Math.PI*2/16)+Math.sin(sunT*2.1+i*0.7)*0.12;
    var inner=cr+3*dpr, outer=cr+18*dpr+Math.sin(sunT*1.7+i*1.3)*5*dpr;
    ctx.beginPath();ctx.moveTo(cx+Math.cos(a)*inner,cy+Math.sin(a)*inner);
    ctx.lineTo(cx+Math.cos(a)*outer,cy+Math.sin(a)*outer);
    ctx.strokeStyle='rgba(180,220,255,0.55)';ctx.lineWidth=1.1*dpr;ctx.stroke();
  }
  ctx.beginPath();ctx.arc(cx,cy,cr,0,Math.PI*2);
  ctx.strokeStyle='rgba(200,230,255,0.85)';ctx.lineWidth=1.5*dpr;ctx.stroke();
  ctx.beginPath();ctx.arc(cx,cy,cr,0,Math.PI*2);
  ctx.fillStyle='rgba(140,190,255,0.18)';ctx.fill();
}
function drawSnow(){
  for(var i=0;i<flakes.length;i++){
    var f=flakes[i];
    ctx.globalAlpha=f.op;ctx.beginPath();ctx.arc(f.x,f.y,f.r,0,Math.PI*2);
    ctx.fillStyle='rgba(220,235,255,1)';ctx.fill();
    f.y+=f.speed;f.x+=f.drift;
    if(f.y>H+5){f.y=-5;f.x=Math.random()*W;}
    if(f.x>W+5) f.x=-5; if(f.x<-5) f.x=W+5;
  }
  ctx.globalAlpha=1;
}
function frame(){ctx.clearRect(0,0,W,H);drawMtn();drawSun();drawSnow();requestAnimationFrame(frame);}
resize();window.addEventListener('resize',resize);requestAnimationFrame(frame);
})();
</script>
<script>
let mode='login';
function sw(m){
  mode=m;
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',(m==='login'&&i===0)||(m==='register'&&i===1)));
  document.querySelectorAll('.signup-only').forEach(el=>el.style.display=m==='register'?'block':'none');
  document.getElementById('tl').style.display=m==='login'?'':'none';
  document.getElementById('tr').style.display=m==='register'?'':'none';
  document.getElementById('sl').style.display=m==='login'?'':'none';
  document.getElementById('sr').style.display=m==='register'?'':'none';
  document.getElementById('sb').textContent=m==='login'?'Sign In':'Create Account';
  document.getElementById('em').textContent='';
}
async function go(e){
  e.preventDefault();
  const btn=document.getElementById('sb'),err=document.getElementById('em');
  btn.disabled=true;err.textContent='';
  const body={username:document.getElementById('username').value.trim(),
               password:document.getElementById('password').value};
  if(mode==='register')body.display_name=document.getElementById('display_name').value.trim();
        if(mode==='register')body.email=document.getElementById('email').value.trim();
  try{
    const r=await fetch(`/api/auth/${mode==='login'?'login':'register'}`,
      {method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify(body)});
    const d=await r.json();
    if(d.success)window.location.href='/dashboard';
    else err.textContent=d.error||'Something went wrong.';
  }catch(ex){err.textContent='Cannot connect to server.';}
  btn.disabled=false;
}
</script>
</body></html>"""


# ─────────────────────────────────────────────
# DASHBOARD GENERATOR (v4 — clean, no emojis, Artemis chat)
# ─────────────────────────────────────────────

class DashboardGenerator:

    # ── CSS (defined as a regular string — no f-string escaping needed) ──
    _CSS = """
*{box-sizing:border-box;margin:0;padding:0;}
:root{
  --bg:#eaf3ff;--surface:rgba(255,255,255,0.88);--surface-2:rgba(225,240,255,0.75);
  --border:rgba(90,160,230,.10);--border-m:rgba(90,160,230,.22);
  --text:#1a2e4a;--muted:#333333;--accent:#5aabdf;
  --green:#10b981;--amber:#f59e0b;--red:#ef4444;
  --serif:'Playfair Display',Georgia,serif;--sans:'Inter',system-ui,sans-serif;
}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;padding:0 0 300px;}
.atm{position:fixed;inset:0;pointer-events:none;z-index:0;}
.orb{position:absolute;border-radius:50%;filter:blur(90px);opacity:.35;}
.o1{width:800px;height:600px;top:-200px;left:-100px;background:radial-gradient(ellipse,#90c8f0,transparent 70%);}
.o2{width:700px;height:500px;bottom:-150px;right:-100px;background:radial-gradient(ellipse,#b0d9ff,transparent 70%);}
.wrapper{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:0 24px;}

/* Nav */
.nav{display:flex;justify-content:space-between;align-items:center;
     padding:18px 0 16px;border-bottom:1px solid var(--border);margin-bottom:28px;}
.nav-brand{display:flex;align-items:center;gap:10px;}
.nav-brand svg{width:36px;height:24px;}
.nav-title{font-size:14px;font-weight:600;letter-spacing:-.3px;font-family:var(--serif);color:var(--text);}
.nav-title span{color:var(--accent);}
.nav-right{display:flex;gap:16px;align-items:center;}
.nav-user{font-size:13px;color:var(--muted);}
.nav-link{font-size:12px;color:var(--accent);text-decoration:none;
          padding:5px 12px;border:1px solid rgba(90,171,223,.25);border-radius:2px;transition:all .2s;}
.nav-link:hover{background:rgba(90,171,223,.1);}

/* Hero */
.hero{margin-bottom:24px;}
.hero-label{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin-bottom:6px;}
.hero-title{font-family:var(--serif);font-size:28px;font-weight:700;color:#1a2e4a;margin-bottom:6px;line-height:1.3;}
.hero-sub{font-size:13px;color:var(--muted);line-height:1.6;font-style:italic;}

/* Stats */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px;}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:20px 24px;text-align:center;}
.stat-value{font-size:36px;font-weight:800;color:#fff;line-height:1.1;margin-bottom:6px;}
.stat-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;}

/* Grid */
.grid{display:grid;grid-template-columns:2fr 1fr;gap:20px;margin-bottom:20px;}
.grid-full{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;}

/* Cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:22px;}
.card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-weight:600;}
.card-action{font-size:12px;color:var(--accent);cursor:pointer;
             background:none;border:1px solid rgba(90,171,223,.25);border-radius:2px;
             padding:4px 10px;font-family:var(--sans);transition:all .2s;}
.card-action:hover{background:rgba(90,171,223,.12);}

/* Tasks */
.task-item{background:var(--surface-2);border:1px solid var(--border);border-radius:2px;
           padding:12px 14px;margin-bottom:8px;transition:border-color .15s;}
.task-item:last-child{margin-bottom:0;}
.task-item.done{opacity:.4;}
.task-item.done .task-title{text-decoration:line-through;}
.task-row{display:flex;gap:10px;align-items:flex-start;}
.task-check{width:16px;height:16px;border:1px solid rgba(90,160,230,.3);border-radius:2px;
            cursor:pointer;flex-shrink:0;margin-top:2px;display:flex;align-items:center;
            justify-content:center;font-size:10px;color:transparent;transition:all .15s;user-select:none;}
.task-check.checked{background:var(--green);border-color:var(--green);color:#fff;}
.task-body{flex:1;}
.task-title{font-size:13px;font-weight:600;color:#e2e8f0;margin-bottom:5px;line-height:1.4;}
.task-meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:4px;}
.tag{font-size:10px;padding:2px 7px;border-radius:8px;font-weight:500;white-space:nowrap;}
.tag-high{background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.25);}
.tag-medium{background:rgba(245,158,11,.12);color:#f59e0b;border:1px solid rgba(245,158,11,.25);}
.tag-low{background:rgba(16,185,129,.12);color:#10b981;border:1px solid rgba(16,185,129,.25);}
.tag-time{background:rgba(30,42,58,.8);color:#7dd3fc;border:1px solid rgba(125,211,252,.15);}
.tag-start{background:rgba(251,191,36,.1);color:#fbbf24;border:1px solid rgba(251,191,36,.2);}
.task-goal{font-size:11px;color:#111111;}
.task-why{font-size:12px;color:var(--muted);margin-top:3px;line-height:1.5;}

/* Goals sidebar */
.goal-item{background:var(--surface-2);border-left:2px solid var(--accent);
           border-radius:0 2px 2px 0;padding:10px 12px;margin-bottom:8px;
           display:flex;justify-content:space-between;align-items:flex-start;}
.goal-item:last-child{margin-bottom:0;}
.goal-body{flex:1;}
.goal-name{font-size:13px;font-weight:500;display:block;margin-bottom:2px;color:var(--text);}
.goal-deadline{font-size:11px;color:var(--muted);}
.goal-remove{background:none;border:none;color:#111111;cursor:pointer;
             font-size:14px;padding:0 0 0 8px;line-height:1;transition:color .15s;flex-shrink:0;}
.goal-remove:hover{color:#ef4444;}

/* Coach */
.coach-text{font-size:13px;line-height:1.75;color:var(--muted);white-space:pre-line;}

/* Status */
.status-chip{display:inline-flex;align-items:center;gap:5px;
             padding:4px 10px;border-radius:12px;font-size:11px;font-weight:700;
             letter-spacing:.04em;margin-bottom:10px;}
.hard-truth{font-size:12px;font-style:italic;color:var(--amber);
            border-left:2px solid var(--amber);padding-left:10px;margin-top:12px;line-height:1.6;}
.replan-note{font-size:12px;color:var(--muted);line-height:1.65;margin-top:6px;}

/* Bars */
.bars{display:flex;align-items:flex-end;gap:5px;height:64px;padding-top:8px;}
.bar-wrap{display:flex;flex-direction:column;align-items:center;gap:3px;}
.bar{width:20px;background:var(--accent);border-radius:2px 2px 0 0;min-height:3px;opacity:.8;}
.bar-label{font-size:9px;color:#111111;}

/* Empty states */
.empty{text-align:center;padding:32px 20px;}
.empty-icon{font-size:24px;color:#111111;margin-bottom:10px;line-height:1;}
.empty-title{font-size:14px;font-weight:600;color:#111111;margin-bottom:6px;}
.empty-text{font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.6;}

/* Buttons */
.btn-primary{background:rgba(90,171,223,.15);border:1px solid rgba(90,171,223,.3);
             border-radius:2px;color:var(--text);font-size:12px;font-weight:600;
             letter-spacing:.05em;text-transform:uppercase;cursor:pointer;
             font-family:var(--sans);padding:9px 18px;transition:all .2s;}
.btn-primary:hover{background:rgba(90,171,223,.25);border-color:rgba(90,171,223,.5);}
.btn-primary:disabled{opacity:.45;cursor:not-allowed;}

.btn-google {
  display:flex;align-items:center;justify-content:center;gap:8px;
  width:100%;padding:10px;border:1px solid #dadce0;border-radius:6px;
  background:#fff;color:#3c4043;font-size:14px;cursor:pointer;
  text-decoration:none;margin-bottom:8px;
}
.btn-google:hover{background:#f8f8f8;border-color:#aaa;}
.btn-google svg{width:18px;height:18px;flex-shrink:0;}
.google-divider{display:flex;align-items:center;gap:8px;margin:12px 0;color:#888;font-size:13px;}
.google-divider::before,.google-divider::after{content:'';flex:1;height:1px;background:#e0e0e0;}
/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(10,20,50,.8);
               z-index:50;align-items:center;justify-content:center;backdrop-filter:blur(4px);}
.modal-overlay.open{display:flex;}
.modal{background:#0f2855;border:1px solid var(--border-m);border-radius:4px;
       padding:32px;width:100%;max-width:440px;}
.modal-title{font-family:var(--serif);font-size:20px;color:#1a2e4a;margin-bottom:20px;}
.modal-field{margin-bottom:16px;}
.modal-field label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.1em;
                    color:var(--muted);margin-bottom:7px;font-weight:500;}
.modal-field input{width:100%;background:rgba(255,255,255,.04);border:1px solid var(--border);
                    border-radius:2px;padding:10px 12px;font-size:13px;color:var(--text);
                    font-family:var(--sans);outline:none;transition:border-color .2s;}
.modal-field input:focus{border-color:var(--border-m);}
.modal-field input::placeholder{color:#111111;}
.modal-actions{display:flex;gap:10px;margin-top:20px;justify-content:flex-end;}
.btn-cancel{background:none;border:1px solid var(--border);border-radius:2px;color:var(--muted);
            font-size:12px;font-weight:500;cursor:pointer;font-family:var(--sans);padding:8px 16px;transition:all .2s;}
.btn-cancel:hover{border-color:var(--border-m);color:var(--text);}
.modal-err{font-size:12px;color:#c97b7b;margin-top:8px;min-height:16px;}

/* Artemis chat */
.art-toggle{position:fixed;bottom:24px;right:24px;z-index:40;
            background:rgba(15,40,85,.95);border:1px solid var(--border-m);
            border-radius:3px;padding:10px 18px;cursor:pointer;display:flex;
            align-items:center;gap:8px;color:var(--text);font-family:var(--sans);
            font-size:13px;font-weight:600;letter-spacing:.04em;
            box-shadow:0 4px 24px rgba(0,0,0,.4);transition:all .2s;}
.art-toggle:hover{background:rgba(25,55,110,.95);border-color:rgba(90,171,223,.4);}
.art-dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:.5;}}
.art-panel{position:fixed;bottom:76px;right:24px;z-index:40;
           width:360px;height:520px;background:rgba(10,25,60,.97);
           border:1px solid var(--border-m);border-radius:4px;
           display:none;flex-direction:column;
           box-shadow:0 8px 40px rgba(0,0,0,.5);}
.art-panel.open{display:flex;}
.art-header{padding:14px 16px;border-bottom:1px solid var(--border);
            display:flex;justify-content:space-between;align-items:center;flex-shrink:0;}
.art-name{font-size:13px;font-weight:700;letter-spacing:.05em;color:var(--text);}
.art-badge{font-size:9px;background:rgba(90,171,223,.2);color:var(--accent);
           border:1px solid rgba(90,171,223,.3);border-radius:8px;padding:2px 6px;margin-left:6px;
           font-weight:600;letter-spacing:.06em;text-transform:uppercase;}
.art-close{background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;
           line-height:1;padding:0;transition:color .15s;}
.art-close:hover{color:var(--text);}
.art-messages{flex:1;overflow-y:auto;padding:14px 14px 8px;display:flex;flex-direction:column;gap:10px;}
.art-messages::-webkit-scrollbar{width:4px;}
.art-messages::-webkit-scrollbar-track{background:transparent;}
.art-messages::-webkit-scrollbar-thumb{background:rgba(90,160,230,.2);border-radius:2px;}
.art-msg{max-width:88%;line-height:1.55;font-size:13px;padding:9px 12px;border-radius:3px;}
.art-msg-bot{background:rgba(20,45,95,.8);border:1px solid var(--border);color:var(--text);align-self:flex-start;}
.art-msg-user{background:rgba(90,171,223,.15);border:1px solid rgba(90,171,223,.25);
              color:var(--text);align-self:flex-end;}
.art-loading{color:var(--muted);font-style:italic;}
.art-footer{padding:10px 12px;border-top:1px solid var(--border);display:flex;gap:8px;flex-shrink:0;}
.art-input{flex:1;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:2px;
           padding:8px 10px;font-size:13px;color:var(--text);font-family:var(--sans);outline:none;transition:border .15s;}
.art-input:focus{border-color:var(--border-m);}
.art-input::placeholder{color:#111111;}
.art-send{background:rgba(90,171,223,.15);border:1px solid rgba(90,171,223,.3);border-radius:2px;
          color:var(--accent);font-size:12px;font-weight:600;cursor:pointer;
          padding:8px 14px;font-family:var(--sans);transition:all .15s;white-space:nowrap;}
.art-send:hover{background:rgba(90,171,223,.25);}
.art-send:disabled{opacity:.4;cursor:not-allowed;}

/* Loading */
.plan-loading{text-align:center;padding:40px 20px;}
.plan-loading-dots{display:flex;justify-content:center;gap:6px;margin-bottom:12px;}
.plan-loading-dots span{width:6px;height:6px;border-radius:50%;background:var(--accent);
                         animation:dot-bounce .9s infinite both;}
.plan-loading-dots span:nth-child(2){animation-delay:.15s;}
.plan-loading-dots span:nth-child(3){animation-delay:.3s;}
@keyframes dot-bounce{0%,80%,100%{transform:scale(.6);opacity:.4;}40%{transform:scale(1);opacity:1;}}
.plan-loading p{font-size:12px;color:var(--muted);}

/* Responsive */
@media(max-width:800px){
  .grid,.grid-full{grid-template-columns:1fr;}
  .stats-row{grid-template-columns:repeat(2,1fr);}
  .art-panel{width:calc(100vw - 32px);right:16px;}
}

/* Save indicator */
.save-ind{font-size:11px;color:var(--muted);}
"""

    def generate(self, goals: dict, memory: dict, daily_plan: dict,
                 coaching: str, replan: dict, habits: dict = None,
                 user_info: dict = None) -> str:

        today        = datetime.now().strftime("%B %d, %Y")
        streak       = memory.get("streak", 0)
        total_done   = memory.get("total_tasks_completed", 0)
        sessions     = memory.get("sessions", [])
        comp_rate    = int(self._calc_rate(sessions) * 100)
        display_name = user_info.get("display_name", "User") if user_info else "User"
        goals_list   = goals.get("goals", [])
        has_goals    = bool(goals_list)
        tasks        = daily_plan.get("tasks", []) if daily_plan else []
        has_plan     = bool(tasks)
        focus_theme  = daily_plan.get("focus_theme", "") if daily_plan else ""
        daily_intent = daily_plan.get("daily_intention", "") if daily_plan else ""

        # ── Pre-compute HTML fragments ──────────────────────────────────

        # Goals HTML
        goals_html = self._build_goals_html(goals_list)

        # Tasks HTML
        tasks_html = self._build_tasks_html(tasks)

        # Habits HTML
        habits_html = self._build_habits_html(habits, memory)

        # Bars HTML
        bars_html = self._build_bars_html(sessions)

        # Status chip (pre-compute to avoid f-string expression bug)
        status       = replan.get("status", "on_track") if replan else "on_track"
        s_colors     = {"on_track": "#10b981", "slightly_behind": "#f59e0b",
                        "significantly_behind": "#ef4444", "critical": "#dc2626"}
        status_color = s_colors.get(status, "#6b7280")
        status_label = status.replace("_", " ").title()
        hard_truth   = (replan or {}).get("hard_truth", "")
        hard_truth_html = (
            f'<div class="hard-truth">&ldquo;{hard_truth}&rdquo;</div>'
            if hard_truth else ""
        )
        assessment       = (replan or {}).get("assessment", "")
        adjusted_note    = (replan or {}).get("adjusted_plan_note", "")
        coach_text       = coaching if coaching else ""

        # Artemis greeting
        g_count = len(goals_list)
        art_intro = (
            f"Hello, {display_name}. I'm Artemis, your Helion AI assistant. "
            + (f"I can see your {g_count} active goal{'s' if g_count != 1 else ''}. "
               if has_goals else "You haven't added any goals yet. Use the Goals panel to get started. ")
            + "What can I help you with today?"
        )

        # Hero block
        if has_plan:
            hero_html = f"""
<div class="hero">
  <div class="hero-label">Personal Execution Engine</div>
  <h1 class="hero-title">{focus_theme}</h1>
  <p class="hero-sub">{daily_intent}</p>
</div>"""
        else:
            hero_html = f"""
<div class="hero">
  <div class="hero-label">Personal Execution Engine</div>
  <h1 class="hero-title">Good to see you, {display_name}.</h1>
  <p class="hero-sub">Add your goals and generate your first action plan to get started.</p>
</div>"""

        # Plan section content
        if has_goals and not has_plan:
            plan_content = """
<div class="empty">
  <div class="empty-icon">&#9671;</div>
  <div class="empty-title">No plan generated yet</div>
  <div class="empty-text">Generate today's action plan based on your goals.<br>
    This uses AI to create focused, specific tasks for you.</div>
  <button class="btn-primary" id="genPlanBtn" onclick="generatePlan()">Generate Today's Plan</button>
</div>"""
        elif not has_goals:
            plan_content = """
<div class="empty">
  <div class="empty-icon">&#9671;</div>
  <div class="empty-title">No goals yet</div>
  <div class="empty-text">Add at least one goal to generate your daily action plan.</div>
</div>"""
        else:
            plan_content = tasks_html

        # Goals section content
        if not has_goals:
            goals_content = """
<div class="empty">
  <div class="empty-icon">&#9651;</div>
  <div class="empty-title">No active goals</div>
  <div class="empty-text">Add your first goal to begin tracking your progress.</div>
</div>"""
        else:
            goals_content = goals_html

        # Coach section
        if coach_text:
            coach_section = f"""
<div class="card">
  <div class="card-head"><div class="card-title">Coach's Assessment</div></div>
  <div class="coach-text">{coach_text}</div>
</div>"""
        else:
            coach_section = f"""
<div class="card">
  <div class="card-head"><div class="card-title">Coach's Assessment</div></div>
  <div class="empty">
    <div class="empty-text">Generate your first plan to receive a coach's assessment.</div>
  </div>
</div>"""

        # Plan status section
        status_section = f"""
<div class="card">
  <div class="card-head"><div class="card-title">Plan Status</div></div>
  <div class="status-chip" style="background:{status_color}18;color:{status_color};border:1px solid {status_color}35;">
    &#9670; {status_label}
  </div>
  <div class="replan-note">{assessment}</div>
  <div class="replan-note">{adjusted_note}</div>
  {hard_truth_html}
</div>"""

        # Generate plan button (for header if plan exists)
        regen_btn = ""
        if has_goals and has_plan:
            regen_btn = '<button class="card-action" id="genPlanBtn" onclick="generatePlan()">Regenerate</button>'

        add_goal_btn = '<button class="card-action" onclick="showAddGoal()">+ Add Goal</button>'

        # Habits section
        habits_section = ""
        if habits_html:
            habits_section = f"""
<div class="card" style="margin-top:18px;">
  <div class="card-head"><div class="card-title">Habit Tracking</div></div>
  {habits_html}
</div>"""

        # Activity bars section
        activity_section = f"""
<div class="card" style="margin-bottom:20px;">
  <div class="card-head"><div class="card-title">7-Day Activity</div></div>
  <div class="bars">
    {bars_html if bars_html else '<p style="font-size:12px;color:var(--muted);">No history yet.</p>'}
  </div>
</div>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Helion AI</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>{self._CSS}</style>
</head>
<body>
<div class="atm"><div class="orb o1"></div><div class="orb o2"></div></div>
<div class="wrapper">

  <!-- Navigation -->
  <div class="nav">
    <div class="nav-brand">
      <svg viewBox="0 0 100 62" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="35" cy="11" r="5.5" stroke="#1a2e4a" stroke-width="1.9"/>
        <path d="M1,58 C8,44 20,31 35,24 C43,28 51,34 57,38 C61,32 65,27 69,27 C77,33 88,40 99,50"
              stroke="#1a2e4a" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div class="nav-title">Helion <span>AI</span></div>
    </div>
    <div class="nav-right">
      <span class="nav-user">{display_name}</span>
      <span id="saveInd" class="save-ind"></span>
      <a href="/artemis" class="nav-link" style="background:rgba(90,171,223,.12);border-color:rgba(90,171,223,.4);">&#9679; Artemis AI</a>
      <a href="/api/auth/logout" class="nav-link">Sign out</a>
    </div>
  </div>

  <!-- Hero -->
  {hero_html}

  <!-- Stats Row -->
  <div class="stats-row">
    <div class="stat-card">
      <div class="stat-value">{streak}</div>
      <div class="stat-label">Day Streak</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{comp_rate}%</div>
      <div class="stat-label">Completion Rate</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{len(goals_list)}</div>
      <div class="stat-label">Active Goals</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{total_done}</div>
      <div class="stat-label">Tasks Completed</div>
    </div>
  </div>

  <!-- Main Grid: Plan + Goals -->
  <div class="grid">
    <div>
      <div class="card">
        <div class="card-head">
          <div class="card-title">Today's Action Plan</div>
          {regen_btn}
        </div>
        <div id="planArea">{plan_content}</div>
      </div>
    </div>

    <div>
      <div class="card">
        <div class="card-head">
          <div class="card-title">Active Goals</div>
          {add_goal_btn}
        </div>
        {goals_content}
      </div>
      {habits_section}
    </div>
  </div>

  <!-- Activity -->
  {activity_section}

  <!-- Coach + Status -->
  <div class="grid-full">
    {coach_section}
    {status_section}
  </div>

</div>

<!-- Add Goal Modal -->
<div class="modal-overlay" id="addGoalModal">
  <div class="modal">
    <div class="modal-title">Add a New Goal</div>
    <div class="modal-field">
      <label>Goal Title</label>
      <input type="text" id="goalTitle" placeholder="e.g. Launch my product by Q3" maxlength="120"/>
    </div>
    <div class="modal-field">
      <label>Deadline (optional)</label>
      <input type="text" id="goalDeadline" placeholder="e.g. June 30, 2026 or open-ended"/>
    </div>
    <div class="modal-err" id="goalErr"></div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="hideAddGoal()">Cancel</button>
      <button class="btn-primary" id="addGoalBtn" onclick="submitGoal()">Add Goal</button>
    </div>
  </div>
</div>

<!-- Artemis is now at /artemis -->

<script>
// ── Task completion ───────────────────────────────────────
function toggleTask(box) {{
  const item = box.closest('.task-item');
  const done = item.classList.toggle('done');
  box.classList.toggle('checked', done);
  box.textContent = done ? '\u2713' : '';
  scheduleSave();
}}

let saveTimer = null;
let pendingChanges = {{}};
function scheduleSave() {{
  clearTimeout(saveTimer);
  const checks = document.querySelectorAll('.task-check.checked');
  pendingChanges.tasks_completed = Array.from(checks).map(c => c.dataset.id || '');
  saveTimer = setTimeout(doSave, 2000);
}}
function doSave() {{
  if (!Object.keys(pendingChanges).length) return;
  const ind = document.getElementById('saveInd');
  if (ind) {{ ind.textContent = 'Saving...'; }}
  fetch('/api/save', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(pendingChanges),
    credentials: 'include'
  }}).then(r => r.json()).then(d => {{
    if (ind) {{ ind.textContent = 'Saved'; setTimeout(() => {{ ind.textContent = ''; }}, 2000); }}
    pendingChanges = {{}};
  }}).catch(() => {{
    if (ind) ind.textContent = '';
  }});
}}
window.addEventListener('beforeunload', doSave);

// ── Add Goal Modal ────────────────────────────────────────
function showAddGoal() {{
  document.getElementById('addGoalModal').classList.add('open');
  document.getElementById('goalTitle').focus();
}}
function hideAddGoal() {{
  document.getElementById('addGoalModal').classList.remove('open');
  document.getElementById('goalTitle').value = '';
  document.getElementById('goalDeadline').value = '';
  document.getElementById('goalErr').textContent = '';
}}
async function submitGoal() {{
  const title    = document.getElementById('goalTitle').value.trim();
  const deadline = document.getElementById('goalDeadline').value.trim();
  const errEl    = document.getElementById('goalErr');
  if (!title) {{ errEl.textContent = 'Please enter a goal title.'; return; }}
  const btn = document.getElentById('addGoalBtn');
  btn.disabled = true; btn.textContent = 'Adding...'; errEl.textContent = '';
  try {{
    const r = await fetch('/api/goals/add', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      credentials: 'include',
      body: JSON.stringify({{ title, deadline: deadline || 'open-ended' }})
    }});
    const d = await r.json();
    if (d.success) {{ location.reload(); }}
    else {{ errEl.textContent = d.error || 'Failed to add goal.'; }}
  }} catch(e) {{
    errEl.textContent = 'Connection error.';
  }}
  btn.disabled = false; btn.textContent = 'Add Goal';
}}
document.getElementById('goalTitle').addEventListener('keydown', e => {{
  if (e.key === 'Enter') submitGoal();
}});

// ── Remove Goal ───────────────────────────────────────────
async function removeGoal(goalId) {{
  if (!confirm('Remove this goal? This cannot be undone.')) return;
  try {{
    const r = await fetch('/api/goals/remove', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      credentials: 'include',
      body: JSON.stringify({{ goal_id: goalId }})
    }});
    const d = await r.json();
    if (d.success) location.reload();
    else alert(d.error || 'Failed to remove goal.');
  }} catch(e) {{ alert('Connection error.'); }}
}}

// ── Generate Plan ─────────────────────────────────────────
async function generatePlan() {{
  const btn = document.getElementById('genPlanBtn');
  if (btn) {{ btn.disabled = true; btn.textContent = 'Generating...'; }}
  const planArea = document.getElementById('planArea');
  if (planArea) {{
    planArea.innerHTML = '<div class="plan-loading"><div class="plan-loading-dots"><span></span><span></span><span></span></div><p>Generating your action plan...</p></div>';
  }}
  try {{
    const r = await fetch('/api/plan/generate', {{
      method: 'POST', credentials: 'include'
    }});
    const d = await r.json();
    if (d.success) {{
      location.reload();
    }} else {{
      if (planArea) planArea.innerHTML = '<div class="empty"><div class="empty-text">' + (d.error || 'Plan generation failed. Check that ANTHROPIC_API_KEY is configured.') + '</div></div>';
      if (btn) {{ btn.disabled = false; btn.textContent = 'Try Again'; }}
    }}
  }} catch(e) {{
    if (planArea) planArea.innerHTML = '<div class="empty"><div class="empty-text">Connection error. Please try again.</div></div>';
    if (btn) {{ btn.disabled = false; btn.textContent = 'Try Again'; }}
  }}
}}

// Artemis chat is now at /artemis page
</script>
{self._snow_mountain_html()}
</body>
</html>"""

    # ── HTML fragment builders ────────────────────────────────────────────

    @staticmethod
    def _snow_mountain_html() -> str:
        """Returns the snow mountain canvas animation HTML/JS block."""
        return """
<canvas id="snowMtn" style="position:fixed;bottom:0;left:0;width:100%;height:280px;pointer-events:none;z-index:0;"></canvas>
<script>
(function(){
var cvs=document.getElementById('snowMtn');
var dpr=window.devicePixelRatio||1;
var ctx=cvs.getContext('2d');
var W,H,flakes=[],sunT=0;

function resize(){
  W=cvs.width=Math.round(window.innerWidth*dpr);
  H=cvs.height=Math.round(280*dpr);
  cvs.style.width=window.innerWidth+'px';
  cvs.style.height='280px';
  initFlakes();
}

function initFlakes(){
  flakes=[];
  var n=Math.min(900,Math.floor(W/2.2));
  for(var i=0;i<n;i++){
    flakes.push({
      x:Math.random()*W,
      y:Math.random()*H,
      r:(0.4+Math.random()*2.2)*dpr,
      speed:(0.4+Math.random()*1.1)*dpr,
      drift:(Math.random()-0.5)*0.5*dpr,
      op:0.35+Math.random()*0.65
    });
  }
}

// Mountain layer definitions  (relative: x=0..1 of W, y=pixels from bottom)
function mtnPath(pts,H){
  // pts: array of [xFrac, yFromBottom]
  ctx.beginPath();
  ctx.moveTo(0,H);
  for(var i=0;i<pts.length;i++){
    ctx.lineTo(pts[i][0]*W, H-pts[i][1]*dpr);
  }
  ctx.lineTo(W,H);
  ctx.closePath();
}

var layer0_pts=[
  [0,0],[0.05,28],[0.12,55],[0.18,38],[0.25,70],[0.30,52],[0.38,65],
  [0.45,42],[0.52,78],[0.58,55],[0.65,68],[0.72,44],[0.80,72],[0.88,50],[0.95,60],[1,0]
];
var layer1_pts=[
  [0,0],[0.04,35],[0.09,62],[0.15,48],[0.20,80],[0.27,60],[0.32,200],
  [0.37,68],[0.43,85],[0.50,55],[0.56,90],[0.63,62],[0.70,95],[0.77,58],
  [0.84,80],[0.91,65],[0.97,42],[1,0]
];
var layer2_pts=[
  [0,0],[0.06,42],[0.11,72],[0.17,55],[0.22,95],[0.28,75],[0.32,210],
  [0.36,78],[0.41,110],[0.48,68],[0.54,100],[0.60,72],[0.67,108],
  [0.74,70],[0.81,90],[0.88,68],[0.94,50],[1,0]
];

function drawMtn(){
  // Layer 0 - back
  mtnPath(layer0_pts,H);
  ctx.fillStyle='rgba(6,14,40,0.93)';
  ctx.fill();
  // Layer 1 - mid
  mtnPath(layer1_pts,H);
  ctx.fillStyle='rgba(9,20,55,0.96)';
  ctx.fill();
  // Layer 2 - front
  mtnPath(layer2_pts,H);
  ctx.fillStyle='rgba(11,26,65,1.0)';
  ctx.fill();
}

function mainPeakTop(){
  // Main peak is at x=0.32, y=210px from bottom in layer2
  return { x: 0.32*W, y: H - 210*dpr };
}

function drawSun(){
  var pt=mainPeakTop();
  var cx=pt.x, cy=pt.y-12*dpr;
  var cr=9*dpr;
  sunT+=0.018;

  // Outer glow
  var grd=ctx.createRadialGradient(cx,cy,cr*0.5,cx,cy,cr*3.5);
  grd.addColorStop(0,'rgba(160,210,255,0.28)');
  grd.addColorStop(1,'rgba(160,210,255,0)');
  ctx.beginPath();ctx.arc(cx,cy,cr*3.5,0,Math.PI*2);
  ctx.fillStyle=grd;ctx.fill();

  // Rays
  var nRays=16;
  for(var i=0;i<nRays;i++){
    var base=i*(Math.PI*2/nRays);
    var jitter=Math.sin(sunT*2.1+i*0.7)*0.12;
    var ang=base+jitter;
    var innerR=cr+3*dpr;
    var lenJitter=Math.sin(sunT*1.7+i*1.3)*5*dpr;
    var outerR=cr+18*dpr+lenJitter;
    ctx.beginPath();
    ctx.moveTo(cx+Math.cos(ang)*innerR, cy+Math.sin(ang)*innerR);
    ctx.lineTo(cx+Math.cos(ang)*outerR, cy+Math.sin(ang)*outerR);
    ctx.strokeStyle='rgba(180,220,255,0.55)';
    ctx.lineWidth=1.1*dpr;
    ctx.stroke();
  }

  // Circle
  ctx.beginPath();ctx.arc(cx,cy,cr,0,Math.PI*2);
  ctx.strokeStyle='rgba(200,230,255,0.85)';
  ctx.lineWidth=1.5*dpr;ctx.stroke();
  ctx.beginPath();ctx.arc(cx,cy,cr,0,Math.PI*2);
  ctx.fillStyle='rgba(140,190,255,0.18)';ctx.fill();
}

function drawSnow(){
  for(var i=0;i<flakes.length;i++){
    var f=flakes[i];
    ctx.globalAlpha=f.op;
    ctx.beginPath();ctx.arc(f.x,f.y,f.r,0,Math.PI*2);
    ctx.fillStyle='rgba(220,235,255,1)';ctx.fill();
    f.y+=f.speed; f.x+=f.drift;
    if(f.y>H+5){f.y=-5;f.x=Math.random()*W;}
    if(f.x>W+5) f.x=-5;
    if(f.x<-5) f.x=W+5;
  }
  ctx.globalAlpha=1;
}

function frame(){
  ctx.clearRect(0,0,W,H);
  drawMtn();
  drawSun();
  drawSnow();
  requestAnimationFrame(frame);
}

resize();
window.addEventListener('resize',resize);
requestAnimationFrame(frame);
})();
</script>"""

    def _build_tasks_html(self, tasks: list) -> str:
        if not tasks:
            return '<div class="empty"><div class="empty-text">No tasks in this plan.</div></div>'
        out = ""
        for t in tasks:
            priority = t.get("priority", "medium")
            tag_cls  = f"tag-{priority}" if priority in ("high", "medium", "low") else "tag-medium"
            momentum = '<span class="tag tag-start">&#9658; Priority start</span>' if t.get("is_momentum_task") else ""
            mins     = t.get("estimated_minutes", 60)
            out += f"""
<div class="task-item" id="ti_{t.get('id','')}">
  <div class="task-row">
    <div class="task-check" data-id="{t.get('id','')}" onclick="toggleTask(this)"></div>
    <div class="task-body">
      <div class="task-title">{t.get('title','')}</div>
      <div class="task-meta">
        <span class="tag {tag_cls}">{priority.upper()}</span>
        <span class="tag tag-time">{mins} min</span>
        {momentum}
        <span class="task-goal">&rsaquo; {t.get('goal_link','')}</span>
      </div>
      <div class="task-why">{t.get('why_today','')}</div>
    </div>
  </div>
</div>"""
        return out

    def _build_goals_html(self, goals_list: list) -> str:
        if not goals_list:
            return ""
        out = ""
        for g in goals_list:
            status = g.get("status", "active")
            color  = "#10b981" if status == "complete" else "#5aabdf"
            dl     = g.get("deadline", "open")
            gid    = g.get("id", "")
            out += f"""
<div class="goal-item" style="border-left-color:{color}">
  <div class="goal-body">
    <span class="goal-name">{g.get('title','')}</span>
    <span class="goal-deadline">Due: {dl}</span>
  </div>
  <button class="goal-remove" onclick="removeGoal('{gid}')" title="Remove goal">&#215;</button>
</div>"""
        return out

    def _build_habits_html(self, habits: dict, memory: dict) -> str:
        if not habits or not habits.get("habits"):
            return ""
        out = ""
        for h in habits["habits"]:
            hid   = h.get("id", "")
            title = h.get("title", "Habit")
            hlog  = memory.get("habit_log", {}).get(hid, [])
            grid  = ""
            for day_offset in range(29, -1, -1):
                day       = (datetime.now() - timedelta(days=day_offset)).strftime("%Y-%m-%d")
                completed = day in hlog
                color     = "#4ade80" if completed else "rgba(30,40,70,.8)"
                grid += f'<div style="width:8px;height:8px;background:{color};border-radius:1px;"></div>'
            out += f"""
<div style="margin-bottom:14px;">
  <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;
              letter-spacing:.08em;margin-bottom:5px;">{title}</div>
  <div style="display:grid;grid-template-columns:repeat(10,1fr);gap:3px;">{grid}</div>
</div>"""
        return out

    def _build_bars_html(self, sessions: list) -> str:
        if not sessions:
            return ""
        out = ""
        for s in sessions[-7:]:
            total = len(s.get("daily_plan", []))
            done  = len(s.get("tasks_completed", []))
            pct   = int(done / total * 100) if total else 0
            h     = max(3, int(pct * 0.56))
            date_s = s.get("date", "")[-5:] if s.get("date") else "--"
            out += f"""
<div class="bar-wrap">
  <div class="bar" style="height:{h}px;" title="{pct}% on {date_s}"></div>
  <div class="bar-label">{date_s}</div>
</div>"""
        return out

    def _calc_rate(self, sessions):
        if not sessions:
            return 0.0
        recent = sessions[-7:]
        rates  = []
        for s in recent:
            total = len(s.get("daily_plan", []))
            done  = len(s.get("tasks_completed", []))
            if total > 0:
                rates.append(done / total)
        return round(sum(rates) / len(rates), 2) if rates else 0.0


# ─────────────────────────────────────────────
# DEMO DATA (for --demo mode)
# ─────────────────────────────────────────────

DEMO_PLAN = {
    "date": datetime.now().strftime("%Y-%m-%d"),
    "focus_theme": "Build momentum on your product launch — small moves compound.",
    "daily_intention": "Every task you finish today is a vote for the person you are becoming.",
    "tasks": [
        {"id": "t1", "title": "Write 3 cold outreach emails to potential beta users",
         "goal_link": "Launch SaaS product", "estimated_minutes": 45,
         "priority": "high", "is_momentum_task": False,
         "why_today": "You are 2 days behind on outreach targets — close the gap now"},
        {"id": "t2", "title": "Finalize onboarding flow wireframes (screens 1-3 only)",
         "goal_link": "Launch SaaS product", "estimated_minutes": 60,
         "priority": "high", "is_momentum_task": False,
         "why_today": "Dev handoff is tomorrow — this blocks the sprint"},
        {"id": "t3", "title": "Read 20 pages of The Mom Test and take notes",
         "goal_link": "Learn customer discovery", "estimated_minutes": 30,
         "priority": "medium", "is_momentum_task": True,
         "why_today": "Easy win to start — builds momentum for the rest of the day"},
        {"id": "t4", "title": "Update LinkedIn with your new role and one insight post",
         "goal_link": "Build personal brand", "estimated_minutes": 25,
         "priority": "low", "is_momentum_task": False,
         "why_today": "Compound visibility — 10 minutes of writing now equals weeks of reach"},
    ]
}

DEMO_COACHING = """You are showing up, which is more than most people do — but showing up
isn't enough anymore. Your 7-day completion rate of 62% tells a specific story:
you start strong on Mondays, stall mid-week, then sprint on Fridays trying to
catch up. That is not a motivation problem. That is a planning problem.

The pattern holding you back is task inflation — you are consistently
over-scheduling yourself by 40%, then feeling like a failure when you cannot
finish. Your brain is lying to you about how long things take.

Here is what you have not considered: the tasks you keep deferring are not hard —
they are ambiguous. "Work on pitch deck" is not a task. It is a category. That is
why it keeps getting skipped.

Your challenge for the next 24 hours: complete only the two highest-priority tasks,
and do them before noon. Nothing else counts today. Build the habit of finishing
before you try to scale volume."""

DEMO_REPLAN = {
    "status": "slightly_behind",
    "assessment": "You are 15% behind your weekly target, driven by 3 deferred tasks that keep rolling over. Manageable, but compounding.",
    "adjusted_plan_note": "Focus the next 3 days on depth over breadth. Drop the optional tasks entirely and push hard on the 2 items that directly move your launch deadline.",
    "hard_truth": ""
}

DEMO_HABITS_DATA = {
    "habits": [
        {"id": "h1", "title": "Morning focus block", "frequency": "daily",
         "created": datetime.now().isoformat()},
        {"id": "h2", "title": "Movement", "frequency": "daily",
         "created": datetime.now().isoformat()},
        {"id": "h3", "title": "Reading", "frequency": "daily",
         "created": datetime.now().isoformat()},
    ]
}


# ─────────────────────────────────────────────
# ARTEMIS PAGE GENERATOR (full-page chat UI)
# ─────────────────────────────────────────────

class ArtemisPageGenerator:
    """Generates the full-screen Artemis AI chat page."""

    @staticmethod
    def generate(display_name: str = "there") -> str:
        snow = DashboardGenerator._snow_mountain_html()
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Artemis — Helion AI</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
:root{{
  --bg:#eaf3ff;--sidebar:rgba(210,230,255,0.97);--surface:rgba(255,255,255,0.88);
  --border:rgba(90,160,230,.10);--border-m:rgba(90,160,230,.22);
  --text:#1a2e4a;--muted:#333333;--accent:#5aabdf;
  --green:#10b981;--sans:'Inter',system-ui,sans-serif;--serif:'Playfair Display',Georgia,serif;
}}
html,body{{height:100%;overflow:hidden;}}
body{{font-family:var(--sans);background:var(--bg);color:var(--text);display:flex;height:100vh;}}
.atm{{position:fixed;inset:0;pointer-events:none;z-index:0;}}
.orb{{position:absolute;border-radius:50%;filter:blur(90px);opacity:.30;}}
.o1{{width:700px;height:500px;top:-150px;left:-100px;background:radial-gradient(ellipse,#90c8f0,transparent 70%);}}
.o2{{width:600px;height:400px;bottom:-100px;right:-100px;background:radial-gradient(ellipse,#b0d9ff,transparent 70%);}}

/* Sidebar */
.sidebar{{
  width:240px;flex-shrink:0;background:var(--sidebar);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:0;
  position:relative;z-index:2;
}}
.sidebar-top{{padding:22px 20px 18px;border-bottom:1px solid var(--border);}}
.back-link{{display:flex;align-items:center;gap:8px;color:var(--muted);
           font-size:12px;text-decoration:none;margin-bottom:20px;
           transition:color .15s;letter-spacing:.03em;}}
.back-link:hover{{color:var(--text);}}
.back-arrow{{font-size:14px;}}
.sidebar-brand{{display:flex;align-items:center;gap:10px;}}
.sidebar-logo-text{{font-size:12px;font-weight:600;letter-spacing:.09em;
                    text-transform:uppercase;color:#1a2e4a;}}
.sidebar-logo-text strong{{color:var(--text);}}
.sidebar-title{{font-family:var(--serif);font-size:22px;font-weight:700;
               color:#1a2e4a;margin-top:20px;}}
.sidebar-sub{{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.5;font-weight:300;}}

.sidebar-status{{padding:18px 20px;border-bottom:1px solid var(--border);}}
.status-dot{{display:inline-block;width:7px;height:7px;border-radius:50%;
            background:var(--green);margin-right:8px;animation:pulse 2s infinite;}}
@keyframes pulse{{0%,100%{{opacity:1;}}50%{{opacity:.4;}}}}
.status-text{{font-size:12px;color:var(--muted);}}

.convo-list{{flex:1;overflow-y:auto;padding:16px 12px;}}
.convo-list::-webkit-scrollbar{{width:3px;}}
.convo-list::-webkit-scrollbar-thumb{{background:rgba(90,160,230,.15);border-radius:2px;}}
.convo-item{{padding:10px 12px;border-radius:4px;cursor:pointer;font-size:13px;
            color:var(--muted);transition:all .15s;border:1px solid transparent;margin-bottom:4px;}}
.convo-item.active{{background:rgba(90,171,223,.08);border-color:rgba(90,171,223,.15);color:var(--text);}}
.convo-item:hover:not(.active){{background:rgba(255,255,255,.03);color:var(--text);}}
.convo-date{{font-size:10px;color:#111111;margin-top:3px;}}

.sidebar-footer{{padding:14px 16px;border-top:1px solid var(--border);font-size:11px;
               color:#111111;letter-spacing:.04em;}}

/* Main chat area */
.chat-area{{
  flex:1;display:flex;flex-direction:column;position:relative;z-index:2;
  min-width:0;
}}
.chat-header{{
  padding:16px 28px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(210,230,255,0.97);backdrop-filter:blur(12px);flex-shrink:0;
}}
.chat-header-title{{font-size:15px;font-weight:600;color:var(--text);letter-spacing:-.2px;}}
.chat-header-badge{{font-size:10px;background:rgba(90,171,223,.15);color:var(--accent);
                   border:1px solid rgba(90,171,223,.25);border-radius:10px;
                   padding:3px 9px;margin-left:10px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;}}
.chat-header-right{{font-size:12px;color:var(--muted);}}

.messages{{
  flex:1;overflow-y:auto;padding:28px 10% 20px;
  display:flex;flex-direction:column;gap:18px;
}}
.messages::-webkit-scrollbar{{width:5px;}}
.messages::-webkit-scrollbar-track{{background:transparent;}}
.messages::-webkit-scrollbar-thumb{{background:rgba(90,160,230,.15);border-radius:3px;}}

.msg-row{{display:flex;align-items:flex-start;gap:14px;max-width:820px;width:100%;}}
.msg-row.user-row{{align-self:flex-end;flex-direction:row-reverse;}}
.msg-row.bot-row{{align-self:flex-start;}}

.msg-avatar{{
  width:32px;height:32px;border-radius:50%;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:700;letter-spacing:.02em;
}}
.bot-avatar{{background:rgba(90,171,223,.18);border:1px solid rgba(90,171,223,.25);color:var(--accent);}}
.user-avatar{{background:rgba(140,185,235,.1);border:1px solid rgba(140,185,235,.18);color:var(--muted);font-size:11px;}}

.msg-bubble{{
  padding:14px 18px;border-radius:6px;line-height:1.65;font-size:14px;
  max-width:calc(100% - 50px)
}}
.bot-bubble{{
  background:rgba(15,38,85,.75);border:1px solid var(--border);color:#ffffff;
}}
.user-bubble{{
  background:rgba(90,171,223,.12);border:1px solid rgba(90,171,223,.2);color:var(--text);
}}

.loading-dots{{display:flex;gap:5px;padding:4px 0;}}
.loading-dots span{{width:6px;height:6px;border-radius:50%;background:var(--accent);
                   opacity:.4;animation:dotpulse .9s infinite both;}}
.loading-dots span:nth-child(2){{animation-delay:.15s;}}
.loading-dots span:nth-child(3){{animation-delay:.3s;}}
@keyframes dotpulse{{0%,80%,100%{{transform:scale(.6);opacity:.3;}}40%{{transform:scale(1);opacity:1;}}}}

/* Input bar */
.input-bar{{
  padding:16px 10%;padding-bottom:300px;
  background:transparent;flex-shrink:0;
}}
.input-wrap{{
  display:flex;gap:10px;align-items:flex-end;
  background:rgba(10,28,70,.88);border:1px solid var(--border-m);
  border-radius:8px;padding:12px 14px;
  box-shadow:0 4px 24px rgba(0,0,0,.25);
  backdrop-filter:blur(12px);
}}
.chat-input{{
  flex:1;background:transparent;border:none;outline:none;
  font-size:14px;color:#ffffff;font-family:var(--sans);
  resize:none;line-height:1.5;max-height:180px;min-height:24px;
  overflow-y:auto;
}}
.chat-input::placeholder{color:rgba(255,255,255,.5);}{{color:#111111;}}
.send-btn{{
  flex-shrink:0;width:36px;height:36px;border-radius:6px;
  background:rgba(90,171,223,.18);border:1px solid rgba(90,171,223,.3);
  color:var(--accent);cursor:pointer;display:flex;align-items:center;
  justify-content:center;transition:all .15s;font-size:16px;
}}
.send-btn:hover{{background:rgba(90,171,223,.3);}}
.send-btn:disabled{{opacity:.35;cursor:not-allowed;}}
.input-hint{{font-size:11px;color:#111111;margin-top:8px;
            text-align:center;letter-spacing:.02em;}}

/* Empty state */
.empty-chat{{
  flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:40px 20px;gap:12px;
}}
.empty-chat-title{{font-family:var(--serif);font-size:26px;color:#1a2e4a;}}
.empty-chat-sub{{font-size:14px;color:var(--muted);text-align:center;max-width:420px;line-height:1.6;}}
.suggestion-chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;justify-content:center;}}
.chip{{
  padding:8px 16px;background:rgba(90,171,223,.08);
  border:1px solid rgba(90,171,223,.18);border-radius:20px;
  font-size:12px;color:var(--muted);cursor:pointer;transition:all .15s;
}}
.chip:hover{{background:rgba(90,171,223,.16);color:var(--text);border-color:rgba(90,171,223,.3);}}

@media(max-width:700px){{
  .sidebar{{display:none;}}
  .messages,.input-bar{{padding-left:16px;padding-right:16px;}}
}}
  </style>
</head>
<body>
<div class="atm"><div class="orb o1"></div><div class="orb o2"></div></div>

<!-- Sidebar -->
<div class="sidebar">
  <div class="sidebar-top">
    <a href="/dashboard" class="back-link"><span class="back-arrow">&#8592;</span> Back to Dashboard</a>
    <div class="sidebar-brand">
      <svg width="28" height="18" viewBox="0 0 100 62" fill="none">
        <circle cx="35" cy="11" r="5.5" stroke="#1a2e4a" stroke-width="1.9"/>
        <path d="M1,58 C8,44 20,31 35,24 C43,28 51,34 57,38 C61,32 65,27 69,27 C77,33 88,40 99,50"
              stroke="#1a2e4a" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="sidebar-logo-text">Helion <strong>AI</strong></span>
    </div>
    <div class="sidebar-title">Artemis</div>
    <div class="sidebar-sub">Your personalized AI assistant. Ask about your goals, plans, and progress.</div>
  </div>
  <div class="sidebar-status">
    <span class="status-dot"></span>
    <span class="status-text">Online &mdash; ready to help</span>
  </div>
  <div class="convo-list">
    <div class="convo-item active">
      Current conversation
      <div class="convo-date" id="convDate"></div>
    </div>
  </div>
  <div class="sidebar-footer">Artemis AI &bull; Helion AI</div>
</div>

<!-- Chat area -->
<div class="chat-area">
  <div class="chat-header">
    <div>
      <span class="chat-header-title">Artemis</span>
      <span class="chat-header-badge">AI</span>
    </div>
    <div class="chat-header-right" id="chatStatus">Ready</div>
  </div>

  <div class="messages" id="messages">
    <!-- Empty state shown until first message -->
    <div class="empty-chat" id="emptyState">
      <div class="empty-chat-title">Hi, {display_name}.</div>
      <div class="empty-chat-sub">I'm Artemis, your Helion AI. I can help you think through your goals, break through blockers, and stay on track.</div>
      <div class="suggestion-chips">
        <div class="chip" onclick="sendChip(this)">What should I focus on today?</div>
        <div class="chip" onclick="sendChip(this)">Review my current goals</div>
        <div class="chip" onclick="sendChip(this)">I'm feeling stuck — help me</div>
        <div class="chip" onclick="sendChip(this)">How am I progressing overall?</div>
      </div>
    </div>
  </div>

  <div class="input-bar">
    <div class="input-wrap">
      <textarea class="chat-input" id="chatInput" rows="1"
        placeholder="Message Artemis..."
        onkeydown="handleKey(event)"
        oninput="autoResize(this)"></textarea>
      <button class="send-btn" id="sendBtn" onclick="sendMessage()" title="Send">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M14 8L2 2l2.5 6L2 14l12-6z" fill="currentColor"/>
        </svg>
      </button>
    </div>
    <div class="input-hint">Press Enter to send &bull; Shift+Enter for new line</div>
  </div>
</div>

<script>
var chatHistory = [];
var sending = false;

// Set today's date
var d = new Date();
document.getElementById('convDate').textContent =
  d.toLocaleDateString('en-US', {{month:'short',day:'numeric',year:'numeric'}});

function autoResize(el) {{
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 180) + 'px';
}}

function handleKey(e) {{
  if (e.key === 'Enter' && !e.shiftKey) {{
    e.preventDefault();
    sendMessage();
  }}
}}

function sendChip(el) {{
  document.getElementById('chatInput').value = el.textContent;
  sendMessage();
}}

function appendMsg(role, text) {{
  var empty = document.getElementById('emptyState');
  if (empty) empty.remove();
  var msgs = document.getElementById('messages');
  var row = document.createElement('div');
  row.className = 'msg-row ' + (role === 'user' ? 'user-row' : 'bot-row');
  var avatarText = role === 'user' ? 'You' : 'A';
  var avatarCls  = role === 'user' ? 'user-avatar' : 'bot-avatar';
  var bubbleCls  = role === 'user' ? 'user-bubble' : 'bot-bubble';
  row.innerHTML =
    '<div class="msg-avatar ' + avatarCls + '">' + avatarText + '</div>' +
    '<div class="msg-bubble ' + bubbleCls + '">' + escHtml(text) + '</div>';
  msgs.appendChild(row);
  msgs.scrollTop = msgs.scrollHeight;
  return row;
}}

function appendLoading() {{
  var empty = document.getElementById('emptyState');
  if (empty) empty.remove();
  var msgs = document.getElementById('messages');
  var row = document.createElement('div');
  row.id = 'loadRow';
  row.className = 'msg-row bot-row';
  row.innerHTML =
    '<div class="msg-avatar bot-avatar">A</div>' +
    '<div class="msg-bubble bot-bubble"><div class="loading-dots">' +
    '<span></span><span></span><span></span></div></div>';
  msgs.appendChild(row);
  msgs.scrollTop = msgs.scrollHeight;
}}

function removeLoading() {{
  var el = document.getElementById('loadRow');
  if (el) el.remove();
}}

function escHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/\\n/g,'<br>');
}}

async function sendMessage() {{
  var input = document.getElementById('chatInput');
  var btn   = document.getElementById('sendBtn');
  var msg   = input.value.trim();
  if (!msg || sending) return;

  sending = true;
  btn.disabled = true;
  document.getElementById('chatStatus').textContent = 'Thinking...';

  appendMsg('user', msg);
  input.value = '';
  input.style.height = 'auto';
  chatHistory.push({{role:'user', content:msg}});

  appendLoading();
  try {{
    var r = await fetch('/api/chat', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      credentials: 'include',
      body: JSON.stringify({{messages: chatHistory}})
    }});
    var data = await r.json();
    removeLoading();
    var reply = data.reply || 'I had trouble with that. Please try again.';
    appendMsg('bot', reply);
    chatHistory.push({{role:'assistant', content:reply}});
    if (chatHistory.length > 40) chatHistory = chatHistory.slice(-40);
  }} catch(e) {{
    removeLoading();
    appendMsg('bot', 'Connection error. Please try again.');
  }}

  sending = false;
  btn.disabled = false;
  document.getElementById('chatStatus').textContent = 'Ready';
  document.getElementById('chatInput').focus();
}}
</script>
{snow}
</body>
</html>"""

# ─────────────────────────────────────────────
# ORCHESTRATOR
# ─────────────────────────────────────────────

class LifeOSOrchestrator:
    def __init__(self, api_key: str, username: str = None):
        self.client   = AnthropicClient(api_key)
        self.username = username
        self.goals    = GoalManager(user_goals_file(username) if username else None)
        self.memory   = MemorySystem(user_memory_file(username) if username else None)
        self.habits   = HabitTracker(self.memory, user_habits_file(username) if username else None)
        self.planner  = PlannerAgent(self.client)
        self.coach    = CoachAgent(self.client)
        self.replanner = ReplannerAgent(self.client)
        self.artemis   = ArtemisAgent(self.client)
        self.dashboard = DashboardGenerator()

    def run_daily_cycle(self) -> str:
        today    = datetime.now().strftime("%Y-%m-%d")
        goals_summary = self.goals.get_goals_summary()
        recent        = self.memory.get_recent_sessions()
        rate          = self.memory.get_completion_rate()

        plan     = self.planner.generate_plan(goals_summary, recent, today)
        coaching = self.coach.generate_coaching(goals_summary, recent, rate)
        replan   = self.replanner.analyze_and_replan(goals_summary, recent, rate)

        # Save plan to user plan file (for dashboard retrieval)
        if self.username:
            user_plan_file(self.username).write_text(json.dumps(plan, indent=2))
            user_coach_file(self.username).write_text(json.dumps({
                "coaching": coaching,
                "replan":   replan,
                "generated": datetime.now().isoformat()
            }, indent=2))

        html = self.dashboard.generate(
            self.goals.data, self.memory.data, plan, coaching, replan,
            self.habits.data
        )

        self.memory.update_streak()
        self.memory.log_session({
            "date":             today,
            "daily_plan":       plan.get("tasks", []),
            "tasks_completed":  [],
            "coaching_summary": coaching[:120],
            "status":           replan.get("status", "unknown"),
        })

        DASHBOARD_FILE.write_text(html)
        return str(DASHBOARD_FILE)

    def checkin(self, completed_ids: list):
        if not self.memory.data["sessions"]:
            print("No active session. Run --run first.")
            return
        session = self.memory.data["sessions"][-1]
        session["tasks_completed"] = completed_ids
        self.memory.save()
        rate = len(completed_ids) / max(len(session.get("daily_plan", [])), 1)
        print(f"\nCheck-in recorded. Completion: {int(rate*100)}%")


# ─────────────────────────────────────────────
# HTTP SERVER
# ─────────────────────────────────────────────

class LifeOSServer(http.server.SimpleHTTPRequestHandler):

    _user_mgr    = UserManager()
    _session_mgr = SessionManager()

    def log_message(self, fmt, *args):
        pass

    def _token(self):
        cookie = http.cookies.SimpleCookie()
        if "Cookie" in self.headers:
            cookie.load(self.headers["Cookie"])
        m = cookie.get("lifeos_session")
        return m.value if m else None

    def _user(self):
        return self._session_mgr.get_user(self._token())

    def _html(self, html_str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_str.encode("utf-8"))

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _redir(self, path):
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8") if length > 0 else ""

    def _load_user_dashboard_data(self, username):
        """Load the user's actual plan/coaching data (not demo data)."""
        plan     = {}
        coaching = ""
        replan   = {"status": "on_track", "assessment": "", "adjusted_plan_note": "", "hard_truth": ""}

        pf = user_plan_file(username)
        if pf.exists():
            try:
                plan = json.loads(pf.read_text())
            except:
                plan = {}

        cf = user_coach_file(username)
        if cf.exists():
            try:
                raw      = json.loads(cf.read_text())
                coaching = raw.get("coaching", "")
                replan   = raw.get("replan", replan)
            except:
                pass

        return plan, coaching, replan

    # ── GET handlers ─────────────────────────────────────────────────────

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        user = self._user()

        if path == "/":
            if user:
                self._redir("/dashboard")
            else:
                self._html(LoginPageGenerator().generate())
            return

        if path == "/dashboard":
            if not user:
                self._redir("/")
                return
            orch      = LifeOSOrchestrator(get_api_key(), user)
            user_info = self._user_mgr.get_user(user)
            plan, coaching, replan = self._load_user_dashboard_data(user)
            html = orch.dashboard.generate(
                orch.goals.data, orch.memory.data, plan, coaching, replan,
                orch.habits.data, user_info
            )
            self._html(html)
            return

        if path == "/api/auth/logout":
            token = self._token()
            if token:
                self._session_mgr.delete(token)
            self.send_response(302)
            self.send_header("Set-Cookie", "lifeos_session=; Max-Age=0")
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/api/me":
            if not user:
                self._json({"error": "Not authenticated"}, 401)
                return
            self._json(self._user_mgr.get_user(user) or {})
            return

        if path == "/artemis":
            if not user:
                self._redir("/")
                return
            user_info    = self._user_mgr.get_user(user)
            display_name = user_info.get("display_name", "there") if user_info else "there"
            self._html(ArtemisPageGenerator.generate(display_name))
            return

        if path == "/api/data":
            if not user:
                self._json({"error": "Not authenticated"}, 401)
                return
            orch = LifeOSOrchestrator(get_api_key(), user)
            self._json({
                "goals":  orch.goals.data,
                "memory": orch.memory.data,
                "habits": orch.habits.data,
            })
            return

        # ── Google OAuth ──────────────────────────────────────────────────────
        if path == "/auth/google":
            cid    = os.environ.get("GOOGLE_CLIENT_ID", "")
            redir  = os.environ.get("GOOGLE_REDIRECT_URI", "")
            scopes = "openid email profile"
            url    = (
                "https://accounts.google.com/o/oauth2/v2/auth"
                f"?client_id={cid}&redirect_uri={redir}"
                f"&response_type=code&scope={urllib.parse.quote(scopes)}"
                "&access_type=offline&prompt=select_account"
            )
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
            return

        if path == "/auth/google/callback":
            import urllib.request as _ur
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            code   = params.get("code", "")
            cid    = os.environ.get("GOOGLE_CLIENT_ID", "")
            csec   = os.environ.get("GOOGLE_CLIENT_SECRET", "")
            redir  = os.environ.get("GOOGLE_REDIRECT_URI", "")
            # Exchange code for token
            token_data = urllib.parse.urlencode({
                "code": code, "client_id": cid, "client_secret": csec,
                "redirect_uri": redir, "grant_type": "authorization_code",
            }).encode()
            req  = _ur.Request("https://oauth2.googleapis.com/token", data=token_data)
            resp = json.loads(_ur.urlopen(req).read())
            # Decode JWT id_token (no-verify; trust TLS)
            payload_b64 = resp["id_token"].split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            import base64 as _b64
            info    = json.loads(_b64.urlsafe_b64decode(payload_b64))
            email   = info.get("email", "")
            name    = info.get("name", email.split("@")[0])
            username = email.split("@")[0].lower().replace(".", "_")
            # Create user if new, else just log in
            if not self._user_mgr.get_user(username):
                self._user_mgr.create_user(username, secrets.token_hex(16), name, email)
            token = self._sess_mgr.create_session(username)
            self.send_response(302)
            self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    # ── POST handlers ────────────────────────────────────────────────────

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()

        # ── Auth ──────────────────────────────────────────────────────────

        if path == "/api/auth/login":
            try:
                d        = json.loads(body)
                username = d.get("username", "").strip()
                password = d.get("password", "")
                if self._user_mgr.verify(username, password):
                    token = self._session_mgr.create(username)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie",
                        f"lifeos_session={token}; Path=/; Max-Age=86400; HttpOnly")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True}).encode())
                else:
                    self._json({"success": False, "error": "Invalid username or password"})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        if path == "/api/auth/register":
            try:
                d            = json.loads(body)
                username     = d.get("username", "").strip()
                password     = d.get("password", "")
                display_name = d.get("display_name", "").strip()
                email        = d.get("email", "").strip()
                ok, msg      = self._user_mgr.create_user(username, password, display_name, email)
                if ok:
                    token = self._session_mgr.create(username)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie",
                        f"lifeos_session={token}; Path=/; Max-Age=86400; HttpOnly")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True}).encode())
                else:
                    self._json({"success": False, "error": msg})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        # ── All other endpoints require auth ──────────────────────────────

        user = self._user()
        if not user and path not in ("/api/auth/login", "/api/auth/register"):
            self._json({"error": "Not authenticated"}, 401)
            return

        if path == "/api/save":
            try:
                d    = json.loads(body)
                orch = LifeOSOrchestrator(get_api_key(), user)
                if "tasks_completed" in d:
                    if not orch.memory.data["sessions"]:
                        orch.memory.log_session({
                            "date": datetime.now().strftime("%Y-%m-%d"),
                            "daily_plan": [], "tasks_completed": []
                        })
                    orch.memory.data["sessions"][-1]["tasks_completed"] = d["tasks_completed"]
                    orch.memory.save()
                self._json({"success": True})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        # ── Goals management ──────────────────────────────────────────────

        if path == "/api/goals/add":
            try:
                d    = json.loads(body)
                title    = d.get("title", "").strip()
                deadline = d.get("deadline", "open-ended").strip()
                if not title:
                    self._json({"success": False, "error": "Goal title is required"})
                    return
                if len(title) > 140:
                    self._json({"success": False, "error": "Goal title too long (max 140 chars)"})
                    return
                orch = LifeOSOrchestrator(get_api_key(), user)
                orch.goals.add_goal(title, deadline)
                self._json({"success": True, "goal_count": len(orch.goals.data["goals"])})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        if path == "/api/goals/remove":
            try:
                d       = json.loads(body)
                goal_id = d.get("goal_id", "").strip()
                if not goal_id:
                    self._json({"success": False, "error": "goal_id required"})
                    return
                orch = LifeOSOrchestrator(get_api_kez(), user)
                orch.goals.remove_goal(goal_id)
                self._json({"success": True})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        # ── Plan generation ───────────────────────────────────────────────

        if path == "/api/plan/generate":
            api_key = get_api_key()
            if not api_key:
                self._json({"success": False,
                            "error": "ANTHROPIC_API_KEY is not configured on this server."})
                return
            try:
                orch         = LifeOSOrchestrator(api_key, user)
                goals_summary = orch.goals.get_goals_summary()
                if goals_summary == "No goals configured yet.":
                    self._json({"success": False, "error": "Add at least one goal before generating a plan."})
                    return
                today   = datetime.now().strftime("%Y-%m-%d")
                recent  = orch.memory.get_recent_sessions()
                rate    = orch.memory.get_completion_rate()
                plan    = orch.planner.generate_plan(goals_summary, recent, today)
                coaching = orch.coach.generate_coaching(goals_summary, recent, rate)
                replan   = orch.replanner.analyze_and_replan(goals_summary, recent, rate)

                # Save to user files
                user_plan_file(user).write_text(json.dumps(plan, indent=2))
                user_coach_file(user).write_text(json.dumps({
                    "coaching":  coaching,
                    "replan":    replan,
                    "generated": datetime.now().isoformat()
                }, indent=2))

                # Log to memory
                orch.memory.update_streak()
                orch.memory.log_session({
                    "date":            today,
                    "daily_plan":      plan.get("tasks", []),
                    "tasks_completed": [],
                    "coaching_summary": coaching[:120],
                    "status":          replan.get("status", "unknown"),
                })
                self._json({"success": True, "task_count": len(plan.get("tasks", []))})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        # ── Artemis Chat ──────────────────────────────────────────────────

        if path == "/api/chat":
            api_key = get_api_key()
            if not api_key:
                self._json({"reply": "I'm not available right now — the API key is not configured on this server."})
                return
            try:
                d        = json.loads(body)
                messages = d.get("messages", [])
                if not messages or not isinstance(messages, list):
                    self._json({"reply": "No message received."})
                    return
                # Cap history to last 20 turns
                messages = messages[-20:]

                orch      = LifeOSOrchestrator(api_key, user)
                user_info = self._user_mgr.get_user(user)
                display_name = user_info.get("display_name", "there") if user_info else "there"

                goals_summary = orch.goals.get_goals_summary()

                # Build concise plan summary for context
                plan_summary = ""
                pf = user_plan_file(user)
                if pf.exists():
                    try:
                        plan = json.loads(pf.read_text())
                        tasks = plan.get("tasks", [])
                        if tasks:
                            plan_summary = f"Today's plan ({plan.get('focus_theme','')}):\n"
                            for t in tasks[:6]:
                                plan_summary += f"- {t.get('title','')} [{t.get('priority','')}]\n"
                    except:
                        pass

                reply = orch.artemis.chat(
                    messages, goals_summary, plan_summary, display_name
                )
                self._json({"reply": reply})
            except Exception as e:
                self._json({"reply": f"I encountered an error: {str(e)[:120]}"})
            return

        self.send_response(404)
        self.end_headers()


# ─────────────────────────────────────────────
# SETUP WIZARD (CLI only)
# ─────────────────────────────────────────────

def run_setup():
    print("\n" + "="*50)
    print("   Helion AI — Setup Wizard")
    print("="*50 + "\n")

    api_key = input("Enter your Anthropic API key (sk-ant-...): ").strip()
    API_KEY_FILE.write_text(api_key)

    goals_data = {"goals": [], "context": {}}
    name = input("\nYour name: ").strip()
    role = input("Your current role/status: ").strip()
    goals_data["context"] = {"name": name, "role": role}

    print("\nLet's add your goals (up to 4). Press Enter to skip.")
    for i in range(1, 5):
        title = input(f"\nGoal {i} title (or Enter to skip): ").strip()
        if not title:
            break
        deadline  = input(f"  Deadline for '{title}': ").strip()
        sub_str   = input(f"  Sub-goals (comma separated, or Enter): ").strip()
        sub_goals = [s.strip() for s in sub_str.split(",") if s.strip()] if sub_str else []
        goals_data["goals"].append({
            "id":        hashlib.md5(title.encode()).hexdigest()[:8],
            "title":     title,
            "deadline":  deadline or "open-ended",
            "status":    "active",
            "created":   datetime.now().isoformat(),
            "sub_goals": [{"title": sg, "priority": "medium"} for sg in sub_goals],
        })

    with open(GOALS_FILE, "w") as f:
        json.dump(goals_data, f, indent=2)

    print(f"\nSetup complete. {len(goals_data['goals'])} goal(s) saved.")
    print("Run: python3 life_os_agent.py --run\n")


# ─────────────────────────────────────────────
# CLI ENTRYPOINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Helion AI — Personal Execution Engine (v4)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 life_os_agent.py --setup          First-time setup
  python3 life_os_agent.py --run            Run daily cycle
  python3 life_os_agent.py --checkin t1 t3  Log completed tasks by ID
  python3 life_os_agent.py --demo           Demo mode (no API key needed)
  python3 life_os_agent.py --serve --port 8080  Start web server"""
    )
    parser.add_argument("--setup",     action="store_true", help="Run setup wizard")
    parser.add_argument("--run",       action="store_true", help="Run daily cycle")
    parser.add_argument("--checkin",   nargs="+", metavar="TASK_ID")
    parser.add_argument("--dashboard", action="store_true", help="Regenerate dashboard")
    parser.add_argument("--demo",      action="store_true", help="Demo mode")
    parser.add_argument("--serve",     action="store_true", help="Start web server")
    parser.add_argument("--port",      type=int, default=8080)
    args = parser.parse_args()

    if args.setup:
        run_setup()
        return

    if args.demo:
        print("\nRunning in DEMO mode...\n")
        goals = GoalManager()
        if not goals.data["goals"]:
            goals.data = {
                "goals": [
                    {"id": "a1b2", "title": "Launch SaaS product MVP",
                     "deadline": "May 15, 2026", "status": "active",
                     "created": datetime.now().isoformat(),
                     "sub_goals": [{"title": "Build onboarding flow", "priority": "high"},
                                   {"title": "Get 10 beta users", "priority": "high"}]},
                    {"id": "c3d4", "title": "Learn customer discovery",
                     "deadline": "ongoing", "status": "active",
                     "created": datetime.now().isoformat(), "sub_goals": []},
                    {"id": "e5f6", "title": "Build personal brand",
                     "deadline": "ongoing", "status": "active",
                     "created": datetime.now().isoformat(), "sub_goals": []},
                ],
                "context": {"name": "Demo User", "role": "Entrepreneur"}
            }
        memory = MemorySystem()
        memory.data["streak"] = 7
        memory.data["total_tasks_completed"] = 43
        memory.data["sessions"] = [
            {"date": (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"),
             "daily_plan": [{"id": f"t{j}"} for j in range(4)],
             "tasks_completed": [f"t{j}" for j in range(int(4 * [0.5,0.75,1.0,0.5,0.75,0.5,0.75][i % 7]))],
             "status": "on_track"} for i in range(6, -1, -1)
        ]
        gen  = DashboardGenerator()
        html = gen.generate(goals.data, memory.data, DEMO_PLAN, DEMO_COACHING,
                            DEMO_REPLAN, DEMO_HABITS_DATA)
        DASHBOARD_FILE.write_text(html)
        print(f"  Demo dashboard generated: {DASHBOARD_FILE}\n")
        return

    if args.serve:
        port = int(os.environ.get("PORT", args.port))
        print(f"\nStarting Helion AI Server on port {port}")
        print("Press Ctrl+C to stop.\n")
        try:
            with socketserver.ThreadingTCPServer(("", port), LifeOSServer) as httpd:
                httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
        return

    # Real mode — need API key
    api_key = (API_KEY_FILE.read_text().strip() if API_KEY_FILE.exists()
               else os.environ.get("ANTHROPIC_API_KEY", ""))

    if not api_key and (args.run or args.dashboard):
        print("\nNo API key found. Run --setup first, or set ANTHROPIC_API_KEY env var.")
        print("Or try --demo to see it in action without an API key.\n")
        sys.exit(1)

    if args.run:
        orch = LifeOSOrchestrator(api_key)
        path = orch.run_daily_cycle()
        print(f"\nDone. Dashboard saved to: {path}\n")

    elif args.checkin:
        orch = LifeOSOrchestrator(api_key)
        orch.checkin(args.checkin)

    elif args.dashboard:
        orch    = LifeOSOrchestrator(api_key)
        recent  = orch.memory.get_recent_sessions()
        rate    = orch.memory.get_completion_rate()
        plan    = {"tasks": recent[-1].get("daily_plan", []),
                   "focus_theme": "Regenerated dashboard",
                   "daily_intention": ""} if recent else {}
        html = orch.dashboard.generate(
            orch.goals.data, orch.memory.data, plan, DEMO_COACHING,
            DEMO_REPLAN, orch.habits.data
        )
        DASHBOARD_FILE.write_text(html)
        print(f"\nDashboard regenerated: {DASHBOARD_FILE}\n")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
