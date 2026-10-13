Output assets/demo.gif

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 15
Set LineHeight 1.15
Set Width 1280
Set Height 760
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 65ms

# Setup (hidden): throwaway SQLite DB so local data is never touched. The API
# runs from the venv directly so `kill %1` stops the real server PID. Two
# prior leads are seeded after the schema exists so /leads spans several tiers.
Hide
Type@5ms "export PS1='$ ' DATABASE_PATH=/tmp/demo.db && rm -f /tmp/demo.db" Enter
Type@5ms ".venv/bin/python -m uvicorn src.api.app:app --port 8000 >/tmp/demo-api.log 2>&1 &" Enter
Type@5ms "until curl -sf localhost:8000/health >/dev/null; do sleep 0.5; done; echo REA''DY" Enter
Wait+Screen@40s /READY/
Type@5ms `.venv/bin/python -c 'import sqlite3; c = sqlite3.connect("/tmp/demo.db"); c.executemany("INSERT INTO leads (url, domain, company_name, fit_score, fit_tier, status) VALUES (?, ?, ?, ?, ?, ?)", [("https://ikea.com", "ikea.com", "IKEA", 0, "Disqualified", "PROCESSED"), ("https://thoughtbot.com", "thoughtbot.com", "thoughtbot", 66, "Tier 2 (Medium)", "PROCESSED")]); c.commit()'` Enter
Sleep 500ms
Type@5ms "clear" Enter
Sleep 500ms
Show

# 1. Reliability: full offline suite
Sleep 600ms
Type "uv run pytest -q"
Sleep 500ms
Enter
Wait+Screen@60s /passed/
Sleep 3s

# Fresh screen for the API walkthrough (clear 1/2)
Type "clear"
Sleep 300ms
Enter
Sleep 600ms

# 2. API + DB readiness
Type "curl -s localhost:8000/health | jq"
Sleep 500ms
Enter
Sleep 2500ms

# 3. SSRF guard rejects the cloud-metadata address before any browser starts
Type `curl -s -X POST localhost:8000/api/v1/enrich -H 'Content-Type: application/json' -d '{"url":"http://169.254.169.254"}' | jq '.detail[0].msg'`
Sleep 500ms
Enter
Sleep 4s

# 4. A valid URL is queued and the response returns immediately. The scrape
# and LLM extraction (~9s) run in the background while the next command is typed.
Type `curl -s -X POST localhost:8000/api/v1/enrich -H 'Content-Type: application/json' -d '{"url":"https://linear.app"}' | jq`
Sleep 500ms
Enter
Sleep 3s

# Full height for the enriched payload (clear 2/2)
Type "clear"
Sleep 300ms
Enter
Sleep 600ms

# 5. Structured result: Pydantic-validated scoring and outreach
Type `curl -s localhost:8000/api/v1/leads/3 | jq '{company_name, fit_score, fit_tier} + .enriched_data.outreach'`
Sleep 500ms
Enter
Sleep 8s

# 6. Multi-lead retrieval across tiers
Type `curl -s 'localhost:8000/api/v1/leads' | jq '[.items[] | {company: .company_name, score: .fit_score, tier: .fit_tier}]'`
Sleep 500ms
Enter
Sleep 5s

# Teardown (hidden): stop the server and drop the throwaway DB.
Hide
Type@5ms "kill %1; wait; rm -f /tmp/demo.db /tmp/demo-api.log" Enter
Sleep 1s
