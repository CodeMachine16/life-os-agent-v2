# 🧠 Helion AI — Personal Execution Engine

> An AI-powered multi-agent system that acts as your personal chief of staff.
> It breaks down your goals, generates daily action plans, tracks progress,
> and dynamically replans when you fall behind — all powered by the Anthropic Claude API.

---

## What It Does

| Agent | Role |
|---|---|
| **PlannerAgent** | Breaks goals into specific, timed daily tasks |
| **CoachAgent** | Delivers direct, data-driven progress feedback |
| **ReplannerAgent** | Detects when you're off-track and restructures the plan |
| **DashboardGenerator** | Produces a beautiful HTML visual report |
| **LifeOSOrchestrator** | Coordinates all agents in a single daily cycle |

---

## Architecture

```
LifeOSAgent/
├── life_os_agent.py    # Full multi-agent system (single file, zero external deps)
├── goals.json          # Your goals & deadlines (auto-created via --setup)
├── memory.json         # Session history, streaks, completion tracking
├── dashboard.html      # Generated visual report (open in any browser)
└── README.md
```

**Zero external dependencies.** Uses Python's stdlib `urllib` to call the
Anthropic API directly — no pip install required.

---

## Quick Start

### 1. Try the demo (no API key needed)
```bash
python3 life_os_agent.py --demo
# Then open dashboard.html in your browser
```

### 2. Set up with your own goals
```bash
python3 life_os_agent.py --setup
# Follow the wizard to add goals and your API key
```

### 3. Run your daily cycle
```bash
python3 life_os_agent.py --run
# Generates a fresh plan + coaching + dashboard every day
```

### 4. Log what you completed
```bash
python3 life_os_agent.py --checkin t1 t3 t4
# Updates your streak and completion rate
```

---

## How the AI Works

Each agent sends a carefully engineered prompt to `claude-opus-4-6` with a
different persona and objective:

- **Planner** is told to be ruthlessly specific — no vague tasks allowed
- **Coach** is told to be direct and data-driven, like "a startup CEO meets
  personal trainer"
- **Replanner** detects patterns (not just missed tasks) and restructures
  forward momentum

The system maintains a rolling 30-session memory to detect trends in your
behavior and adjust advice accordingly.

---

## Resume Talking Points

- Built a multi-agent AI orchestration system using the Anthropic Claude API
- Designed prompt engineering for three specialized AI personas (Planner, Coach, Replanner)
- Implemented persistent memory/state management for cross-session learning
- Zero external dependencies — pure Python stdlib HTTP client for API calls
- Generated interactive HTML dashboards with real-time task completion tracking

---

## Requirements

- Python 3.8+
- An [Anthropic API key](https://console.anthropic.com) (free tier available)
- No other dependencies

---

*Built with Claude claude-opus-4-6 · Helion AI v1.0*
