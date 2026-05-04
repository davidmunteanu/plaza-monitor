# 🏠 Plaza Delft Monitor

Watches [plaza.newnewnew.space](https://plaza.newnewnew.space) for new Delft
rental listings and pings you on **Discord** instantly.

Plaza listings go live ~4 weeks before availability. The first 10 minutes of
responses enter a **lottery** — speed matters. This runs 24/7 so you never miss one.

---

## Setup (5 minutes)

### 1. Create a Discord webhook

1. Open Discord, go to the server/channel where you want notifications
2. Right-click the channel → **Edit Channel** → **Integrations** → **Webhooks**
3. Click **New Webhook**, give it a name like "Plaza Monitor"
4. Click **Copy Webhook URL** — you'll need this in a moment

### 2. Deploy to GitHub Actions (free, runs every 10 min, zero maintenance)

```bash
# Clone / download this folder, then:
cd plaza_monitor
git init
git add .
git commit -m "Initial commit"
```

Create a **new repo** on GitHub (public = unlimited free minutes), then:

```bash
git remote add origin https://github.com/YOUR_USERNAME/plaza-monitor.git
git push -u origin main
```

Now add your webhook as a secret:

- Go to your repo → **Settings** → **Secrets and variables** → **Actions**
- Click **New repository secret**
- Name: `DISCORD_WEBHOOK_URL`
- Value: paste the webhook URL from step 1

### 3. Test it

Go to **Actions** tab → **Plaza Delft Monitor** → **Run workflow** → **Run workflow**.

Watch the run. If a message appears in your Discord channel, you're done.
It'll now run automatically every 10 minutes, forever, for free.

---

## Alternative: run locally

If you'd rather run it on your own machine:

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env — paste your Discord webhook URL
python plaza_monitor.py
```

Keep it running with `screen`, `tmux`, `nohup`, or a systemd service.

---

## How it works

Every 10 minutes, the script fetches 4 Plaza pages (the main listings overview
plus the two known Delft complex pages). It parses the HTML for listing links
containing "delft", hashes each URL to create a unique ID, and compares against
`seen_listings.json`. New listings trigger a Discord embed with title, price,
size, and a direct link.

On GitHub Actions, the seen file is committed back to the repo after each run
so state persists. The `[skip ci]` in the commit message prevents infinite loops.

## Anti-ban measures

- Randomised 8–15 min intervals between checks
- Rotates through 5 real browser User-Agent strings
- Visits homepage first for cookies (like a real browser)
- 2–5 second delays between page requests
- Exponential back-off on errors
- Only fetches public pages — no login, no scraping behind auth
