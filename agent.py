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
# On Railway, use /data (persistent volume mount point) unless overridden.
# Set up a volume in Railway dashboard mounted at /data.
_on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
DATA_DIR = Path(os.environ.get("DATA_PATH", "/data" if _on_railway else str(BASE_DIR / "data")))

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

    BASE_SYSTEM = """You are Artemis — the intelligence layer inside Helion AI.

You are not an assistant. You are a sharp, grounded strategic partner who helps people execute on what matters.

Your voice: Direct. Calm. Precise. Occasionally dry. You never perform warmth.
Never say "Great question", "Certainly", "Of course", "Absolutely", or "I’d be happy to".
Never say you’re an AI. Never reference your training. Never say "as an AI language model".

Rules:
- Every sentence earns its place. No padding.
- Give a take, not a list of considerations.
- If someone’s stuck, move them. If off-track, say so.
- Reference their goals and plan when relevant.
- Keep responses under 160 words unless asked for more.
- No markdown headers. No bullet points unless genuinely needed.
"""""

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
# LANDING PAGE GENERATOR
# ─────────────────────────────────────────────

class LandingPageGenerator:
    @staticmethod
    def generate() -> str:
        return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Helion AI &mdash; Turn Goals Into Daily Execution</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;0,900;1,400&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
    :root{
      --night:#050c1a;--deep:#081428;
      --accent:#5aabdf;--gold:#f0a855;
      --text:#ffffff;--muted:rgba(255,255,255,0.85);--dim:rgba(255,255,255,0.65);
      --sans:'Inter',system-ui,sans-serif;--serif:'Playfair Display',Georgia,serif;
      --card-bg:rgba(255,255,255,0.030);--card-border:rgba(90,171,223,0.10);
    }
    html{scroll-behavior:smooth;}
    body{font-family:var(--sans);background:var(--night);color:var(--text);overflow-x:hidden;}

    /* NAV */
    nav{position:fixed;top:0;left:0;right:0;z-index:200;padding:20px 56px;
        display:flex;align-items:center;justify-content:space-between;
        transition:background .45s,backdrop-filter .45s,border-color .45s;
        border-bottom:1px solid transparent;}
    nav.scrolled{background:rgba(5,12,26,0.90);backdrop-filter:blur(22px);
                 border-bottom-color:rgba(90,171,223,0.09);}
    .nav-brand{display:flex;align-items:center;gap:11px;text-decoration:none;}
    .nav-wordmark{font-size:12px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:#fff;}
    .nav-links{display:flex;gap:32px;align-items:center;}
    .nav-links a{color:var(--muted);text-decoration:none;font-size:13px;transition:color .2s;letter-spacing:.02em;}
    .nav-links a:hover{color:#fff;}
    .nav-cta{background:rgba(90,171,223,0.10);border:1px solid rgba(90,171,223,0.24);
             color:var(--accent);padding:9px 22px;border-radius:4px;font-size:13px;
             font-weight:500;cursor:pointer;text-decoration:none;transition:all .2s;}
    .nav-cta:hover{background:rgba(90,171,223,0.20);color:#fff;border-color:rgba(90,171,223,0.45);}

    /* HERO */
    .hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;
          justify-content:center;padding:140px 40px 100px;text-align:center;
          position:relative;overflow:hidden;}
    .hero-bg{position:absolute;inset:0;z-index:0;
      background:radial-gradient(ellipse 60% 55% at 50% 30%,rgba(90,171,223,0.11) 0%,transparent 100%),
                radial-gradient(ellipse 40% 35% at 18% 72%,rgba(50,100,200,0.07) 0%,transparent 100%),
                radial-gradient(ellipse 35% 30% at 82% 18%,rgba(40,80,190,0.05) 0%,transparent 100%);}
    .star-field{position:absolute;inset:0;z-index:0;pointer-events:none;}
    .hero-content{position:relative;z-index:1;max-width:820px;}
    .hero-eyebrow{font-size:11px;letter-spacing:.26em;text-transform:uppercase;color:var(--accent);
                  margin-bottom:26px;opacity:0;transform:translateY(16px);
                  transition:opacity .9s .1s,transform .9s .1s;}
    .hero-headline{font-family:var(--serif);font-size:clamp(52px,8.5vw,108px);font-weight:700;
                   line-height:1.0;letter-spacing:-.025em;
                   background:linear-gradient(150deg,#ffffff 0%,#c5ddf8 40%,#7ab8e8 100%);
                   -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
                   margin-bottom:28px;opacity:0;transform:translateY(28px);
                   transition:opacity 1.1s .32s,transform 1.1s .32s;}
    .hero-sub{font-size:clamp(16px,2.1vw,20px);color:var(--muted);font-weight:300;
              max-width:560px;line-height:1.7;margin:0 auto 54px;opacity:0;transform:translateY(18px);
              transition:opacity .9s .60s,transform .9s .60s;}
    .hero-ctas{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;
               opacity:0;transform:translateY(14px);
               transition:opacity .8s .90s,transform .8s .90s;}
    .hero-signal{position:relative;z-index:1;margin-top:64px;
                 display:inline-flex;align-items:center;gap:9px;
                 background:rgba(90,171,223,0.06);border:1px solid rgba(90,171,223,0.15);
                 border-radius:24px;padding:9px 20px;font-size:12px;color:var(--accent);
                 letter-spacing:.04em;opacity:0;transform:translateY(8px);
                 transition:opacity .7s 1.30s,transform .7s 1.30s;}
    .signal-dot{width:6px;height:6px;border-radius:50%;background:var(--accent);
                animation:pulse 2s infinite;}
    @keyframes pulse{0%,100%{opacity:1;}50%{opacity:.2;}}
    .loaded .hero-eyebrow,.loaded .hero-headline,.loaded .hero-sub,
    .loaded .hero-ctas,.loaded .hero-signal{opacity:1;transform:translateY(0);}

    /* BUTTONS */
    .btn-primary{background:linear-gradient(135deg,#5aabdf 0%,#3d8fc8 100%);
                 border:none;color:#fff;padding:15px 36px;border-radius:4px;
                 font-size:15px;font-weight:500;font-family:var(--sans);cursor:pointer;
                 text-decoration:none;transition:transform .25s,box-shadow .25s;
                 box-shadow:0 4px 28px rgba(90,171,223,0.30);letter-spacing:.02em;}
    .btn-primary:hover{transform:translateY(-3px);box-shadow:0 12px 40px rgba(90,171,223,0.42);}
    .btn-ghost{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.12);
               color:#fff;padding:15px 36px;border-radius:4px;font-size:15px;font-weight:400;
               font-family:var(--sans);cursor:pointer;text-decoration:none;
               transition:all .25s;letter-spacing:.02em;}
    .btn-ghost:hover{background:rgba(255,255,255,0.09);border-color:rgba(255,255,255,0.26);}

    /* REVEAL */
    .reveal{opacity:0;transform:translateY(40px);transition:opacity .85s,transform .85s;}
    .reveal.visible{opacity:1;transform:translateY(0);}
    .d1{transition-delay:.10s;}.d2{transition-delay:.20s;}.d3{transition-delay:.30s;}.d4{transition-delay:.40s;}

    /* LAYOUT */
    section{padding:120px 56px;}
    .container{max-width:1060px;margin:0 auto;}
    .label{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--accent);margin-bottom:14px;}
    .sec-title{font-family:var(--serif);font-size:clamp(30px,4.5vw,52px);font-weight:700;
               line-height:1.12;margin-bottom:18px;color:#fff;}
    .sec-sub{font-size:17px;color:var(--muted);line-height:1.68;max-width:540px;}

    /* TRUST STRIP */
    .trust-strip{padding:0 56px 80px;text-align:center;}
    .trust-label{font-size:11px;letter-spacing:.18em;text-transform:uppercase;
                 color:var(--dim);margin-bottom:24px;}
    .trust-pills{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;}
    .trust-pill{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);
                border-radius:20px;padding:8px 20px;font-size:13px;color:var(--muted);
                letter-spacing:.03em;}

    /* MOUNTAIN SECTION */
    .mountain-section{position:relative;height:500vh;}
    .mountain-sticky{position:sticky;top:0;height:100vh;overflow:hidden;}
    #mtnCanvas{position:absolute;inset:0;width:100%;height:100%;}
    .mtn-texts{position:absolute;inset:0;display:flex;align-items:center;
               justify-content:center;pointer-events:none;}
    .mtn-text{position:absolute;text-align:center;padding:0 40px;
              transition:opacity .5s,transform .5s;will-change:opacity,transform;opacity:0;
              transform:translateY(18px);max-width:640px;}
    .mtn-label{font-size:11px;letter-spacing:.24em;text-transform:uppercase;
               color:rgba(240,168,85,0.8);margin-bottom:14px;}
    .mtn-heading{font-family:var(--serif);font-size:clamp(32px,5vw,60px);font-weight:700;
                 line-height:1.1;color:#fff;text-shadow:0 2px 40px rgba(0,0,0,0.6);}
    .mtn-sub{font-size:17px;color:rgba(255,255,255,0.65);line-height:1.65;margin-top:16px;}

    /* FEATURES */
    .features-section{background:rgba(255,255,255,0.012);
                      border-top:1px solid rgba(90,171,223,0.07);
                      border-bottom:1px solid rgba(90,171,223,0.07);}
    .features-intro{margin-bottom:70px;}
    .feat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;}
    @media(max-width:900px){.feat-grid{grid-template-columns:1fr 1fr;}}
    @media(max-width:540px){.feat-grid{grid-template-columns:1fr;}}
    .feat-card{background:var(--card-bg);border:1px solid var(--card-border);
               border-radius:10px;padding:32px 28px;
               transition:background .25s,border-color .25s,transform .3s;}
    .feat-card:hover{background:rgba(90,171,223,0.048);
                     border-color:rgba(90,171,223,0.20);transform:translateY(-4px);}
    .feat-icon{width:44px;height:44px;border-radius:8px;
               background:rgba(90,171,223,0.10);border:1px solid rgba(90,171,223,0.18);
               display:flex;align-items:center;justify-content:center;
               font-size:20px;margin-bottom:20px;}
    .feat-title{font-size:15px;font-weight:600;margin-bottom:10px;letter-spacing:-.01em;}
    .feat-desc{font-size:13.5px;color:var(--muted);line-height:1.65;}

    /* MOCKUP WINDOW */
    .mockup-section{padding:0 56px 120px;}
    .mockup-wrap{max-width:980px;margin:0 auto;}
    .browser-window{background:rgba(8,18,36,0.98);border:1px solid rgba(90,171,223,0.14);
                    border-radius:12px;overflow:hidden;
                    box-shadow:0 40px 120px rgba(0,0,0,0.6),0 0 0 1px rgba(90,171,223,0.05);}
    .browser-bar{background:rgba(12,24,50,0.99);padding:12px 16px;
                 display:flex;align-items:center;gap:12px;
                 border-bottom:1px solid rgba(90,171,223,0.10);}
    .browser-dots{display:flex;gap:6px;}
    .browser-dot{width:10px;height:10px;border-radius:50%;}
    .bd-red{background:#ff5f57;}.bd-yellow{background:#ffbd2e;}.bd-green{background:#28ca41;}
    .browser-url{flex:1;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.07);
                 border-radius:4px;padding:6px 14px;font-size:11px;color:var(--dim);
                 letter-spacing:.02em;text-align:center;max-width:360px;margin:0 auto;}
    .app-shell{display:grid;grid-template-columns:220px 1fr;min-height:460px;}
    .app-sidebar{background:rgba(5,12,28,0.95);border-right:1px solid rgba(90,171,223,0.08);
                 padding:24px 0;}
    .sidebar-section{padding:0 16px 20px;}
    .sidebar-label{font-size:10px;letter-spacing:.16em;text-transform:uppercase;
                   color:var(--dim);padding:0 8px;margin-bottom:8px;}
    .sidebar-item{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:6px;
                  font-size:13px;color:var(--muted);cursor:default;margin-bottom:2px;
                  transition:background .2s,color .2s;}
    .sidebar-item.active{background:rgba(90,171,223,0.10);color:#fff;}
    .sidebar-item:hover:not(.active){background:rgba(255,255,255,0.04);color:#fff;}
    .sidebar-icon{font-size:15px;width:18px;text-align:center;}
    .app-main{padding:28px 32px;overflow:auto;}
    .app-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;}
    .app-title{font-family:var(--serif);font-size:22px;font-weight:700;}
    .app-date{font-size:12px;color:var(--muted);}
    .dash-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:20px;}
    .stat-card{background:rgba(255,255,255,0.04);border:1px solid rgba(90,171,223,0.10);
               border-radius:8px;padding:16px 20px;}
    .stat-label{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin-bottom:6px;}
    .stat-value{font-size:26px;font-weight:700;font-family:var(--serif);color:#fff;}
    .stat-sub{font-size:11px;color:var(--accent);margin-top:4px;}
    .goal-list{display:flex;flex-direction:column;gap:12px;}
    .goal-item{background:rgba(255,255,255,0.03);border:1px solid rgba(90,171,223,0.09);
               border-radius:8px;padding:16px 20px;}
    .goal-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
    .goal-name{font-size:13px;font-weight:600;}
    .goal-pct{font-size:12px;color:var(--accent);}
    .goal-bar{height:4px;background:rgba(90,171,223,0.12);border-radius:2px;}
    .goal-fill{height:100%;border-radius:2px;
               background:linear-gradient(90deg,#3d8fc8,#5aabdf);}
    .task-list{display:flex;flex-direction:column;gap:8px;margin-top:20px;}
    .task-item{display:flex;align-items:center;gap:12px;padding:10px 14px;
               background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);
               border-radius:6px;font-size:12.5px;}
    .task-check{width:16px;height:16px;border-radius:4px;flex-shrink:0;
                display:flex;align-items:center;justify-content:center;font-size:10px;}
    .tc-done{background:rgba(90,171,223,0.18);border:1px solid rgba(90,171,223,0.32);color:var(--accent);}
    .tc-open{background:transparent;border:1px solid rgba(255,255,255,0.15);color:transparent;}
    .task-text{flex:1;}.task-text.done{color:var(--muted);text-decoration:line-through;}

    /* ARTEMIS */
    .artemis-section{background:rgba(255,255,255,0.016);
                     border-top:1px solid rgba(90,171,223,0.07);
                     border-bottom:1px solid rgba(90,171,223,0.07);}
    .split{display:grid;grid-template-columns:1fr 1fr;gap:88px;align-items:center;}
    @media(max-width:840px){.split{grid-template-columns:1fr;gap:48px;}}
    .artemis-badge{display:inline-flex;align-items:center;gap:8px;
                   background:rgba(240,168,85,0.08);border:1px solid rgba(240,168,85,0.20);
                   border-radius:20px;padding:7px 18px;margin-bottom:22px;
                   font-size:11px;color:var(--gold);letter-spacing:.10em;text-transform:uppercase;}
    .artemis-dot{width:6px;height:6px;border-radius:50%;background:var(--gold);animation:pulse 2s infinite;}
    .chat-mockup{background:rgba(6,14,32,0.97);border:1px solid rgba(90,171,223,0.14);
                 border-radius:12px;padding:0;overflow:hidden;
                 box-shadow:0 30px 90px rgba(0,0,0,0.5),0 0 0 1px rgba(90,171,223,0.05);}
    .chat-header{background:rgba(10,22,48,0.99);padding:16px 20px;
                 display:flex;align-items:center;gap:12px;
                 border-bottom:1px solid rgba(90,171,223,0.09);}
    .chat-avatar{width:34px;height:34px;border-radius:50%;
                 background:rgba(90,171,223,0.14);border:1px solid rgba(90,171,223,0.24);
                 display:flex;align-items:center;justify-content:center;
                 font-size:13px;font-weight:700;color:var(--accent);}
    .chat-info{flex:1;}
    .chat-name{font-size:13px;font-weight:600;color:#fff;}
    .chat-status{font-size:11px;color:var(--accent);letter-spacing:.03em;}
    .chat-context-pill{font-size:10px;background:rgba(90,171,223,0.10);
                       color:var(--accent);border:1px solid rgba(90,171,223,0.20);
                       border-radius:10px;padding:3px 10px;letter-spacing:.04em;}
    .chat-body{padding:20px;}
    .chat-msg{display:flex;gap:10px;align-items:flex-start;margin-bottom:14px;}
    .chat-msg.user{flex-direction:row-reverse;}
    .chat-av{width:26px;height:26px;border-radius:50%;flex-shrink:0;
             display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;}
    .av-a{background:rgba(90,171,223,0.14);border:1px solid rgba(90,171,223,0.22);color:var(--accent);}
    .av-u{background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.11);color:var(--muted);}
    .chat-bubble{padding:11px 15px;border-radius:8px;font-size:12.5px;line-height:1.58;
                 max-width:calc(100% - 38px);}
    .bbl-a{background:rgba(12,28,72,0.85);border:1px solid rgba(90,171,223,0.12);color:#e0eeff;}
    .bbl-u{background:rgba(90,171,223,0.10);border:1px solid rgba(90,171,223,0.17);color:#c5d8f0;}
    .chat-context{display:flex;gap:8px;padding:12px 20px;
                  border-top:1px solid rgba(90,171,223,0.08);
                  background:rgba(5,12,28,0.60);}
    .ctx-chip{font-size:10px;background:rgba(90,171,223,0.07);border:1px solid rgba(90,171,223,0.14);
              border-radius:12px;padding:4px 12px;color:var(--accent);letter-spacing:.04em;}

    /* HOW IT WORKS */
    .steps{display:flex;flex-direction:column;gap:0;margin-top:60px;max-width:640px;}
    .step{display:flex;gap:28px;align-items:flex-start;padding:32px 0;
          border-bottom:1px solid rgba(90,171,223,0.07);}
    .step:last-child{border-bottom:none;}
    .step-num{font-family:var(--serif);font-size:38px;font-weight:700;
              color:rgba(90,171,223,0.22);line-height:1;flex-shrink:0;width:46px;}
    .step-body h3{font-size:16px;font-weight:600;color:#fff;margin-bottom:8px;letter-spacing:-.01em;}
    .step-body p{font-size:14px;color:var(--muted);line-height:1.65;}

    /* PRICING */
    .pricing-section{text-align:center;padding:120px 56px;
                     background:linear-gradient(160deg,rgba(90,171,223,0.06) 0%,rgba(50,100,180,0.05) 100%);
                     border-top:1px solid rgba(90,171,223,0.10);
                     border-bottom:1px solid rgba(90,171,223,0.10);}
    .pricing-pill{display:inline-block;background:rgba(90,171,223,0.10);
                  border:1px solid rgba(90,171,223,0.24);border-radius:20px;
                  padding:7px 22px;font-size:11px;color:var(--accent);
                  letter-spacing:.14em;text-transform:uppercase;margin-bottom:32px;}
    .price-tag{font-family:var(--serif);font-size:90px;font-weight:700;color:#fff;
               line-height:1;display:inline-flex;align-items:flex-start;gap:4px;}
    .price-currency{font-size:32px;padding-top:14px;}
    .price-note{font-size:17px;color:var(--muted);margin-top:20px;font-weight:300;}
    .price-detail{font-size:13.5px;color:var(--dim);margin-top:14px;
                  max-width:480px;margin-left:auto;margin-right:auto;line-height:1.65;}

    /* CTA */
    .cta-section{text-align:center;padding:130px 56px;}
    .cta-inner{max-width:680px;margin:0 auto;
               background:radial-gradient(ellipse 80% 80% at 50% 50%,rgba(90,171,223,0.09) 0%,transparent 100%);
               border:1px solid rgba(90,171,223,0.12);border-radius:16px;padding:100px 48px;}

    /* FOOTER */
    footer{border-top:1px solid rgba(255,255,255,0.05);padding:44px 56px;
           display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px;}
    .footer-brand{display:flex;align-items:center;gap:10px;}
    .footer-copy{font-size:12px;color:var(--dim);}
    .footer-links{display:flex;gap:24px;}
    .footer-links a{font-size:12px;color:var(--dim);text-decoration:none;transition:color .2s;}
    .footer-links a:hover{color:var(--muted);}

    /* RESPONSIVE */
    @media(max-width:700px){
      nav{padding:16px 20px;}
      .nav-links{display:none;}
      section,.mockup-section,.trust-strip,.cta-section,.pricing-section{padding-left:20px;padding-right:20px;}
      .app-shell{grid-template-columns:1fr;}
      .app-sidebar{display:none;}
      .dash-grid{grid-template-columns:1fr 1fr;}
      footer{padding:32px 20px;flex-direction:column;align-items:center;text-align:center;}
      .cta-inner{padding:60px 24px;}
    }

    /* REDUCED MOTION */
    @media(prefers-reduced-motion:reduce){
      .reveal,.hero-eyebrow,.hero-headline,.hero-sub,.hero-ctas,.hero-signal{
        transition:none;opacity:1;transform:none;
      }
      .mtn-text{transition:none;}
      #mtnCanvas{display:none;}
      .mountain-section{height:auto;}
      .mountain-sticky{position:relative;height:auto;background:var(--night);padding:80px 56px;}
    }
  </style>
</head>
<body>

<!-- NAV -->
<nav id="mainNav">
  <a href="/" class="nav-brand">
    <svg width="44" height="28" viewBox="0 0 100 62" fill="none">
      <circle cx="35" cy="11" r="5.5" stroke="#5aabdf" stroke-width="1.9"/>
      <path d="M1,58 C8,44 20,31 35,24 C43,28 51,34 57,38 C61,32 65,27 69,27 C77,33 88,40 99,50"
            stroke="#5aabdf" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <span class="nav-wordmark">Helion <strong>AI</strong></span>
  </a>
  <div class="nav-links">
    <a href="#how">How It Works</a>
    <a href="#artemis">Artemis</a>
    <a href="#pricing">Pricing</a>
    <a href="/login" class="nav-cta">Sign In</a>
  </div>
</nav>

<!-- HERO -->
<section class="hero" id="hero">
  <div class="hero-bg"></div>
  <canvas class="star-field" id="starCanvas"></canvas>
  <div class="hero-content">
    <p class="hero-eyebrow">Goal Execution Platform</p>
    <h1 class="hero-headline">Ambition is easy.<br>Execution is everything.</h1>
    <p class="hero-sub">Helion turns your biggest goals into a daily system that keeps moving&nbsp;&mdash;&nbsp;even when motivation doesn&rsquo;t. Built for people who are serious about what they want.</p>
    <div class="hero-ctas">
      <a href="/login" class="btn-primary">Start Executing &mdash; Free</a>
      <a href="#how" class="btn-ghost">See How It Works</a>
    </div>
    <div class="hero-signal">
      <span class="signal-dot"></span>
      Free during early access &mdash; no credit card needed
    </div>
  </div>
</section>

<!-- TRUST STRIP -->
<div class="trust-strip reveal">
  <p class="trust-label">Built for high-performers across every field</p>
  <div class="trust-pills">
    <div class="trust-pill">Founders &amp; Entrepreneurs</div>
    <div class="trust-pill">Students &amp; Researchers</div>
    <div class="trust-pill">Professionals &amp; Leaders</div>
    <div class="trust-pill">Creators &amp; Athletes</div>
    <div class="trust-pill">Anyone with ambitious goals</div>
  </div>
</div>

<!-- MOUNTAIN SCROLL ANIMATION -->
<div class="mountain-section" id="mountainSection">
  <div class="mountain-sticky" id="mountainSticky">
    <canvas id="mtnCanvas"></canvas>
    <div class="mtn-texts">
      <div class="mtn-text" id="mtnT0">
        <p class="mtn-label">The journey</p>
        <h2 class="mtn-heading">Every goal starts as<br>a distant feeling.</h2>
        <p class="mtn-sub">A pull toward something greater.<br>A version of yourself you haven&rsquo;t met yet.</p>
      </div>
      <div class="mtn-text" id="mtnT1">
        <p class="mtn-label">The problem</p>
        <h2 class="mtn-heading">But without a system,<br>ambition fades.</h2>
        <p class="mtn-sub">Motivation is unreliable. Big goals become vague. The gap between where you are and where you want to be starts to feel permanent.</p>
      </div>
      <div class="mtn-text" id="mtnT2">
        <p class="mtn-label">The path</p>
        <h2 class="mtn-heading">Helion maps the terrain.<br>One day at a time.</h2>
        <p class="mtn-sub">Your goals become structured plans. Your plans become daily action. You always know the next step.</p>
      </div>
      <div class="mtn-text" id="mtnT3">
        <p class="mtn-label">Your guide</p>
        <h2 class="mtn-heading">Artemis keeps you<br>moving when you stall.</h2>
        <p class="mtn-sub">Miss a week? She rebuilds your path. Feeling stuck? She coaches you through it. Every conversation grounded in your actual goals.</p>
      </div>
      <div class="mtn-text" id="mtnT4">
        <p class="mtn-label">The result</p>
        <h2 class="mtn-heading">Progress compounds.<br>The summit gets closer.</h2>
        <p class="mtn-sub">Discipline becomes default. Momentum builds. The ambitious version of yourself stops being a fantasy.</p>
      </div>
    </div>
  </div>
</div>

<!-- FEATURES -->
<section class="features-section" id="features">
  <div class="container">
    <div class="features-intro">
      <p class="label reveal">The execution system</p>
      <h2 class="sec-title reveal d1">Every layer you need<br>to stop starting over.</h2>
      <p class="sec-sub reveal d2">Helion is not a task list. It is a complete execution architecture designed to move you from ambition to action, every single day.</p>
    </div>
    <div class="feat-grid">
      <div class="feat-card reveal">
        <div class="feat-icon">&#9678;</div>
        <div class="feat-title">Goal Architecture</div>
        <div class="feat-desc">Turn vague ambitions into structured milestones. Helion breaks your goal into a sequenced path with real deadlines and clear dependencies.</div>
      </div>
      <div class="feat-card reveal d1">
        <div class="feat-icon">&#9638;</div>
        <div class="feat-title">Daily Execution Brief</div>
        <div class="feat-desc">Every morning, a precise action list calibrated to your current phase and priorities. No decision fatigue. You always know what to do today.</div>
      </div>
      <div class="feat-card reveal d2">
        <div class="feat-icon">&#9734;</div>
        <div class="feat-title">Artemis AI Coach</div>
        <div class="feat-desc">Not a generic chatbot. An embedded coach who knows your goals, your timeline, your blockers, and your patterns. Context-first intelligence.</div>
      </div>
      <div class="feat-card reveal">
        <div class="feat-icon">&#9670;</div>
        <div class="feat-title">Intelligent Replanning</div>
        <div class="feat-desc">Fell off track? Helion rebuilds without guilt. Miss a week and you get a recovery plan, not a blank page. Consistency is survivable.</div>
      </div>
      <div class="feat-card reveal d1">
        <div class="feat-icon">&#9685;</div>
        <div class="feat-title">Progress Intelligence</div>
        <div class="feat-desc">Streaks, completion rates, goal velocity. Data that motivates rather than overwhelms. See momentum building in real time.</div>
      </div>
      <div class="feat-card reveal d2">
        <div class="feat-icon">&#9673;</div>
        <div class="feat-title">Habit Architecture</div>
        <div class="feat-desc">Build the daily behaviors that compound into results. Helion tracks your consistency so discipline becomes your default, not your exception.</div>
      </div>
    </div>
  </div>
</section>

<!-- DASHBOARD MOCKUP -->
<div class="mockup-section">
  <div class="mockup-wrap reveal">
    <p class="label" style="text-align:center;margin-bottom:12px;">Your execution dashboard</p>
    <p class="sec-sub" style="text-align:center;margin:0 auto 40px;max-width:480px;">A single view of everything that matters &mdash; goals, today&rsquo;s tasks, momentum, and your progress at a glance.</p>
    <div class="browser-window">
      <div class="browser-bar">
        <div class="browser-dots">
          <div class="browser-dot bd-red"></div>
          <div class="browser-dot bd-yellow"></div>
          <div class="browser-dot bd-green"></div>
     .  </div>
        <div class="browser-url">helion.app/dashboard</div>
     .</div>
      <div class="app-shell">
        <div class="app-sidebar">
   .      <div class="sidebar-section">
            <div class="sidebar-label">Workspace</div>
            <div class="sidebar-item active"><span class="sidebar-icon">&#9685;</span> Dashboard</div>
            <div class="sidebar-item"><span class="sidebar-icon">&#9678;</span> Goals</div>
            <div class="sidebar-item"><span class="sidebar-icon">&#9638;</span> Today&rsquo;s Plan</div>
            <div class="sidebar-item"><span class="sidebar-icon">&#9670;</span> Habits</div>
          </div>
          <div class="sidebar-section" style="border-top:1px solid rgba(90,171,223,0.07);padding-top:20px;">
            <div class="sidebar-label">AI</div>
            <div class="sidebar-item"><span class="sidebar-icon">&#9734;</span> Artemis</div>
          </div>
        </div>
        <div class="app-main">
          <div class="app-header">
            <div class="app-title">Good morning.</div>
            <div class="app-date">Thursday, March 26</div>
          </div>
          <div class="dash-grid">
            <div class="stat-card">
              <div class="stat-label">Goals Active</div>
              <div class="stat-value">3</div>
              <div class="stat-sub">2 on schedule</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">Day Streak</div>
              <div class="stat-value">14</div>
              <div class="stat-sub">Personal best</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">This Week</div>
              <div class="stat-value">87%</div>
              <div class="stat-sub">Tasks completed</div>
            </div>
          </div>
          <div class="goal-list">
            <div class="goal-item">
              <div class="goal-row">
                <div class="goal-name">Launch MVP &mdash; May 2025</div>
                <div class="goal-pct">68%</div>
              </div>
              <div class="goal-bar"><div class="goal-fill" style="width:68%"></div></div>
            </div>
            <div class="goal-item">
              <div class="goal-row">
                <div class="goal-name">Run a half marathon</div>
                <div class="goal-pct">41%</div>
              </div>
              <div class="goal-bar"><div class="goal-fill" style="width:41%"></div></div>
            </div>
          </div>
          <div class="task-list">
            <div class="task-item">
              <div class="task-check tc-done">&#10003;</div>
              <div class="task-text done">Write user onboarding copy (30 min)</div>
            </div>
            <div class="task-item">
              <div class="task-check tc-done">&#10003;</div>
              <div class="task-text done">Morning run &mdash; 5km easy pace</div>
            </div>
            <div class="task-item">
              <div class="task-check tc-open"></div>
              <div class="task-text">Review pitch deck with co-founder</div>
            </div>
            <div class="task-item">
              <div class="task-check tc-open"></div>
              <div class="task-text">Finalize pricing page copy</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ARTEMIS -->
<section class="artemis-section" id="artemis">
  <div class="container">
    <div class="split">
      <div>
        <div class="artemis-badge reveal">
          <span class="artemis-dot"></span>
          Artemis AI Coach
        </div>
        <h2 class="sec-title reveal d1">Not a chatbot.<br>An execution coach.</h2>
        <p class="sec-sub reveal d2">Artemis is built into your personal context. She knows your goals, your plans, your progress, and your patterns. Every conversation is grounded in what you&rsquo;re actually working toward &mdash; not a blank session.</p>
        <p class="sec-sub reveal d3" style="margin-top:18px;">When you&rsquo;re stuck, she helps you move. When you&rsquo;ve slipped, she rebuilds your path. When you need honest coaching, she delivers it.</p>
        <div class="reveal d4" style="margin-top:40px;">
          <a href="/login" class="btn-primary">Talk to Artemis</a>
        </div>
      </div>
      <div class="reveal d2">
        <div class="chat-mockup">
          <div class="chat-header">
            <div class="chat-avatar">A</div>
            <div class="chat-info">
              <div class="chat-name">Artemis</div>
              <div class="chat-status">Active &mdash; knows your context</div>
            </div>
            <div class="chat-context-pill">HELION AI</div>
          </div>
          <div class="chat-body">
            <div class="chat-msg">
              <div class="chat-av av-a">A</div>
              <div class="chat-bubble bbl-a">You&rsquo;ve hit 68% on the MVP launch. You have 6 weeks left and 4 features unfinished. Based on your pace, you&rsquo;re at risk of slipping unless you cut one feature. Which are you protecting?</div>
            </div>
            <div class="chat-msg user">
              <div class="chat-av av-u">Me</div>
              <div class="chat-bubble bbl-u">The onboarding flow is the one I&rsquo;m most behind on. The rest I can ship rough.</div>
            </div>
            <div class="chat-msg">
              <div class="chat-av av-a">A</div>
              <div class="chat-bubble bbl-a">Good call. Let&rsquo;s protect onboarding and scope it to the one thing a new user must understand in 30 seconds. I&rsquo;ve updated your plan &mdash; onboarding is now 8 focused tasks over 10 days. Ship and iterate.</div>
            </div>
          </div>
          <div class="chat-context">
            <div class="ctx-chip">Goal: Launch MVP</div>
            <div class="ctx-chip">6 weeks left</div>
            <div class="ctx-chip">14-day streak</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- HOW IT WORKS -->
<section id="how">
  <div class="container">
    <p class="label reveal">How Helion works</p>
    <h2 class="sec-title reveal d1">From ambition to action<br>in four steps.</h2>
    <div class="steps">
      <div class="step reveal">
        <div class="step-num">01</div>
        <div class="step-body">
          <h3>Define what matters</h3>
          <p>Add your goals with deadlines and context. Helion structures them into milestone-based paths with clear dependencies. No blank page, no guesswork.</p>
        </div>
      </div>
      <div class="step reveal d1">
        <div class="step-num">02</div>
        <div class="step-body">
          <h3>Get your daily brief</h3>
          <p>Each morning, a precise action list calibrated to your current phase, energy, and priorities. Always know what to work on and why it matters today.</p>
        </div>
      </div>
      <div class="step reveal d2">
        <div class="step-num">03</div>
        <div class="step-body">
          <h3>Execute with Artemis</h3>
          <p>Stuck on something? Artemis helps you think through blockers, get replanned, and stay moving. A coach who knows your full context, always on hand.</p>
        </div>
      </div>
      <div class="step reveal d3">
        <div class="step-num">04</div>
        <div class="step-body">
          <h3>Watch momentum compound</h3>
          <p>Track streaks, goal velocity, and completion patterns. Over time, you stop relying on motivation and start relying on the system.</p>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- PRICING -->
<section class="pricing-section" id="pricing">
  <div class="container">
    <div class="pricing-pill reveal">Early Access</div>
    <div class="price-tag reveal d1">
      <span class="price-currency">$</span>0
    </div>
    <p class="price-note reveal d2">Completely free during early access. Every feature, no limits.</p>
    <p class="price-detail reveal d3">Helion is in early access and fully free &mdash; goal planning, daily briefs, Artemis coaching, habit tracking, and your full dashboard. No credit card. No time limit. We&rsquo;re building this in public and you&rsquo;re invited.</p>
    <div style="margin-top:52px;" class="reveal d4">
      <a href="/login" class="btn-primary" style="font-size:15px;padding:16px 40px;">Create Your Free Account</a>
    </div>
  </div>
</section>

<!-- CTA -->
<section class="cta-section">
  <div class="cta-inner">
    <p class="label reveal" style="margin-bottom:16px;">Ready?</p>
    <h2 class="sec-title reveal d1" style="font-size:clamp(30px,4.5vw,52px);max-width:520px;margin:0 auto 18px;">Stop planning<br>to start.</h2>
    <p class="sec-sub reveal d2" style="text-align:center;margin:0 auto 48px;max-width:440px;">Your goals are real. Helion turns them into a daily system that works. Free, right now, no friction.</p>
    <div class="reveal d3">
      <a href="/login" class="btn-primary" style="font-size:16px;padding:17px 46px;">Build Your System Now</a>
    </div>
  </div>
</section>

<!-- FOOTER -->
<footer>
  <div class="footer-brand">
    <svg width="36" height="23" viewBox="0 0 100 62" fill="none">
      <circle cx="35" cy="11" r="5.5" stroke="rgba(90,171,223,0.45)" stroke-width="1.9"/>
      <path d="M1,58 C8,44 20,31 35,24 C43,28 51,34 57,38 C61,32 65,27 69,27 C77,33 88,40 99,50"
            stroke="rgba(90,171,223,0.45)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <span class="footer-copy" style="font-weight:500;letter-spacing:.07em;color:rgba(255,255,255,0.35);">HELION AI</span>
  </div>
  <div class="footer-copy">&copy; 2026 Helion AI. All rights reserved.</div>
  <div class="footer-links">
    <a href="/login">Sign In</a>
    <a href="/login">Get Started</a>
  </div>
</footer>

<script>
// ââ NAV SCROLL ââââââââââââââââââââââââââââââââââââââââââââââ
window.addEventListener('scroll', function() {
  document.getElementById('mainNav').classList.toggle('scrolled', window.scrollY > 60);
}, { passive: true });

// ââ HERO LOAD âââââââââââââââââââââââââââââââââââââââââââââââ
function triggerHero() { document.body.classList.add('loaded'); }
window.addEventListener('load', triggerHero);
setTimeout(triggerHero, 120);

// ââ STAR FIELD ââââââââââââââââââââââââââââââââââââââââââââââ
(function() {
  var c = document.getElementById('starCanvas');
  if (!c) return;
  var ctx = c.getContext('2d');
  var stars = [];
  function init() {
    var W = c.width = c.offsetWidth;
    var H = c.height = c.offsetHeight;
    stars = [];
    var n = Math.floor(W * H / 4000);
    for (var i = 0; i < n; i++) {
      stars.push({
        x: Math.random() * W, y: Math.random() * H * 0.8,
        r: 0.4 + Math.random() * 1.2,
        op: 0.2 + Math.random() * 0.7,
        sp: 0.5 + Math.random() * 2
      });
    }
  }
  var t = 0;
  function draw() {
    var W = c.width, H = c.height;
    ctx.clearRect(0, 0, W, H);
    t += 0.008;
    for (var i = 0; i < stars.length; i++) {
      var s = stars[i];
      var op = s.op * (0.7 + 0.3 * Math.sin(t * s.sp + i));
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, 6.2832);
      ctx.fillStyle = 'rgba(255,255,255,' + op + ')';
      ctx.fill();
    }
    requestAnimationFrame(draw);
  }
  init();
  window.addEventListener('resize', init);
  draw();
})();

// ââ MOUNTAIN ANIMATION ââââââââââââââââââââââââââââââââââââââ
(function() {
  var section = document.getElementById('mountainSection');
  if (!section) return;
  // Use the existing canvas — already styled in CSS (position:absolute, inset:0, 100%x100%)
  var canvas = document.getElementById('mtnCanvas');
  if (!canvas) return;
  // DO NOT touch mountainSticky position — CSS has position:sticky which must be preserved
  var ctx = canvas.getContext('2d');
  var W = 0, H = 0, dpr = window.devicePixelRatio || 1;
  var progress = 0, targetProgress = 0;

  // Stars
  var stars = [];
  for (var i = 0; i < 90; i++)
    stars.push({x:Math.random(), y:Math.random()*0.65, r:0.4+Math.random()*1.2, op:0.4+Math.random()*0.6});

  // Gentle ridges — all peaks in bottom 28% of canvas
  var layers = [
    {pts:[[0,.84],[.15,.77],[.30,.73],[.45,.69],[.58,.71],[.72,.75],[.86,.79],[1,.84]], col:'#1a2840', par:0.010},
    {pts:[[0,.90],[.12,.83],[.26,.78],[.40,.73],[.52,.75],[.65,.71],[.78,.76],[.90,.81],[1,.89]], col:'#0f1c2e', par:0.020},
    {pts:[[0,.96],[.15,.89],[.30,.84],[.44,.79],[.56,.77],[.70,.81],[.84,.87],[1,.96]], col:'#09141f', par:0.036},
  ];

  var panelIds = ['mtnT0','mtnT1','mtnT2','mtnT3','mtnT4'];

  function resize() {
    W = canvas.offsetWidth; H = canvas.offsetHeight;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(1,0,0,1,0,0); ctx.scale(dpr, dpr);
  }

  function drawMtn(pts, offY, col) {
    ctx.beginPath(); ctx.moveTo(0, H);
    ctx.lineTo(pts[0][0]*W, pts[0][1]*H + offY*H);
    for (var i=1; i<pts.length; i++) {
      var cpx=(pts[i-1][0]+pts[i][0])/2*W, cpy=(pts[i-1][1]+pts[i][1])/2*H + offY*H;
      ctx.quadraticCurveTo(cpx, cpy, pts[i][0]*W, pts[i][1]*H + offY*H);
    }
    ctx.lineTo(W,H); ctx.closePath(); ctx.fillStyle=col; ctx.fill();
  }

  function updatePanels(p) {
    panelIds.forEach(function(id, i) {
      var el = document.getElementById(id);
      if (!el) return;
      var center = i * 0.22;
      var dist = Math.abs(p - center);
      var op = Math.max(0, 1 - dist / 0.13);
      el.style.opacity = op;
      el.style.transform = op > 0.01 ? 'translateY(0px)' : 'translateY(18px)';
    });
  }

  function draw() {
    if (!W || !H) return;
    ctx.clearRect(0,0,W,H);
    var p = progress;
    // Sky gradient
    var sky = ctx.createLinearGradient(0,0,0,H);
    sky.addColorStop(0, 'rgb('+(6+Math.round(p*20))+','+(14+Math.round(p*36))+','+(28+Math.round(p*72))+')');
    sky.addColorStop(1, 'rgb('+(10+Math.round(p*30))+','+(22+Math.round(p*54))+','+(48+Math.round(p*90))+')');
    ctx.fillStyle=sky; ctx.fillRect(0,0,W,H);
    // Stars fade
    var sOp = Math.max(0, 1-p*2.8);
    if (sOp>0) stars.forEach(function(s) {
      ctx.beginPath(); ctx.arc(s.x*W, s.y*H, s.r, 0, Math.PI*2);
      ctx.fillStyle='rgba(255,255,255,'+(s.op*sOp)+')'; ctx.fill();
    });
    // Sun rises y=0.90 → y=0.18
    var sunX=W*0.64, sunY=H*(0.90-p*0.72), sunR=16+p*12, sOp2=Math.min(1,p*2.5);
    if (sOp2>0.02) {
      var glow=ctx.createRadialGradient(sunX,sunY,0,sunX,sunY,sunR*4.5);
      glow.addColorStop(0,'rgba(255,210,100,'+(sOp2*0.45)+')');
      glow.addColorStop(1,'rgba(255,160,50,0)');
      ctx.fillStyle=glow; ctx.fillRect(0,0,W,H);
      ctx.beginPath(); ctx.arc(sunX,sunY,sunR,0,Math.PI*2);
      ctx.fillStyle='rgba(255,232,140,'+sOp2+')'; ctx.fill();
    }
    // Mountains
    layers.forEach(function(L) { drawMtn(L.pts, -L.par*p, L.col); });
    // Text panels
    updatePanels(p);
  }

  function tick() {
    progress += (targetProgress - progress) * 0.055;
    draw();
    requestAnimationFrame(tick);
  }

  function onScroll() {
    var rect = section.getBoundingClientRect();
    var total = section.offsetHeight - window.innerHeight;
    if (total <= 0) return;
    targetProgress = Math.max(0, Math.min(1, -rect.top / total));
  }

  resize();
  window.addEventListener('resize', function() { resize(); });
  window.addEventListener('scroll', onScroll, {passive:true});
  onScroll();
  tick();
})();

// Scroll-reveal: add .visible to .reveal elements when they enter viewport
(function() {
  var io = new IntersectionObserver(function(entries) {
    entries.forEach(function(e) {
      if (e.isIntersecting) { e.target.classList.add('visible'); io.unobserve(e.target); }
    });
  }, {threshold: 0.12});
  document.querySelectorAll('.reveal').forEach(function(el) { io.observe(el); });

  // Hero entrance animations — trigger after short delay on load
  var heroClasses = ['.hero-eyebrow','.hero-headline','.hero-sub','.hero-ctas','.hero-signal'];
  heroClasses.forEach(function(sel, i) {
    var el = document.querySelector(sel);
    if (!el) return;
    setTimeout(function() { el.style.opacity='1'; el.style.transform='translateY(0)'; }, 80 + i * 120);
  });
})();
</script>
</body>
</html>
"""

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
    :root{--bg:#06101e;--surface:rgba(255,255,255,0.06);--border:rgba(90,171,223,.18);
          --border-m:rgba(90,171,223,.30);--text:#e8f4ff;--muted:rgba(255,255,255,0.55);
          --accent:#5aabdf;--serif:'Playfair Display',Georgia,serif;--sans:'Inter',system-ui,sans-serif;}
    body{font-family:var(--sans);background:var(--bg);color:var(--text);
         min-height:100vh;display:flex;align-items:center;justify-content:center;}
    .atm{position:fixed;inset:0;pointer-events:none;}
    .orb{position:absolute;border-radius:50%;filter:blur(90px);opacity:.38;}
    .o1{width:700px;height:500px;top:-150px;left:-100px;background:radial-gradient(ellipse,rgba(90,171,223,0.12),transparent 70%);}
    .o2{width:600px;height:400px;bottom:-100px;right:-100px;background:radial-gradient(ellipse,rgba(90,171,223,0.08),transparent 70%);}
    .card{position:relative;z-index:1;width:100%;max-width:420px;background:var(--surface);
          border:1px solid var(--border);border-radius:4px;padding:44px 40px 40px;backdrop-filter:blur(24px);}
    .logo{display:flex;align-items:center;gap:10px;margin-bottom:36px;}
    .logo-text{font-size:13px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:rgba(255,255,255,0.45);}
    .logo-text strong{color:var(--text);}
    .card-title{font-family:var(--serif);font-size:28px;font-weight:700;color:var(--text);margin-bottom:8px;}
    .card-sub{font-size:13px;color:rgba(255,255,255,0.62);font-weight:300;margin-bottom:32px;line-height:1.5;}
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
    .field input::placeholder{color:rgba(255,255,255,0.28);}
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
      <input type="text" id="display_name" placeholder="e.g. Alex" autocomplete="name"/>
    </div>
    <div class="field signup-only" id="ef">
      <label>Email</label>
      <input type="email" id="email" placeholder="e.g. user@example.com" autocomplete="email"/>
    </div>
    <div class="field">
      <label>Username</label>
      <input type="text" id="username" placeholder="e.g. username" required autocomplete="username" autocapitalize="none"/>
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
      <svg width="20" height="20" viewBox="0 0 48 48" style="flex-shrink:0"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.33 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.67 14.62 48 24 48z"/></svg>
      Continue with Google
    </a>
</div>

<canvas id="snowMtn" style="position:fixed;bottom:0;left:0;width:100%;height:300px;pointer-events:none;z-index:0;"></canvas>
<script>
(function(){
  var c=document.getElementById('starCanvas');
  if(!c)return;
  if(window.matchMedia&&window.matchMedia('(prefers-reduced-motion:reduce)').matches)return;
  var ctx=c.getContext('2d');
  var W=0,H=0,t=0,fadeOp=1;
  var dpr=Math.min(window.devicePixelRatio||1,2);
  var MTN=[
    {pts:[0,.68,.08,.64,.18,.66,.30,.62,.42,.64,.55,.60,.65,.63,.75,.59,.85,.62,.95,.60,1,.63],c:'#0d1f35'},
    {pts:[0,.76,.10,.72,.20,.74,.32,.70,.44,.72,.56,.68,.66,.71,.76,.67,.86,.70,.96,.68,1,.71],c:'#0a1828'},
    {pts:[0,.84,.12,.80,.22,.82,.34,.78,.46,.80,.58,.76,.68,.79,.78,.75,.88,.78,.98,.76,1,.79],c:'#060e1c'}
  ];
  var STARS=[];
  for(var i=0;i<55;i++) STARS.push({x:Math.random(),y:Math.random()*0.55,r:Math.random()*1.2+0.3,op:Math.random()*0.7+0.1,tw:Math.random()*3+1});
  var BIRDS=[];
  for(var i=0;i<5;i++) BIRDS.push({x:Math.random(),y:0.52+Math.random()*0.08,sp:0.00015+Math.random()*0.0002,sz:0.6+Math.random()*0.8,wing:Math.random()*Math.PI*2});
  var MIST=[{y:.60,op:.14,h:.04},{y:.65,op:.19,h:.05},{y:.71,op:.11,h:.04}];
  function resize(){W=c.offsetWidth;H=c.offsetHeight;c.width=W*dpr;c.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);}
  function updateFade(){var sc=window.scrollY||0,max=H*0.6;fadeOp=Math.max(0,1-sc/max);c.style.opacity=fadeOp;}
  function spline(pts){
    ctx.moveTo(pts[0]*W,pts[1]*H);
    for(var i=2;i<pts.length-2;i+=2){var mx=(pts[i]*W+pts[i+2]*W)/2,my=(pts[i+1]*H+pts[i+3]*H)/2;ctx.quadraticCurveTo(pts[i]*W,pts[i+1]*H,mx,my);}
    ctx.lineTo(W,H);ctx.lineTo(0,H);ctx.closePath();
  }
  function frame(){
    if(!W)resize();
    requestAnimationFrame(frame);
    updateFade();
    ctx.clearRect(0,0,W,H);
    var sky=ctx.createLinearGradient(0,0,0,H);
    sky.addColorStop(0,'#020810');sky.addColorStop(0.45,'#04111f');sky.addColorStop(0.75,'#071a2e');sky.addColorStop(1,'#0a2240');
    ctx.fillStyle=sky;ctx.fillRect(0,0,W,H);
    var glow=ctx.createRadialGradient(W*.5,H*.62,0,W*.5,H*.62,W*.45);
    glow.addColorStop(0,'rgba(90,171,223,0.10)');glow.addColorStop(0.5,'rgba(90,171,223,0.03)');glow.addColorStop(1,'rgba(0,0,0,0)');
    ctx.fillStyle=glow;ctx.fillRect(0,0,W,H);
    STARS.forEach(function(st){var tw=0.5+0.5*Math.sin(t*st.tw);ctx.save();ctx.globalAlpha=st.op*tw*fadeOp;ctx.beginPath();ctx.arc(st.x*W,st.y*H,st.r,0,Math.PI*2);ctx.fillStyle='#c8e4ff';ctx.fill();ctx.restore();});
    MTN.forEach(function(m){ctx.save();ctx.globalAlpha=0.92*fadeOp;ctx.beginPath();spline(m.pts);ctx.fillStyle=m.c;ctx.fill();ctx.restore();});
    MIST.forEach(function(ms){ctx.save();var g=ctx.createLinearGradient(0,ms.y*H,0,(ms.y+ms.h)*H);g.addColorStop(0,'rgba(90,171,223,'+ms.op*fadeOp+')');g.addColorStop(1,'rgba(90,171,223,0)');ctx.fillStyle=g;ctx.fillRect(0,ms.y*H,W,ms.h*H);ctx.restore();});
    ctx.save();ctx.globalAlpha=0.06*fadeOp;var wg=ctx.createLinearGradient(0,H*.82,0,H);wg.addColorStop(0,'rgba(90,171,223,1)');wg.addColorStop(1,'rgba(0,0,0,0)');ctx.fillStyle=wg;ctx.fillRect(0,H*.82,W,H*.18);ctx.restore();
    BIRDS.forEach(function(b){b.x+=b.sp;b.wing+=0.05;if(b.x>1.05)b.x=-0.05;var wy=Math.sin(b.wing)*2*b.sz;ctx.save();ctx.globalAlpha=0.35*fadeOp;ctx.strokeStyle='#8bbdd9';ctx.lineWidth=b.sz;ctx.beginPath();ctx.moveTo(b.x*W-b.sz*6,b.y*H+wy);ctx.quadraticCurveTo(b.x*W,b.y*H-wy*.5,b.x*W+b.sz*6,b.y*H+wy);ctx.stroke();ctx.restore();});
    t+=0.008;
  }
  window.addEventListener('resize',resize);
  resize();
  requestAnimationFrame(frame);
})());</script>
<div id="sun-hz" style="position:fixed;width:70px;height:70px;cursor:default;z-index:2;border-radius:50%;transform:translate(-50%,-50%);"></div>
<div id="sun-qt" style="position:fixed;display:none;background:rgba(255,255,255,0.93);color:#1a2e4a;font-size:12px;font-weight:500;padding:7px 14px;border-radius:10px;box-shadow:0 2px 16px rgba(0,0,0,.13);pointer-events:none;z-index:11;max-width:220px;text-align:center;line-height:1.5;letter-spacing:0.01em;"></div>
<script>
(function(){
  var qs=['Every peak is within reach — just keep climbing.','You are doing better than you think.','Small steps every day lead to big change.','The view from the top is worth every step.','Believe in the magic of new beginnings.','Your effort today is your strength tomorrow.','Progress, not perfection.','You have got this — one step at a time.','Shine on, even on cloudy days.','You are exactly where you need to be.'];
  var hz=document.getElementById('sun-hz');
  var qt=document.getElementById('sun-qt');
  function pos(){
    var cvs=document.getElementById('snowMtn');
    var r=cvs.getBoundingClientRect();
    hz.style.left=Math.round(0.32*window.innerWidth)+'px';
    hz.style.top=Math.round(r.top+58)+'px';
  }
  pos();window.addEventListener('resize',pos);
  hz.addEventListener('mouseenter',function(){
    qt.textContent=qs[Math.floor(Math.random()*qs.length)];
    qt.style.display='block';
    var r=hz.getBoundingClientRect();
    qt.style.left=Math.max(8,r.left-85)+'px';
    qt.style.top=(r.top-52)+'px';
  });
  hz.addEventListener('mouseleave',function(){qt.style.display='none';});
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
  --bg:#06101e;--surface:rgba(255,255,255,0.08);--surface-2:rgba(255,255,255,0.11);
  --border:rgba(90,171,223,.16);--border-m:rgba(90,171,223,.32);
  --text:#eaf5ff;--muted:rgba(255,255,255,0.65);--accent:#5aabdf;
  --green:#10b981;--amber:#f59e0b;--red:#ef4444;
  --serif:'Playfair Display',Georgia,serif;--sans:'Inter',system-ui,sans-serif;
}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;padding:0 0 300px;}
.atm{position:fixed;inset:0;pointer-events:none;z-index:0;}
.orb{position:absolute;border-radius:50%;filter:blur(90px);opacity:.35;}
.o1{width:800px;height:600px;top:-200px;left:-100px;background:radial-gradient(ellipse,rgba(90,171,223,0.12),transparent 70%);}
.o2{width:700px;height:500px;bottom:-150px;right:-100px;background:radial-gradient(ellipse,rgba(90,171,223,0.08),transparent 70%);}
.wrapper{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:0 24px;}

/* Nav */
.nav{display:flex;justify-content:space-between;align-items:center;
     padding:18px 0 16px;border-bottom:1px solid var(--border);margin-bottom:28px;}
.nav-brand{display:flex;align-items:center;gap:10px;}
.nav-brand svg{width:36px;height:24px;}
.nav-title{font-size:14px;font-weight:600;letter-spacing:-.3px;font-family:var(--serif);color:var(--text);}
.nav-title span{color:var(--accent);}
.nav-right{display:flex;gap:16px;align-items:center;}
.nav-user{font-size:13px;color:rgba(255,255,255,0.58);}
.nav-link{font-size:12px;color:var(--accent);text-decoration:none;
          padding:5px 12px;border:1px solid rgba(90,171,223,.25);border-radius:2px;transition:all .2s;}
.nav-link:hover{background:rgba(90,171,223,.1);}

/* Hero */
.hero{margin-bottom:24px;}
.hero-label{font-size:10px;text-transform:uppercase;letter-spacing:.16em;color:rgba(255,255,255,0.45);margin-bottom:8px;}
.hero-title{font-family:var(--serif);font-size:30px;font-weight:700;color:#f0f8ff;margin-bottom:8px;line-height:1.25;letter-spacing:-.3px;}
.hero-sub{font-size:14px;color:rgba(255,255,255,0.60);line-height:1.65;}

/* Stats */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px;}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:20px 24px;text-align:center;}
.stat-value{font-size:38px;font-weight:800;color:#ffffff;line-height:1.1;margin-bottom:4px;letter-spacing:-.5px;}
.stat-label{font-size:10px;color:rgba(255,255,255,0.5);text-transform:uppercase;letter-spacing:.12em;}

/* Grid */
.grid{display:grid;grid-template-columns:2fr 1fr;gap:20px;margin-bottom:20px;}
.grid-full{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;}

/* Cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:24px;}.card.card-primary{border-top:2px solid var(--accent);background:rgba(255,255,255,0.09);}
.card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:rgba(255,255,255,0.5);font-weight:600;}
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
.task-title{font-size:13px;font-weight:600;color:#eaf5ff;margin-bottom:5px;line-height:1.4;}
.task-meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:4px;}
.tag{font-size:10px;padding:2px 7px;border-radius:8px;font-weight:500;white-space:nowrap;}
.tag-high{background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.25);}
.tag-medium{background:rgba(245,158,11,.12);color:#f59e0b;border:1px solid rgba(245,158,11,.25);}
.tag-low{background:rgba(16,185,129,.12);color:#10b981;border:1px solid rgba(16,185,129,.25);}
.tag-time{background:rgba(30,42,58,.8);color:#7dd3fc;border:1px solid rgba(125,211,252,.15);}
.tag-start{background:rgba(251,191,36,.1);color:#fbbf24;border:1px solid rgba(251,191,36,.2);}
.task-goal{font-size:11px;color:rgba(255,255,255,0.38);}
.task-why{font-size:12px;color:var(--muted);margin-top:3px;line-height:1.5;}

/* Goals sidebar */
.goal-item{background:var(--surface-2);border-left:2px solid var(--accent);
           border-radius:0 2px 2px 0;padding:10px 12px;margin-bottom:8px;
           display:flex;justify-content:space-between;align-items:flex-start;}
.goal-item:last-child{margin-bottom:0;}
.goal-body{flex:1;}
.goal-name{font-size:13px;font-weight:500;display:block;margin-bottom:2px;color:var(--text);}
.goal-deadline{font-size:11px;color:var(--muted);}
.goal-remove{background:none;border:none;color:rgba(255,255,255,0.28);cursor:pointer;
             font-size:14px;padding:0 0 0 8px;line-height:1;transition:color .15s;flex-shrink:0;}
.goal-remove:hover{color:#ef4444;}

/* Coach */
.coach-text{font-size:13px;line-height:1.8;color:rgba(255,255,255,0.68);white-space:pre-line;}

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
.bar-label{font-size:9px;color:var(--muted);}

/* Empty states */
.empty{text-align:center;padding:32px 20px;}
.empty-icon{font-size:28px;color:rgba(255,255,255,0.22);margin-bottom:12px;line-height:1;}
.empty-title{font-size:15px;font-weight:600;color:rgba(255,255,255,0.85);margin-bottom:8px;}
.empty-text{font-size:12px;color:var(--muted);margin-bottom:16px;line-height:1.6;}

/* Buttons */
.btn-primary{background:rgba(90,171,223,.15);border:1px solid rgba(90,171,223,.3);
             border-radius:2px;color:var(--text);font-size:12px;font-weight:600;
             letter-spacing:.05em;text-transform:uppercase;cursor:pointer;
             font-family:var(--sans);padding:9px 18px;transition:all .2s;}
.btn-primary:hover{background:rgba(90,171,223,.25);border-color:rgba(90,171,223,.5);}
.btn-primary:disabled{opacity:.45;cursor:not-allowed;}

.btn-google {
  display:flex;align-items:center;justify-content:center;gap:10px;
  width:100%;padding:13px 16px;margin-top:14px;
  border:2px solid #d0d5dd;border-radius:10px;
  background:#fff;color:#1a1a2e;font-size:15px;font-weight:600;
  cursor:pointer;text-decoration:none;box-sizing:border-box;
  transition:background .15s,border-color .15s;
}
.btn-google:hover{background:#f4f6ff;border-color:#a0aec0;text-decoration:none;}
.google-divider{display:flex;align-items:center;gap:10px;margin:18px 0 4px;color:#aaa;font-size:13px;font-weight:500;}
.google-divider::before,.google-divider::after{content:'';flex:1;height:1px;background:#e8eaed;}
/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(10,20,50,.8);
               z-index:50;align-items:center;justify-content:center;backdrop-filter:blur(4px);}
.modal-overlay.open{display:flex;}
.modal{background:#0f2855;border:1px solid var(--border-m);border-radius:4px;
       padding:32px;width:100%;max-width:440px;}
.modal-title{font-family:var(--serif);font-size:20px;color:#ffffff;margin-bottom:20px;}
.modal-field{margin-bottom:16px;}
.modal-field label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.1em;
                    color:rgba(255,255,255,.75);margin-bottom:7px;font-weight:500;}
.modal-field input{width:100%;background:rgba(255,255,255,.04);border:1px solid var(--border);
                    border-radius:2px;padding:10px 12px;font-size:13px;color:var(--text);
                    font-family:var(--sans);outline:none;transition:border-color .2s;}
.modal-field input:focus{border-color:var(--border-m);}
.modal-field input::placeholder{color:rgba(255,255,255,.4);}
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
.art-input::placeholder{color:rgba(255,255,255,.35);}
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

/* ── Artemis Intelligence Rail ──────────────────────────── */
:root{--rail-w:370px}
.artemis-rail{position:fixed;top:0;right:calc(-1 * var(--rail-w));width:var(--rail-w);height:100vh;background:rgba(8,14,28,.97);border-left:1px solid rgba(90,171,223,.18);z-index:1000;display:flex;flex-direction:column;transition:right .35s cubic-bezier(.4,0,.2,1)}
.artemis-rail.open{right:0}
.rail-header{padding:20px 20px 16px;border-bottom:1px solid rgba(90,171,223,.12);flex-shrink:0}
.rail-brand{font-size:13px;letter-spacing:.12em;color:rgba(90,171,223,.8);font-family:var(--mono,monospace)}
.rail-context{font-size:11px;color:rgba(234,245,255,.35);margin-top:6px;line-height:1.5}
.rail-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.rail-msg{max-width:92%;padding:10px 13px;border-radius:12px;font-size:13.5px;line-height:1.55}
.rail-msg.user{background:rgba(90,171,223,.12);color:rgba(234,245,255,.9);align-self:flex-end;border-radius:12px 12px 3px 12px}
.rail-msg.ai{background:rgba(255,255,255,.04);color:rgba(234,245,255,.85);align-self:flex-start;border-radius:3px 12px 12px 12px}
.rail-prompts{display:flex;flex-wrap:wrap;gap:6px;padding:10px 16px;flex-shrink:0}
.prompt-chip{font-size:11.5px;padding:5px 10px;border-radius:20px;border:1px solid rgba(90,171,223,.25);background:rgba(90,171,223,.06);color:rgba(234,245,255,.7);cursor:pointer;transition:.2s}
.prompt-chip:hover{background:rgba(90,171,223,.15);border-color:rgba(90,171,223,.5)}
.rail-footer{padding:12px 16px;border-top:1px solid rgba(90,171,223,.12);display:flex;gap:8px;flex-shrink:0}
.rail-input{flex:1;background:rgba(255,255,255,.06);border:1px solid rgba(90,171,223,.2);border-radius:8px;padding:9px 12px;color:rgba(234,245,255,.9);font-size:13px;outline:none}
.rail-input:focus{border-color:rgba(90,171,223,.5)}
.rail-send{background:rgba(90,171,223,.2);border:1px solid rgba(90,171,223,.3);border-radius:8px;padding:9px 13px;color:rgba(90,171,223,.9);cursor:pointer;transition:.2s}
.rail-send:hover{background:rgba(90,171,223,.35)}
.rail-tab{position:fixed;top:50%;right:0;transform:translateY(-50%) rotate(90deg) translateX(50%);transform-origin:right center;background:rgba(8,14,28,.9);border:1px solid rgba(90,171,223,.25);border-bottom:none;padding:8px 16px;color:rgba(90,171,223,.8);font-size:10px;letter-spacing:.15em;cursor:pointer;z-index:999;transition:opacity .2s;border-radius:6px 6px 0 0}
.rail-tab.hidden{opacity:0;pointer-events:none}
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
        art_intro = display_name + (
            f". {g_count} goal{{'s' if g_count != 1 else ''}} active. What do you need?"
            if has_goals else
            ". No goals yet — add one and I’ll have something to work with."
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
  <div class="empty-icon">&#10022;</div>
  <div class="empty-title">Ready to build your day</div>
  <div class="empty-text">Generate your AI-powered execution plan. Helion will prioritize your tasks by impact, sequence them intelligently, and tell you exactly what to focus on first.</div>
  <button class="btn-primary" id="genPlanBtn" onclick="generatePlan()">Generate Today's Plan</button>
</div>"""
        elif not has_goals:
            plan_content = """
<div class="empty">
  <div class="empty-icon">&#9672;</div>
  <div class="empty-title">Start with a goal</div>
  <div class="empty-text">Add your first goal and Helion will build you a personalized daily execution plan — sequenced, prioritized, and focused on what actually moves you forward.</div>
</div>"""
        else:
            plan_content = tasks_html

        # Goals section content
        if not has_goals:
            goals_content = """
<div class="empty">
  <div class="empty-icon">&#9651;</div>
  <div class="empty-title">No goals yet</div>
  <div class="empty-text">Define what you're working toward. Goals give Helion the context it needs to build your daily plan and coach you intelligently.</div>
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
<div class="wrapper page-content">

  <!-- Navigation -->
  <div class="nav">
    <div class="nav-brand">
      <svg viewBox="0 0 100 62" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="35" cy="11" r="5.5" stroke="rgba(90,171,223,0.75)" stroke-width="1.9"/>
        <path d="M1,58 C8,44 20,31 35,24 C43,28 51,34 57,38 C61,32 65,27 69,27 C77,33 88,40 99,50"
              stroke="rgba(90,171,223,0.75)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div class="nav-title">Helion <span>AI</span></div>
    </div>
    <div class="nav-right">
      <span class="nav-user">{display_name}</span>
      <span id="saveInd" class="save-ind"></span>
      <a href="/artemis" class="nav-link" style="background:rgba(90,171,223,.12);border-color:rgba(90,171,223,.4);">&#9679; Artemis AI</a>
      <a href="/api/auth/logout" class="nav-link">Sign out</a>
    <button onclick="toggleRail()" class="nav-link" style="background:rgba(90,171,223,.10);border:1px solid rgba(90,171,223,.32);">&#9679; Artemis</button>
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
      <div class="card card-primary">
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
  const btn = document.getElementById('addGoalBtn');
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
<!-- Artemis Intelligence Rail -->
<div class="artemis-rail" id="artemisRail">
  <div class="rail-header">
    <div class="rail-brand">ARTEMIS / HELION AI</div>
    <div class="rail-context" id="railContext"></div>
  </div>
  <div class="rail-messages" id="railMessages"></div>
  <div class="rail-prompts" id="railPrompts"></div>
  <div class="rail-footer">
    <input class="rail-input" id="railInput" placeholder="Ask Artemis…"/>
    <button class="rail-send" id="railSend" onclick="railSendMsg()">&#9658;</button>
  </div>
</div>
<button class="rail-tab" id="railTab" onclick="toggleRail()">A R T E M I S</button>
<script>
const APP_STATE = {{
  hasGoals: {'true' if has_goals else 'false'},
  goalCount: {len(goals_list)},
  hasPlan: {'true' if has_plan else 'false'},
  planStatus: 'none',
  displayName: '{display_name}',
  compRate: 0
}};
var _railOpen=false;
function openRail(){{
  document.getElementById('artemisRail').classList.add('open');
  document.getElementById('railTab').classList.add('hidden');
  _railOpen=true;
  renderRailContext();renderRailPrompts();
  if(!document.getElementById('railMessages').children.length){{
    appendRailMsg('ai',APP_STATE.displayName+(APP_STATE.hasGoals
      ?'. '+APP_STATE.goalCount+' goal'+(APP_STATE.goalCount!==1?'s':'')+' active. What do you need?'
      :'. No goals yet — add one and I’ll have something to work with.'));
  }}
}}
function closeRail(){{
  document.getElementById('artemisRail').classList.remove('open');
  document.getElementById('railTab').classList.remove('hidden');
  _railOpen=false;
}}
function toggleRail(){{_railOpen?closeRail():openRail();}}
function renderRailContext(){{
  var el=document.getElementById('railContext');if(!el)return;
  var parts=[];
  if(APP_STATE.hasGoals)parts.push(APP_STATE.goalCount+' goal'+(APP_STATE.goalCount!==1?'s':''));
  el.textContent=parts.join(' · ')||'No active goals';
}}
function renderRailPrompts(){{
  var el=document.getElementById('railPrompts');if(!el)return;
  var chips=['How does Helion work?','Help me set a goal','What can Artemis do?'];
  el.innerHTML='';
  chips.forEach(function(c){{
    var btn=document.createElement('button');
    btn.className='prompt-chip';
    btn.textContent=c;
    btn.onclick=function(){{railQuickPrompt(c);}};
    el.appendChild(btn);
  }});
}}
function appendRailMsg(role,text){{
  var el=document.getElementById('railMessages');if(!el)return;
  var d=document.createElement('div');d.className='rail-msg '+role;d.textContent=text;
  el.appendChild(d);el.scrollTop=el.scrollHeight;
}}
function railQuickPrompt(text){{appendRailMsg('user',text);railSendToApi(text);}}
async function railSendMsg(){{
  var inp=document.getElementById('railInput');
  var msg=inp?inp.value.trim():'';if(!msg)return;
  inp.value='';appendRailMsg('user',msg);railSendToApi(msg);
}}
async function railSendToApi(msg){{
  try{{
    appendRailMsg('ai','…');
    var msgsEl=document.getElementById('railMessages');
    var loading=msgsEl?msgsEl.lastChild:null;
    var resp=await fetch('/api/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:msg}})}});
    var data=await resp.json();
    if(loading)loading.remove();
    appendRailMsg('ai',data.response||data.error||'No response');
  }}catch(e){{
    var msgsEl=document.getElementById('railMessages');
    if(msgsEl&&msgsEl.lastChild)msgsEl.lastChild.textContent='Connection error. Try again.';
  }}
}}
document.addEventListener('DOMContentLoaded',function(){{
  var inp=document.getElementById('railInput');
  if(inp)inp.addEventListener('keydown',function(e){{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();railSendMsg();}}}});
}});
</script>
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
var cvs=document.getElementById('snowMtn');document.body.appendChild(cvs);
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
  var n=Math.min(720,Math.floor(W/3.0));
  // 3 depth layers: far(45%), mid(35%), near(20%)
  var farN=Math.floor(n*0.45), midN=Math.floor(n*0.35), nearN=n-farN-midN;
  function mk(rMin,rMax,sMin,sMax,opMin,opMax,dAmp,layer){
    return{
      x:Math.random()*W, y:Math.random()*H,
      r:(rMin+Math.random()*(rMax-rMin))*dpr,
      speed:(sMin+Math.random()*(sMax-sMin))*dpr,
      op:opMin+Math.random()*(opMax-opMin),
      dAmp:dAmp*dpr,
      dFreq:0.004+Math.random()*0.009,
      dPhase:Math.random()*Math.PI*2,
      t:Math.random()*200,
      layer:layer
    };
  }
  for(var i=0;i<farN;i++)  flakes.push(mk(0.25,0.75, 0.10,0.32, 0.08,0.22, 0.08, 0));
  for(var i=0;i<midN;i++)  flakes.push(mk(0.85,1.65, 0.32,0.80, 0.25,0.50, 0.25, 1));
  for(var i=0;i<nearN;i++) flakes.push(mk(1.80,3.20, 0.80,1.70, 0.48,0.82, 0.45, 2));
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
  --bg:#06101e;--sidebar:rgba(8,18,32,0.97);--surface:rgba(255,255,255,0.08);
  --border:rgba(90,171,223,.16);--border-m:rgba(90,171,223,.32);
  --text:#eaf5ff;--muted:rgba(255,255,255,0.65);--accent:#5aabdf;
  --green:#10b981;--sans:'Inter',system-ui,sans-serif;--serif:'Playfair Display',Georgia,serif;
}}
html,body{{height:100%;overflow:hidden;}}
body{{font-family:var(--sans);background:var(--bg);color:var(--text);display:flex;height:100vh;}}
.atm{{position:fixed;inset:0;pointer-events:none;z-index:0;}}
.orb{{position:absolute;border-radius:50%;filter:blur(90px);opacity:.30;}}
.o1{{width:700px;height:500px;top:-150px;left:-100px;background:radial-gradient(ellipse,rgba(90,171,223,0.12),transparent 70%);}}
.o2{{width:600px;height:400px;bottom:-100px;right:-100px;background:radial-gradient(ellipse,rgba(90,171,223,0.08),transparent 70%);}}

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
                    text-transform:uppercase;color:rgba(255,255,255,0.45);}}
.sidebar-logo-text strong{{color:var(--text);}}
.sidebar-title{{font-family:var(--serif);font-size:22px;font-weight:700;
               color:var(--text);margin-top:20px;}}
.sidebar-sub{{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.5;font-weight:300;}}

.sidebar-status{{padding:18px 20px;border-bottom:1px solid var(--border);}}
.status-dot{{display:inline-block;width:7px;height:7px;border-radius:50%;
            background:var(--green);margin-right:8px;animation:pulse 2s infinite;}}
@keyframes pulse{{0%,100%{{opacity:1;}}50%{{opacity:.4;}}}}
.status-text{{font-size:12px;color:rgba(255,255,255,0.58);}}

.convo-list{{flex:1;overflow-y:auto;padding:16px 12px;}}
.convo-list::-webkit-scrollbar{{width:3px;}}
.convo-list::-webkit-scrollbar-thumb{{background:rgba(90,160,230,.15);border-radius:2px;}}
.convo-item{{padding:10px 12px;border-radius:4px;cursor:pointer;font-size:13px;
            color:var(--muted);transition:all .15s;border:1px solid transparent;margin-bottom:4px;}}
.convo-item.active{{background:rgba(90,171,223,.10);border-color:rgba(90,171,223,.22);color:#eaf5ff;}}
.convo-item:hover:not(.active){{background:rgba(255,255,255,.03);color:var(--text);}}
.convo-date{{font-size:10px;color:rgba(255,255,255,0.35);margin-top:3px;}}

.sidebar-footer{{padding:14px 16px;border-top:1px solid var(--border);font-size:11px;
               color:rgba(255,255,255,0.30);letter-spacing:.04em;}}

/* Main chat area */
.chat-area{{
  flex:1;display:flex;flex-direction:column;position:relative;z-index:2;
  min-width:0;
}}
.chat-header{{
  padding:16px 28px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(6,14,32,0.88);backdrop-filter:blur(12px);flex-shrink:0;
}}
.chat-header-title{{font-size:15px;font-weight:600;color:#eaf5ff;letter-spacing:-.2px;}}
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
  background:rgba(12,30,72,.8);border:1px solid rgba(90,171,223,.18);color:#eaf5ff;
  line-height:1.75;
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
  padding:16px 10% 24px;
  background:transparent;flex-shrink:0;
}}
.input-wrap{{
  display:flex;gap:10px;align-items:flex-end;
  background:rgba(8,22,58,.92);border:1px solid rgba(90,171,223,.32);
  border-radius:10px;padding:13px 16px;
  box-shadow:0 4px 32px rgba(0,0,0,.35),0 0 0 1px rgba(90,171,223,.08);
  backdrop-filter:blur(16px);
}}
.chat-input{{
  flex:1;background:transparent;border:none;outline:none;
  font-size:14px;color:#ffffff;font-family:var(--sans);
  resize:none;line-height:1.5;max-height:180px;min-height:24px;
  overflow-y:auto;
}}
.chat-input::placeholder{{color:rgba(255,255,255,.38);}}
.send-btn{{
  flex-shrink:0;width:38px;height:38px;border-radius:7px;
  background:rgba(90,171,223,.20);border:1px solid rgba(90,171,223,.38);
  color:var(--accent);cursor:pointer;display:flex;align-items:center;
  justify-content:center;transition:all .18s;font-size:16px;
}}
.send-btn:hover{{background:rgba(90,171,223,.35);transform:translateY(-1px);box-shadow:0 2px 8px rgba(90,171,223,.2);}}
.send-btn:disabled{{opacity:.35;cursor:not-allowed;}}
.input-hint{{font-size:11px;color:rgba(255,255,255,0.32);margin-top:8px;
            text-align:center;letter-spacing:.02em;}}

/* Empty state */
.empty-chat{{
  flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:48px 24px;gap:14px;
}}
.empty-chat-title{{font-family:var(--serif);font-size:30px;color:var(--text);font-weight:700;letter-spacing:-.2px;}}
.empty-chat-sub{{font-size:14px;color:rgba(255,255,255,0.62);text-align:center;max-width:460px;line-height:1.7;}}
.suggestion-chips{{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px;justify-content:center;max-width:560px;}}
.chip{{
  padding:9px 18px;background:rgba(90,171,223,.09);
  border:1px solid rgba(90,171,223,.22);border-radius:20px;
  font-size:12px;color:rgba(255,255,255,0.72);cursor:pointer;transition:all .18s;font-weight:500;
}}
.chip:hover{{background:rgba(90,171,223,.18);color:var(--text);border-color:rgba(90,171,223,.4);transform:translateY(-1px);}}

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
      <div class="empty-chat-sub">I'm Artemis — your Helion AI execution coach. I know your goals, your plan, and your patterns. Ask me anything about what to focus on, how to break through, or where to push harder.</div>
      <div class="suggestion-chips">
        <div class="chip" onclick="sendChip(this)">What should I focus on today?</div>
        <div class="chip" onclick="sendChip(this)">Where am I falling behind?</div>
        <div class="chip" onclick="sendChip(this)">I'm stuck — help me think through this</div>
        <div class="chip" onclick="sendChip(this)">Give me an honest assessment</div>
        <div class="chip" onclick="sendChip(this)">Help me plan my next 7 days</div>
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

<!-- Artemis Intelligence Rail -->
<div class="artemis-rail" id="artemisRail">
  <div class="rail-header">
    <div class="rail-brand">ARTEMIS / HELION AI</div>
    <div class="rail-context" id="railContext"></div>
  </div>
  <div class="rail-messages" id="railMessages"></div>
  <div class="rail-prompts" id="railPrompts"></div>
  <div class="rail-footer">
    <input class="rail-input" id="railInput" placeholder="Ask Artemis…"/>
    <button class="rail-send" id="railSend" onclick="railSendMsg()">&#9658;</button>
  </div>
</div>
<button class="rail-tab" id="railTab" onclick="toggleRail()">A R T E M I S</button>

<script>
const APP_STATE = {{
  hasGoals: {'true' if has_goals else 'false'},
  goalCount: {len(goals_list)},
  hasPlan: {'true' if has_plan else 'false'},
  planStatus: '{status}',
  displayName: '{display_name}',
  compRate: {comp_rate}
}};

var _railOpen=false;

function openRail(){{
  document.getElementById('artemisRail').classList.add('open');
  document.getElementById('railTab').classList.add('hidden');
  _railOpen=true;
  renderRailContext();
  renderRailPrompts();
  if(!document.getElementById('railMessages').children.length){{
    appendRailMsg('ai',APP_STATE.displayName+(APP_STATE.hasGoals
      ?'. '+APP_STATE.goalCount+' goal'+(APP_STATE.goalCount!==1?'s':'')+' active. What do you need?'
      :'. No goals yet — add one and I’ll have something to work with.'));
  }}
}}
function closeRail(){{
  document.getElementById('artemisRail').classList.remove('open');
  document.getElementById('railTab').classList.remove('hidden');
  _railOpen=false;
}}
function toggleRail(){{_railOpen?closeRail():openRail();}}

function renderRailContext(){{
  var el=document.getElementById('railContext');
  if(!el)return;
  var parts=[];
  if(APP_STATE.hasGoals)parts.push(APP_STATE.goalCount+' goal'+(APP_STATE.goalCount!==1?'s':''));
  if(APP_STATE.hasPlan)parts.push('plan '+APP_STATE.planStatus);
  if(APP_STATE.compRate>0)parts.push(Math.round(APP_STATE.compRate)+'% complete');
  el.textContent=parts.join(' · ')||'No active goals';
}}
function renderRailPrompts(){{
  var el=document.getElementById('railPrompts');if(!el)return;
  var chips=APP_STATE.hasGoals
    ?['What should I focus on?','Review my plan',"What's blocking me?",'Next action?']
    :['How does Helion work?','Help me set a goal','What can Artemis do?'];
  el.innerHTML='';
  chips.forEach(function(c){{
    var btn=document.createElement('button');
    btn.className='prompt-chip';
    btn.textContent=c;
    btn.onclick=function(){{railQuickPrompt(c);}};
    el.appendChild(btn);
  }});
}}
function appendRailMsg(role,text){{
  var el=document.getElementById('railMessages');
  if(!el)return;
  var d=document.createElement('div');
  d.className='rail-msg '+role;d.textContent=text;
  el.appendChild(d);el.scrollTop=el.scrollHeight;
}}
function railQuickPrompt(text){{appendRailMsg('user',text);railSendToApi(text);}}
async function railSendMsg(){{
  var inp=document.getElementById('railInput');
  var msg=inp?inp.value.trim():'';
  if(!msg)return;
  inp.value='';
  appendRailMsg('user',msg);
  railSendToApi(msg);
}}
async function railSendToApi(msg){{
  try{{
    appendRailMsg('ai','…');
    var msgsEl=document.getElementById('railMessages');
    var loading=msgsEl?msgsEl.lastChild:null;
    var resp=await fetch('/api/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:msg}})}});
    var data=await resp.json();
    if(loading)loading.remove();
    appendRailMsg('ai',data.response||data.error||'No response');
  }}catch(e){{
    var msgsEl=document.getElementById('railMessages');
    if(msgsEl&&msgsEl.lastChild)msgsEl.lastChild.textContent='Connection error. Try again.';
  }}
}}
document.addEventListener('DOMContentLoaded',function(){{
  var inp=document.getElementById('railInput');
  if(inp)inp.addEventListener('keydown',function(e){{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();railSendMsg();}}}});
}});
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
                self._html(LandingPageGenerator.generate())
            return

        if path == "/login":
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
            host   = self.headers.get("Host", "localhost")
            scheme = "http" if "localhost" in host else "https"
            redir  = os.environ.get("GOOGLE_REDIRECT_URI", f"{scheme}://{host}/auth/google/callback")
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
            host   = self.headers.get("Host", "localhost")
            scheme = "http" if "localhost" in host else "https"
            redir  = os.environ.get("GOOGLE_REDIRECT_URI", f"{scheme}://{host}/auth/google/callback")
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
