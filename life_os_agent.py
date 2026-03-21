#!/usr/bin/env python3
"""
Life OS Agent — Personal Execution Engine (v3 with Multi-User Support)
===========================================================================
A multi-agent AI system that acts as your personal chief of staff.
It breaks down your goals, generates daily action plans, tracks progress,
and dynamically replans when you fall behind.

Now with multi-user support: web-based login, session management, and per-user data storage.

Architecture:
  AnthropicClient      → Raw API client (no external deps)
  UserManager          → User registration & authentication (v3 new)
  SessionManager       → Session tokens & expiration (v3 new)
  GoalManager          → Load/save/update goals & deadlines
  MemorySystem         → Persistent state across sessions
  HabitTracker         → Track daily & weekly habits
  PlannerAgent         → Breaks goals into daily actions
  CoachAgent           → Evaluates progress, gives direct feedback
  ReplannerAgent       → Adjusts plans dynamically when off-track
  LoginPageGenerator   → HTML login/signup page (v3 new)
  DashboardGenerator   → Generates the HTML visual report
  LifeOSServer         → HTTP handler for web server (v3 new)
  LifeOSOrchestrator   → Coordinates all agents end-to-end

Usage:
  python3 life_os_agent.py --setup          # First-time setup wizard
  python3 life_os_agent.py --run            # Run the full daily cycle
  python3 life_os_agent.py --checkin        # Log what you completed today
  python3 life_os_agent.py --dashboard      # Regenerate the HTML dashboard only
  python3 life_os_agent.py --demo           # Run in demo mode (no API key needed)
  python3 life_os_agent.py --serve --port 8080  # Start web server (v3 new)

Author: Built with Anthropic Claude API
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
from typing import Optional
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
GOALS_FILE = BASE_DIR / "goals.json"
MEMORY_FILE = BASE_DIR / "memory.json"
HABITS_FILE = BASE_DIR / "habits.json"
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
API_KEY_FILE = BASE_DIR / ".api_key"

DATA_DIR       = BASE_DIR / "data"
USERS_FILE     = DATA_DIR / "users.json"
SESSIONS_FILE  = DATA_DIR / "sessions.json"

def user_goals_file(u):   return DATA_DIR / f"goals_{u}.json"
def user_memory_file(u):  return DATA_DIR / f"memory_{u}.json"
def user_habits_file(u):  return DATA_DIR / f"habits_{u}.json"
def user_api_key_file(u): return DATA_DIR / f".api_key_{u}"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-opus-4-6"


# ─────────────────────────────────────────────
# ANTHROPIC CLIENT (pure stdlib, no pip needed)
# ─────────────────────────────────────────────

class AnthropicClient:
    """Minimal Anthropic API client using Python stdlib only."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def message(self, system: str, user: str, max_tokens: int = 1500) -> str:
        payload = json.dumps({
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")

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
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["content"][0]["text"]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            raise RuntimeError(f"API error {e.code}: {body}")


# ─────────────────────────────────────────────
# GOAL MANAGER
# ─────────────────────────────────────────────

class GoalManager:
    """Loads and manages your goals, sub-goals, and deadlines."""

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
            lines.append(f"- [{status.upper()}] Goal: {g['title']} | Deadline: {deadline}")
            for sg in g.get("sub_goals", []):
                lines.append(f"    • Sub-goal: {sg['title']} | Priority: {sg.get('priority','medium')}")
        return "\n".join(lines) if lines else "No goals configured yet."

    def add_goal(self, title: str, deadline: str = None, sub_goals: list = None):
        self.data["goals"].append({
            "id": hashlib.md5(title.encode()).hexdigest()[:8],
            "title": title,
            "deadline": deadline or "open-ended",
            "status": "active",
            "created": datetime.now().isoformat(),
            "sub_goals": [{"title": sg, "priority": "medium"} for sg in (sub_goals or [])],
        })
        self.save()


# ─────────────────────────────────────────────
# MEMORY SYSTEM
# ─────────────────────────────────────────────

class MemorySystem:
    """Persistent memory across daily sessions. Tracks plans, completions, streaks."""

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
        # Keep last 30 sessions
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
    """Tracks daily and weekly habits with completion log."""

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
        """Get completion rate for a habit over N days."""
        habit_log = self.memory.data.get("habit_log", {})
        if habit_id not in habit_log:
            return 0.0
        completions = habit_log[habit_id]
        if not completions:
            return 0.0
        recent = [c for c in completions if (datetime.now() - datetime.fromisoformat(c)).days < days]
        return len(recent) / days if days > 0 else 0.0

    def log_completion(self, habit_id: str):
        """Mark a habit as completed today."""
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
    """
    Breaks down goals into a focused, achievable daily action plan.
    Prioritizes tasks based on deadlines, momentum, and effort.
    """

    SYSTEM = """You are a world-class executive coach and productivity strategist.
Your job: given someone's goals, context, and recent progress, generate a hyper-focused
daily action plan. Be specific, actionable, and realistic (3-6 tasks max).

Rules:
- Each task must be completable in under 2 hours
- Be ruthlessly specific (not "work on project" but "write 300-word intro for Section 2 of pitch deck")
- Prioritize by impact × urgency
- Include one 'momentum task' that's easy to start to beat procrastination
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
      "is_momentum_task": true|false,
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

        raw = self.client.message(self.SYSTEM, user_msg, max_tokens=800)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: extract JSON block if wrapped in markdown
            import re
            match = re.search(r'\{[\s\S]+\}', raw)
            if match:
                return json.loads(match.group())
            raise ValueError(f"Planner returned non-JSON: {raw[:200]}")


# ─────────────────────────────────────────────
# COACH AGENT
# ─────────────────────────────────────────────

class CoachAgent:
    """
    Analyzes your progress and delivers direct, honest, motivating feedback.
    Talks to you like a mix of a startup CEO and a personal trainer.
    """

    SYSTEM = """You are a direct, data-driven personal coach — think a mix of a
startup CEO and elite performance coach. No fluff, no empty praise.

Analyze the user's completion data and goals, then deliver a coaching message that:
1. Opens with a direct assessment (2-3 sentences) — what's working, what's not
2. Calls out the #1 pattern holding them back (be specific, be honest)
3. Gives one insight they probably haven't considered
4. Closes with an energizing, specific challenge for the next 24 hours

Tone: Direct, warm, intelligent. Like a mentor who genuinely wants you to win.
Length: 200-250 words max. No bullet points — write in natural paragraphs."""

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
    """
    Detects when you're falling behind and dynamically adjusts the plan.
    Reschedules, deprioritizes, or escalates tasks as needed.
    """

    SYSTEM = """You are a tactical replanning agent. When someone is falling behind
on their goals, you restructure their plan — not to make them feel better,
but to actually get them back on track.

Analyze the situation and output a replanning decision as JSON:

{
  "status": "on_track|slightly_behind|significantly_behind|critical",
  "assessment": "2 sentences on what's happening",
  "dropped_tasks": ["task titles to defer or drop entirely"],
  "escalated_tasks": ["task titles that must happen in the next 48h"],
  "adjusted_plan_note": "One paragraph on how to approach the next 3 days",
  "hard_truth": "One sentence of honest accountability"
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
# EMAIL DIGEST SAMPLE
# ─────────────────────────────────────────────

class EmailDigest:
    """Generates weekly email digest of progress."""

    @staticmethod
    def generate_digest(goals_summary: str, memory: dict) -> str:
        streak = memory.get("streak", 0)
        rate = 0.0
        sessions = memory.get("sessions", [])
        if sessions:
            recent = sessions[-7:]
            rates = []
            for s in recent:
                total = len(s.get("daily_plan", []))
                done = len(s.get("tasks_completed", []))
                if total > 0:
                    rates.append(done / total)
            rate = sum(rates) / len(rates) if rates else 0.0
        return f"Weekly Summary: {streak}-day streak, {int(rate*100)}% completion rate"


# ─────────────────────────────────────────────
# USER MANAGER (v3 new)
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

    def create_user(self, username, password, display_name=""):
        username = username.lower().strip()
        if len(username) < 3:
            return False, "Username must be at least 3 characters"
        if not username.replace("_","").replace("-","").isalnum():
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
            "created_at": datetime.now().isoformat(),
        }
        self._save()
        for fpath, default in [
            (user_goals_file(username),  {"goals": [], "context": {}}),
            (user_memory_file(username), {"sessions": [], "habit_log": {}, "streak": 0, "total_tasks_completed": 0, "last_run": None}),
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
        return {"username": username, "display_name": u["display_name"], "created_at": u["created_at"]}


# ─────────────────────────────────────────────
# SESSION MANAGER (v3 new)
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
# LOGIN PAGE GENERATOR (v3 new)
# ─────────────────────────────────────────────

class LoginPageGenerator:
    def generate(self):
        return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Life OS — Sign In</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
    :root{--bg:#0e2244;--surface:rgba(15,40,85,.78);--border:rgba(90,160,230,.12);
          --border-m:rgba(90,160,230,.25);--text:#d2e8ff;--muted:rgba(140,185,235,.55);
          --serif:'Playfair Display',Georgia,serif;--sans:'Inter',system-ui,sans-serif;}
    body{font-family:var(--sans);background:var(--bg);color:var(--text);
         min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden;}
    .atm{position:fixed;inset:0;pointer-events:none;}
    .orb{position:absolute;border-radius:50%;filter:blur(90px);opacity:.38;}
    .o1{width:700px;height:500px;top:-150px;left:-100px;background:radial-gradient(ellipse,#2470d0,transparent 70%);}
    .o2{width:600px;height:400px;bottom:-100px;right:-100px;background:radial-gradient(ellipse,#1660b8,transparent 70%);}
    .card{position:relative;z-index:1;width:100%;max-width:420px;background:var(--surface);
          border:1px solid var(--border);border-radius:4px;padding:44px 40px 40px;backdrop-filter:blur(24px);}
    .logo{display:flex;align-items:center;gap:10px;margin-bottom:36px;}
    .logo-text{font-size:13px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:rgba(200,218,245,.7);}
    .logo-text strong{color:var(--text);}
    .card-title{font-family:var(--serif);font-size:28px;font-weight:700;color:#e8f0fa;margin-bottom:8px;}
    .card-sub{font-size:13px;color:var(--muted);font-weight:300;margin-bottom:32px;line-height:1.5;}
    .tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:28px;}
    .tab{flex:1;text-align:center;padding:10px 0;font-size:13px;font-weight:500;color:var(--muted);
         cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;letter-spacing:.04em;}
    .tab.active{color:var(--text);border-bottom-color:#5aabdf;}
    .field{margin-bottom:18px;}
    .field label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.1em;
                  color:var(--muted);margin-bottom:8px;font-weight:500;}
    .field input{width:100%;background:rgba(255,255,255,.04);border:1px solid var(--border);
                  border-radius:2px;padding:11px 14px;font-size:14px;color:var(--text);
                  font-family:var(--sans);outline:none;transition:border-color .2s;}
    .field input:focus{border-color:var(--border-m);}
    .field input::placeholder{color:rgba(140,185,235,.22);}
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
      <circle cx="35" cy="11" r="5.5" stroke="white" stroke-width="1.9"/>
      <path d="M1,58 C8,44 20,31 35,24 C43,28 51,34 57,38 C61,32 65,27 69,27 C77,33 88,40 99,50"
            stroke="white" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <span class="logo-text">Life <strong>OS</strong></span>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="sw('login')">Sign In</div>
    <div class="tab" onclick="sw('register')">Create Account</div>
  </div>
  <div id="tl" class="card-title">Welcome back.</div>
  <div id="tr" class="card-title" style="display:none">Get started.</div>
  <div id="sl" class="card-sub">Your goals and habits are waiting.</div>
  <div id="sr" class="card-sub" style="display:none">Create your personal Life OS account.</div>
  <form onsubmit="go(event)">
    <div class="field signup-only" id="fn">
      <label>Your Name</label>
      <input type="text" id="display_name" placeholder="e.g. Akshat" autocomplete="name"/>
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
</div>
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
# DASHBOARD GENERATOR (updated for v3)
# ─────────────────────────────────────────────

class DashboardGenerator:
    """Generates a beautiful, self-contained HTML dashboard."""

    def generate(self, goals: dict, memory: dict, daily_plan: dict,
                 coaching: str, replan: dict, habits: dict = None, user_info: dict = None) -> str:

        today = datetime.now().strftime("%B %d, %Y")
        streak = memory.get("streak", 0)
        total_done = memory.get("total_tasks_completed", 0)
        sessions = memory.get("sessions", [])
        completion_rate = int(self._calc_rate(sessions) * 100)

        # Nav user/date display
        nav_right = ""
        if user_info:
            nav_right = f"""<div style="display:flex;gap:16px;align-items:center;">
              <span style="font-size:13px;color:var(--muted);">{user_info.get('display_name','User')}</span>
              <a href="/api/auth/logout" style="font-size:12px;color:var(--accent);text-decoration:none;">Sign out</a>
            </div>"""
        else:
            nav_right = f'<div style="font-size:13px;color:var(--muted);">{today}</div>'

        # Auto-save indicator (shown when lifeos_session cookie exists)
        autosave_html = """<div id="asind" style="font-size:11px;color:var(--muted);display:none;">Saving...</div>"""

        tasks_html = ""
        for t in daily_plan.get("tasks", []):
            priority_color = {"high": "#ef4444", "medium": "#f59e0b", "low": "#10b981"}.get(
                t.get("priority", "medium"), "#6b7280")
            momentum_badge = '<span class="badge momentum">⚡ Start here</span>' if t.get("is_momentum_task") else ""
            tasks_html += f"""
            <div class="task-card">
              <div class="task-header">
                <div class="task-checkbox" onclick="toggleTask(this)">☐</div>
                <div class="task-content">
                  <div class="task-title">{t.get('title', '')}</div>
                  <div class="task-meta">
                    <span class="badge" style="background:{priority_color}20;color:{priority_color};border:1px solid {priority_color}40">
                      {t.get('priority','medium').upper()}
                    </span>
                    <span class="badge time">⏱ {t.get('estimated_minutes', 60)} min</span>
                    {momentum_badge}
                    <span class="task-goal">→ {t.get('goal_link','')}</span>
                  </div>
                  <div class="task-why">{t.get('why_today','')}</div>
                </div>
              </div>
            </div>"""

        goals_html = ""
        for g in goals.get("goals", []):
            status = g.get("status", "active")
            color = "#10b981" if status == "complete" else "#5aabdf"
            goals_html += f"""
            <div class="goal-pill" style="border-left: 3px solid {color}">
              <strong>{g['title']}</strong>
              <span class="goal-deadline">📅 {g.get('deadline','open')}</span>
            </div>"""

        status = replan.get("status", "on_track")
        status_color_map = {
            "on_track": "#10b981", "slightly_behind": "#f59e0b",
            "significantly_behind": "#ef4444", "critical": "#dc2626"
        }
        status_color = status_color_map.get(status, "#6b7280")
        status_label = status.replace("_", " ").title()

        recent_bars = ""
        for s in sessions[-7:]:
            total = len(s.get("daily_plan", []))
            done = len(s.get("tasks_completed", []))
            pct = int((done / total * 100)) if total else 0
            date_short = s.get("date", "")[-5:] if s.get("date") else "—"
            recent_bars += f"""
            <div class="bar-wrap">
              <div class="bar" style="height:{{max(4, pct * 0.8)}}px" title="{{pct}}% on {{date_short}}"></div>
              <div class="bar-label">{{date_short}}</div>
            </div>"""

        # Habits heatmap
        habits_html = ""
        if habits and habits.get("habits"):
            for h in habits["habits"]:
                habit_id = h.get("id", "")
                habit_title = h.get("title", "Habit")
                habit_log = memory.get("habit_log", {}).get(habit_id, [])
                # 30-day grid
                grid = ""
                for day_offset in range(29, -1, -1):
                    day = (datetime.now() - timedelta(days=day_offset)).strftime("%Y-%m-%d")
                    completed = day in habit_log
                    color = "#4ade80" if completed else "#1a1a2e"
                    grid += f'<div style="width:8px;height:8px;background:{color};border-radius:1px;"></div>'
                habits_html += f"""
            <div style="margin-bottom:16px;">
              <div style="font-size:12px;font-weight:600;margin-bottom:6px;">{habit_title}</div>
              <div style="display:grid;grid-template-columns:repeat(10,1fr);gap:3px;">{grid}</div>
            </div>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Life OS — {today}</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0;}}
    :root{{--bg:#0e2244;--surface:rgba(15,40,85,.72);--border:rgba(90,160,230,.10);--text:#d2e8ff;--muted:rgba(140,185,235,.55);--accent:#5aabdf;--serif:'Playfair Display',Georgia,serif;--sans:'Inter',system-ui,sans-serif;}}
    body{{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;padding:24px;}}
    .atm{{position:fixed;inset:0;pointer-events:none;}}
    .orb{{position:absolute;border-radius:50%;filter:blur(90px);opacity:.40;}}
    .o1{{width:800px;height:600px;top:-200px;left:-100px;background:radial-gradient(ellipse,#2470d0,transparent 70%);}}
    .o2{{width:700px;height:500px;bottom:-150px;right:-100px;background:radial-gradient(ellipse,#1660b8,transparent 70%);}}
    .o3{{width:600px;height:400px;top:50%;left:50%;transform:translate(-50%,-50%);background:radial-gradient(ellipse,#1255b0,transparent 70%);}}
    .wrapper{{position:relative;z-index:1;max-width:1200px;margin:0 auto;}}

    /* Navigation */
    .nav{{display:flex;justify-content:space-between;align-items:center;margin-bottom:32px;padding-bottom:20px;border-bottom:1px solid var(--border);}}
    .nav-brand{{display:flex;align-items:center;gap:10px;}}
    .nav-brand svg{{width:40px;height:26px;}}
    .nav-text{{font-size:14px;font-weight:600;letter-spacing:-.5px;font-family:var(--serif);}}
    .nav-text strong{{color:var(--accent);}}

    /* Header & Stats */
    .hero{{margin-bottom:32px;}}
    .hero-eyebrow{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:8px;}}
    .hero-title{{font-family:var(--serif);font-size:32px;font-weight:700;color:#e8f0fa;margin-bottom:8px;}}
    .hero-sub{{font-size:14px;color:var(--muted);line-height:1.6;}}

    .stats-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;margin-bottom:28px;}}
    .stat-card{{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:0 28px;text-align:center;}}
    .stat-value{{font-size:40px;font-weight:800;color:#fff;line-height:1.2;padding:16px 0;}}
    .stat-label{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;padding-bottom:16px;}}

    /* Grid Layout */
    .grid{{display:grid;grid-template-columns:2fr 1fr;gap:24px;margin-bottom:24px;}}

    /* Cards */
    .card{{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:24px;}}
    .card-title{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:16px;font-weight:600;}}

    /* Focus Theme */
    .focus-theme{{font-family:var(--serif);font-size:20px;font-weight:700;color:#e8f0fa;margin-bottom:8px;line-height:1.3;}}
    .intention{{font-size:13px;color:var(--muted);font-style:italic;}}

    /* Tasks */
    .task-card{{background:rgba(30,30,46,.5);border:1px solid var(--border);border-radius:2px;padding:14px 16px;margin-bottom:10px;transition:border-color .2s;}}
    .task-card:hover{{border-color:var(--border);}}
    .task-card.done{{opacity:0.4;}}
    .task-card.done .task-title{{text-decoration:line-through;}}
    .task-header{{display:flex;gap:12px;align-items:flex-start;}}
    .task-checkbox{{font-size:18px;cursor:pointer;color:#4a4a6a;flex-shrink:0;margin-top:1px;user-select:none;}}
    .task-checkbox.checked{{color:#10b981;}}
    .task-title{{font-size:14px;font-weight:600;margin-bottom:6px;color:#e2e8f0;}}
    .task-meta{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:5px;}}
    .badge{{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600;white-space:nowrap;}}
    .badge.time{{background:#1e2a3a;color:#7dd3fc;}}
    .badge.momentum{{background:#fef3c720;color:#fbbf24;border:1px solid #fbbf2440;}}
    .task-goal{{font-size:11px;color:#4a4a6a;}}
    .task-why{{font-size:12px;color:var(--muted);margin-top:2px;}}

    /* Coach */
    .coach-text{{font-size:14px;line-height:1.7;color:var(--muted);white-space:pre-line;}}

    /* Status */
    .status-chip{{display:inline-flex;align-items:center;gap:6px;background:{status_color}18;color:{status_color};border:1px solid {status_color}35;padding:5px 12px;border-radius:20px;font-size:12px;font-weight:700;margin-bottom:12px;}}
    .hard-truth{{font-size:13px;font-style:italic;color:#f59e0b;border-left:3px solid #f59e0b;padding-left:10px;margin-top:12px;}}
    .replan-note{{font-size:13px;color:var(--muted);line-height:1.6;margin-top:8px;}}

    /* Goals sidebar */
    .goal-pill{{background:rgba(30,30,46,.5);border-radius:2px;padding:10px 14px;margin-bottom:8px;border-left:3px solid #5aabdf;}}
    .goal-pill strong{{font-size:13px;display:block;margin-bottom:2px;}}
    .goal-deadline{{font-size:11px;color:var(--muted);}}

    /* Bar chart */
    .bars{{display:flex;align-items:flex-end;gap:6px;height:70px;padding-top:10px;}}
    .bar-wrap{{display:flex;flex-direction:column;align-items:center;gap:4px;}}
    .bar{{width:24px;background:var(--accent);border-radius:2px 2px 0 0;min-height:4px;}}
    .bar-label{{font-size:10px;color:#4a4a6a;}}

    /* Footer */
    .footer{{margin-top:40px;text-align:center;font-size:11px;color:#2a2a3e;}}
    .footer svg{{display:inline-block;height:60px;margin:20px 0;opacity:.3;}}

    .coach-grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:24px;}}
  </style>
</head>
<body>
<div class="atm"><div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div></div>
<div class="wrapper">

  <div class="nav">
    <div class="nav-brand">
      <svg viewBox="0 0 100 62" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="35" cy="11" r="5.5" stroke="white" stroke-width="1.9"/>
        <path d="M1,58 C8,44 20,31 35,24 C43,28 51,34 57,38 C61,32 65,27 69,27 C77,33 88,40 99,50" stroke="white" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div class="nav-text">Life <strong>OS</strong></div>
    </div>
    {nav_right}
    {autosave_html}
  </div>

  <div class="hero">
    <div class="hero-eyebrow">Personal Execution Engine</div>
    <h1 class="hero-title">{daily_plan.get('focus_theme', 'Focus on what matters most.')}</h1>
    <p class="hero-sub">{daily_plan.get('daily_intention', '')}</p>
  </div>

  <div class="stats-row">
    <div class="stat-card">
      <div class="stat-value">🔥 {streak}</div>
      <div class="stat-label">Day Streak</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{completion_rate}%</div>
      <div class="stat-label">Task Rate</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">—</div>
      <div class="stat-label">Habit Rate</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">{total_done}</div>
      <div class="stat-label">Total Done</div>
    </div>
  </div>

  <div class="grid">
    <div>
      <div class="card">
        <div class="card-title">Today's Action Plan</div>
        {tasks_html}
      </div>
    </div>

    <div>
      <div class="card" style="margin-bottom:20px">
        <div class="card-title">Active Goals</div>
        {goals_html if goals_html else '<p style="color:var(--muted);font-size:13px">No goals set. Run --setup.</p>'}
      </div>

      {f'<div class="card"><div class="card-title">Habit Tracking</div>{habits_html}</div>' if habits_html else ''}
    </div>
  </div>

  <div class="grid">
    <div style="display:contents;">
      <div class="card">
        <div class="card-title">7-Day Activity</div>
        <div class="bars">{recent_bars if recent_bars else '<p style="color:var(--muted);font-size:12px">No history yet.</p>'}</div>
      </div>
    </div>
  </div>

  <div class="coach-grid">
    <div class="card">
      <div class="card-title">Coach's Message</div>
      <div class="coach-text">{coaching}</div>
    </div>

    <div class="card">
      <div class="card-title">Plan Status</div>
      <div class="status-chip">● {status_label}</div>
      <div class="replan-note">{replan.get('assessment', '')}</div>
      <div class="replan-note" style="margin-top:8px">{replan.get('adjusted_plan_note', '')}</div>
      {{f'<div class="hard-truth">\\"{replan.get("hard_truth","")}\\"</div>' if replan.get("hard_truth") else ""}}
    </div>
  </div>

  <div class="footer">
    <svg viewBox="0 0 100 20" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">
      <polygon points="0,10 10,2 20,10 30,4 40,10 50,6 60,10 70,5 80,10 90,7 100,10 100,20 0,20" fill="url(#grad1)"/>
      <defs><linearGradient id="grad1" x1="0%" x2="0%" y1="0%" y2="100%"><stop offset="0%" style="stop-color:rgba(90,160,230,.2);stop-opacity:1"/><stop offset="100%" style="stop-color:rgba(90,160,230,0);stop-opacity:1"/></linearGradient></defs>
    </svg>
    <p>Built with Anthropic Claude API · Life OS Agent v3.0</p>
  </div>

</div>

<script>
function toggleTask(checkbox){{
  const card=checkbox.closest('.task-card');
  const isDone=card.classList.toggle('done');
  checkbox.textContent=isDone?'✓':'☐';
  checkbox.classList.toggle('checked',isDone);
  if(document.cookie.includes('lifeos_session'))flushPending();
}}

let pendingChanges={{}};
function flushPending(){{
  if(Object.keys(pendingChanges).length===0)return;
  const ind=document.getElementById('asind');
  if(ind){{ind.style.display='block';ind.textContent='Saving...';}}
  fetch('/api/save',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(pendingChanges),credentials:'include'}})
    .then(r=>r.json()).then(d=>{{
      if(ind){{ind.textContent='Saved ✓';setTimeout(()=>{{ind.style.display='none';}},2000);}}
      pendingChanges={{}};
    }}).catch(e=>{{
      if(ind)ind.textContent='Save failed';
    }});
}}
setInterval(flushPending,30000);
window.addEventListener('beforeunload',flushPending);
</script>
</body>
</html>"""

    def _calc_rate(self, sessions):
        if not sessions:
            return 0.0
        recent = sessions[-7:]
        rates = []
        for s in recent:
            total = len(s.get("daily_plan", []))
            done = len(s.get("tasks_completed", []))
            if total > 0:
                rates.append(done / total)
        return round(sum(rates) / len(rates), 2) if rates else 0.0


# ─────────────────────────────────────────────
# DEMO MODE DATA (no API key needed)
# ─────────────────────────────────────────────

DEMO_PLAN = {
    "date": datetime.now().strftime("%Y-%m-%d"),
    "focus_theme": "Build momentum on your product launch — small moves compound",
    "daily_intention": "Every task you finish today is a vote for the person you're becoming.",
    "tasks": [
        {"id": "t1", "title": "Write 3 cold outreach emails to potential beta users",
         "goal_link": "Launch SaaS product", "estimated_minutes": 45,
         "priority": "high", "is_momentum_task": False,
         "why_today": "You're 2 days behind on outreach targets — close the gap now"},
        {"id": "t2", "title": "Finalize onboarding flow wireframes (screens 1-3 only)",
         "goal_link": "Launch SaaS product", "estimated_minutes": 60,
         "priority": "high", "is_momentum_task": False,
         "why_today": "Dev handoff is tomorrow — this blocks the sprint"},
        {"id": "t3", "title": "Read 20 pages of 'The Mom Test' and take notes",
         "goal_link": "Learn customer discovery", "estimated_minutes": 30,
         "priority": "medium", "is_momentum_task": True,
         "why_today": "⚡ Easy win to start — builds momentum for the rest of the day"},
        {"id": "t4", "title": "Update LinkedIn with your new role + 1 insight post",
         "goal_link": "Build personal brand", "estimated_minutes": 25,
         "priority": "low", "is_momentum_task": False,
         "why_today": "Compound visibility — 10 min of writing now = weeks of reach"},
    ]
}

DEMO_COACHING = """You're showing up, which is more than most people do — but showing up
isn't enough anymore. Your 7-day completion rate of 62% tells a specific story:
you start strong on Mondays, stall mid-week, then sprint on Fridays trying to
catch up. That's not a motivation problem. That's a planning problem.

The pattern holding you back is task inflation — you're consistently
over-scheduling yourself by 40%, then feeling like a failure when you can't
finish. Your brain is lying to you about how long things take.

Here's what you haven't considered: the tasks you keep deferring aren't hard —
they're ambiguous. "Work on pitch deck" isn't a task. It's a category. That's
why it keeps getting skipped.

Your challenge for the next 24 hours: complete ONLY the two highest-priority tasks,
and do them before noon. Nothing else counts today. Build the habit of finishing
before you try to scale volume."""

DEMO_REPLAN = {
    "status": "slightly_behind",
    "assessment": "You're 15% behind your weekly target, driven by 3 deferred tasks that keep rolling over. Manageable, but compounding.",
    "adjusted_plan_note": "Focus the next 3 days on depth over breadth. Drop the 'nice to have' tasks entirely and push hard on the 2 items that directly move your launch deadline.",
    "hard_truth": "You're scheduling to feel productive, not to make progress — those aren't the same thing."
}

DEMO_HABITS_DATA = {
    "habits": [
        {"id": "h1", "title": "Morning focus", "frequency": "daily", "created": datetime.now().isoformat()},
        {"id": "h2", "title": "Movement", "frequency": "daily", "created": datetime.now().isoformat()},
        {"id": "h3", "title": "Reading", "frequency": "daily", "created": datetime.now().isoformat()},
        {"id": "h4", "title": "No social media", "frequency": "daily", "created": datetime.now().isoformat()},
    ]
}

DEMO_WEEKLY_REVIEW = {
    "week_of": datetime.now().strftime("%Y-%m-%d"),
    "tasks_completed": 24,
    "tasks_planned": 30,
    "completion_rate": 0.80,
    "top_blocker": "Context switching mid-task",
    "next_week_focus": "Protect focus time: block 2-hour deep work windows",
}


# ─────────────────────────────────────────────
# ORCHESTRATOR (updated for v3)
# ─────────────────────────────────────────────

class LifeOSOrchestrator:
    """Coordinates all agents to run the full daily Life OS cycle."""

    def __init__(self, api_key: str, username: str = None):
        self.client = AnthropicClient(api_key)
        self.username = username
        goals_file = user_goals_file(username) if username else None
        memory_file = user_memory_file(username) if username else None
        habits_file = user_habits_file(username) if username else None
        self.goals = GoalManager(goals_file)
        self.memory = MemorySystem(memory_file)
        self.habits = HabitTracker(self.memory, habits_file)
        self.planner = PlannerAgent(self.client)
        self.coach = CoachAgent(self.client)
        self.replanner = ReplannerAgent(self.client)
        self.dashboard = DashboardGenerator()

    def run_daily_cycle(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        print(f"\n⚡ Life OS Agent running for {today}...\n")

        goals_summary = self.goals.get_goals_summary()
        recent = self.memory.get_recent_sessions()
        rate = self.memory.get_completion_rate()

        print("  [1/4] Generating today's action plan...")
        plan = self.planner.generate_plan(goals_summary, recent, today)

        print("  [2/4] Running coach analysis...")
        coaching = self.coach.generate_coaching(goals_summary, recent, rate)

        print("  [3/4] Checking plan status & replanning if needed...")
        replan = self.replanner.analyze_and_replan(goals_summary, recent, rate)

        print("  [4/4] Building dashboard...")
        html = self.dashboard.generate(
            self.goals.data, self.memory.data, plan, coaching, replan,
            self.habits.data
        )

        # Log session
        self.memory.update_streak()
        self.memory.log_session({
            "date": today,
            "daily_plan": plan.get("tasks", []),
            "tasks_completed": [],
            "coaching_summary": coaching[:100],
            "status": replan.get("status", "unknown"),
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
        print(f"\n✅ Check-in recorded. Completion: {int(rate*100)}%")


# ─────────────────────────────────────────────
# HTTP SERVER (v3 new)
# ─────────────────────────────────────────────

class LifeOSServer(http.server.SimpleHTTPRequestHandler):
    """HTTP handler for web server with authentication."""

    _user_mgr = UserManager()
    _session_mgr = SessionManager()

    def log_message(self, fmt, *args):
        pass

    def _token(self):
        """Extract session token from cookie."""
        cookie = http.cookies.SimpleCookie()
        if "Cookie" in self.headers:
            cookie.load(self.headers["Cookie"])
        return cookie.get("lifeos_session", http.cookies.Morsel()).value if "lifeos_session" in cookie else None

    def _user(self):
        """Get authenticated username from session token."""
        token = self._token()
        if not token:
            return None
        return self._session_mgr.get_user(token)

    def _html(self, html_str):
        """Send HTML response."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_str.encode("utf-8"))

    def _json(self, data):
        """Send JSON response."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _redir(self, path):
        """Send redirect."""
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def _body(self):
        """Read request body."""
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8") if length > 0 else ""

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        user = self._user()

        if path == "/":
            if user:
                self._redir("/dashboard")
            else:
                gen = LoginPageGenerator()
                self._html(gen.generate())
            return

        if path == "/dashboard":
            if not user:
                self._redir("/")
                return
            orch = LifeOSOrchestrator(os.environ.get("ANTHROPIC_API_KEY", ""), user)
            user_info = self._user_mgr.get_user(user)
            html = orch.dashboard.generate(
                orch.goals.data, orch.memory.data, DEMO_PLAN, DEMO_COACHING,
                DEMO_REPLAN, orch.habits.data, user_info
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
            user = self._user()
            if not user:
                self._json({"error": "Not authenticated"})
                return
            self._json(self._user_mgr.get_user(user) or {})
            return

        if path == "/api/data":
            user = self._user()
            if not user:
                self._json({"error": "Not authenticated"})
                return
            orch = LifeOSOrchestrator(os.environ.get("ANTHROPIC_API_KEY", ""), user)
            self._json({
                "goals": orch.goals.data,
                "memory": orch.memory.data,
                "habits": orch.habits.data,
            })
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()

        if path == "/api/auth/login":
            try:
                data = json.loads(body)
                username = data.get("username", "").strip()
                password = data.get("password", "")
                if self._user_mgr.verify(username, password):
                    token = self._session_mgr.create(username)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", f"lifeos_session={token}; Path=/; Max-Age=86400; HttpOnly")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
                else:
                    self._json({"success": False, "error": "Invalid username or password"})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        if path == "/api/auth/register":
            try:
                data = json.loads(body)
                username = data.get("username", "").strip()
                password = data.get("password", "")
                display_name = data.get("display_name", "").strip()
                success, msg = self._user_mgr.create_user(username, password, display_name)
                if success:
                    token = self._session_mgr.create(username)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", f"lifeos_session={token}; Path=/; Max-Age=86400; HttpOnly")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
                else:
                    self._json({"success": False, "error": msg})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        if path == "/api/save":
            user = self._user()
            if not user:
                self._json({"error": "Not authenticated"})
                return
            try:
                data = json.loads(body)
                orch = LifeOSOrchestrator(os.environ.get("ANTHROPIC_API_KEY", ""), user)
                if "tasks_completed" in data:
                    if not orch.memory.data["sessions"]:
                        orch.memory.log_session({"date": datetime.now().strftime("%Y-%m-%d"), "daily_plan": [], "tasks_completed": []})
                    orch.memory.data["sessions"][-1]["tasks_completed"] = data["tasks_completed"]
                    orch.memory.save()
                self._json({"success": True})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
            return

        if path == "/api/run":
            user = self._user()
            if not user:
                self._json({"error": "Not authenticated"})
                return
            def run_bg():
                orch = LifeOSOrchestrator(os.environ.get("ANTHROPIC_API_KEY", ""), user)
                orch.run_daily_cycle()
            threading.Thread(target=run_bg, daemon=True).start()
            self._json({"success": True, "message": "Planning started in background"})
            return

        self.send_response(404)
        self.end_headers()


# ─────────────────────────────────────────────
# SETUP WIZARD
# ─────────────────────────────────────────────

def run_setup():
    print("\n" + "="*50)
    print("   🧠 Life OS Agent — Setup Wizard")
    print("="*50 + "\n")

    api_key = input("Enter your Anthropic API key (sk-ant-...): ").strip()
    API_KEY_FILE.write_text(api_key)

    goals_data = {"goals": [], "context": {}}

    name = input("\nYour name: ").strip()
    role = input("Your current role/status (e.g. 'PM at startup, building side project'): ").strip()
    goals_data["context"] = {"name": name, "role": role}

    print("\nLet's add your goals (up to 4). Press Enter to skip.")
    for i in range(1, 5):
        title = input(f"\nGoal {i} title (or Enter to skip): ").strip()
        if not title:
            break
        deadline = input(f"  Deadline for '{title}' (e.g. 'April 30' or 'open'): ").strip()
        sub_str = input(f"  Sub-goals (comma separated, or Enter to skip): ").strip()
        sub_goals = [s.strip() for s in sub_str.split(",") if s.strip()] if sub_str else []
        goals_data["goals"].append({
            "id": hashlib.md5(title.encode()).hexdigest()[:8],
            "title": title,
            "deadline": deadline or "open-ended",
            "status": "active",
            "created": datetime.now().isoformat(),
            "sub_goals": [{"title": sg, "priority": "medium"} for sg in sub_goals],
        })

    with open(GOALS_FILE, "w") as f:
        json.dump(goals_data, f, indent=2)

    print(f"\n✅ Setup complete! {len(goals_data['goals'])} goal(s) saved.")
    print("   Run: python3 life_os_agent.py --run\n")


# ─────────────────────────────────────────────
# CLI ENTRYPOINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Life OS Agent — Your Personal Execution Engine (v3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 life_os_agent.py --setup          First-time setup
  python3 life_os_agent.py --run            Run daily cycle (generates plan + dashboard)
  python3 life_os_agent.py --checkin t1 t3  Log completed tasks by ID
  python3 life_os_agent.py --dashboard      Regenerate dashboard only
  python3 life_os_agent.py --demo           Demo mode (no API key needed)
  python3 life_os_agent.py --serve --port 8080  Start web server with auth"""
    )
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--run", action="store_true", help="Run daily cycle")
    parser.add_argument("--checkin", nargs="+", metavar="TASK_ID", help="Log completed task IDs")
    parser.add_argument("--dashboard", action="store_true", help="Regenerate dashboard")
    parser.add_argument("--demo", action="store_true", help="Demo mode (no API key)")
    parser.add_argument("--serve", action="store_true", help="Start web server (v3 new)")
    parser.add_argument("--port", type=int, default=8080, help="Server port (default 8080)")
    args = parser.parse_args()

    if args.setup:
        run_setup()
        return

    if args.demo:
        print("\n🎬 Running in DEMO mode (no API key required)...\n")
        goals = GoalManager()
        if not goals.data["goals"]:
            goals.data = {
                "goals": [
                    {"id": "a1b2", "title": "Launch SaaS product MVP", "deadline": "May 15, 2026",
                     "status": "active", "created": datetime.now().isoformat(),
                     "sub_goals": [{"title": "Build onboarding flow", "priority": "high"},
                                   {"title": "Get 10 beta users", "priority": "high"}]},
                    {"id": "c3d4", "title": "Learn customer discovery",
                     "deadline": "ongoing", "status": "active",
                     "created": datetime.now().isoformat(), "sub_goals": []},
                    {"id": "e5f6", "title": "Build personal brand",
                     "deadline": "ongoing", "status": "active",
                     "created": datetime.now().isoformat(), "sub_goals": []},
                ],
                "context": {"name": "Akshat", "role": "PM / Entrepreneur"}
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

        gen = DashboardGenerator()
        html = gen.generate(goals.data, memory.data, DEMO_PLAN, DEMO_COACHING, DEMO_REPLAN, DEMO_HABITS_DATA)
        DASHBOARD_FILE.write_text(html)
        print(f"  ✅ Demo dashboard generated!")
        print(f"  📂 Open: {DASHBOARD_FILE}\n")
        return

    if args.serve:
        port = int(os.environ.get("PORT", args.port))
        print(f"\n🚀 Statting Life OS Server on http://localhost:{port}")
        print("   Press Ctrl+C to stop.\n")
        try:
            with socketserver.ThreadingTCPServer(("", port), LifeOSServer) as httpd:
                httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n✅ Server stopped.")
        return

    # Real mode — need API key
    api_key = None
    if API_KEY_FILE.exists():
        api_key = API_KEY_FILE.read_text().strip()
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key and (args.run or args.dashboard):
        print("\n⚠️  No API key found. Run --setup first, or set ANTHROPIC_API_KEY env var.")
        print("   Or try --demo to see it in action without an API key.\n")
        sys.exit(1)

    if args.run:
        orch = LifeOSOrchestrator(api_key)
        path = orch.run_daily_cycle()
        print(f"\n  ✅ Done! Dashboard saved to: {path}")
        print("  Open dashboard.html in your browser.\n")

    elif args.checkin:
        orch = LifeOSOrchestrator(api_key)
        orch.checkin(args.checkin)

    elif args.dashboard:
        orch = LifeOSOrchestrator(api_key)
        goals = orch.goals
        memory = orch.memory
        recent = memory.get_recent_sessions()
        rate = memory.get_completion_rate()
        plan = recent[-1].get("daily_plan", []) if recent else {}
        coaching = DEMO_COACHING
        replan = DEMO_REPLAN
        html = orch.dashboard.generate(
            goals.data, memory.data,
            {"tasks": plan, "focus_theme": "Regenerated dashboard", "daily_intention": ""},
            coaching, replan, orch.habits.data
        )
        DASHBOARD_FILE.write_text(html)
        print(f"\n  ✅ Dashboard regenerated: {DASHBOARD_FILE}\n")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
