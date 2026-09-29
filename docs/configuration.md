# Configuration

Every environment variable, setting and command in Beehive. A first run needs only three
variables; the [quick start](../README.md#quick-start) walks through them. To run Beehive on a
server, see [deploy/README.md](../deploy/README.md).

## Environment variables

Secrets and paths come from the environment. Everything else is set in the web app.

| Variable | Needed for | What it does |
| --- | --- | --- |
| `DB_PATH` | Every command | Path to the SQLite database. Defaults to `/data/beehive.db`, the path inside the container. |
| `SESSION_SECRET` | The web app | Signs the sign-in cookie. Use 32 or more random characters, for example from `openssl rand -hex 32`. The web app won't start without it. |
| `COPILOT_GITHUB_TOKEN` | Scores, summaries, deep reads, research and the model list | A GitHub fine-grained personal access token with the **Copilot Requests** permission ([GitHub's guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli)). Classic tokens don't work. The collector, which also writes deep reads, and the research worker need it; the web app doesn't. |
| `ACS_CONNECTION_STRING` | Sending email | Connects to Azure Communication Services Email, the only email provider. Install the `email` extra to use it. Without it, Beehive prints each email instead of sending it. |
| `DIGEST_EMAIL_FROM` | Sending email | The sender address. Your Azure resource must allow it. Defaults to `beehive@example.com`. |
| `DIGEST_EMAIL_TO` | Sending email | The default recipient. The **Default email address** in the admin overrides it. |
| `DB_SYNCHRONOUS` | Tuning | SQLite's `synchronous` mode: `OFF`, `NORMAL`, `FULL` or `EXTRA`. Defaults to `NORMAL`: a crash loses nothing, and a power cut can undo the last few writes. |
| `TRUSTED_PROXY_IPS` | A reverse proxy | Your proxy's addresses, so the sign-in rate limit sees each visitor's real address. See [Reverse proxy and client addresses](../deploy/README.md#reverse-proxy-and-client-addresses). |
| `RESEARCH_WORKER_*` | The research worker | Pool sizes, polling and lease timing. See [Environment overrides](../deploy/README.md#environment-overrides). |

Keep credentials out of the repository. The server setup passes them in as Podman secrets.

## Settings in the web app

**Global settings**:

- **Platform language**: pages, email and AI output in English, Simplified Chinese, Japanese,
  Korean, Spanish, French or German. English is the default. Changing it doesn't translate
  summaries that already exist.
- **LLM model**: the model for future scores, summaries and deep reads. The default is
  `claude-haiku-4.5`. **Refresh list** loads the models your Copilot account offers. The research
  worker does the loading, so it has to be running. Old work isn't redone with the new model.
- **Featured window**: how many days of stories the home page shows, 1 to 30. The default is 3.
  Days follow Auckland time, and a story without a publish time counts from when it was fetched.

**Email groups**: the **Default email address**, and for each group its channels, recipient and
**Delivery schedule**. A schedule is either a fixed interval, or selected weekdays at a local
`HH:MM` time in a timezone such as `Pacific/Auckland`. Each group shows its last check, last send,
next send and latest error.

**Each channel**: **What to focus on**, **Fetch schedule**, **Highlights per channel**, **Minimum AI
score**, its email group and a **Failure alert email**. The schedule is every 3 or 6 hours, or once
a day at a time in a timezone. A daily fetch keeps to its time: a late run doesn't push the next
one back, and **Fetch now** doesn't move it.

## Commands

Run these from the repository folder with `DB_PATH` set. On a server, the units in `deploy/` run
them for you.

| Command | What it does | On a server |
| --- | --- | --- |
| `python -m scripts.set_admin_password` | Sets or changes the admin password, and creates the database if it doesn't exist. | Once |
| `python -m scripts.run_web` | Runs the web app on port 8000, on all network interfaces. | Always on |
| `python -m scripts.run_collector --mode fetch` | Fetches every channel that is due and scores the new items. A new channel is due at once. | Every 15 minutes |
| `python -m scripts.run_collector --mode fetch-channel` | Fetches the channels queued with **Fetch now**. | When you select **Fetch now** |
| `python -m scripts.run_collector --mode digest` | Sends the email groups that are due, and research-finished emails. | Every 15 minutes |
| `python -m scripts.run_collector --mode tracker-reminders` | Emails a reminder for each watched lot about an hour before it closes. The old name `auction-reminders` still works. | Every 5 minutes |
| `python -m scripts.run_collector --mode deep-read` | Writes the deep reads you asked for. | When you ask, and every 30 minutes |
| `python -m scripts.run_collector --mode migrate` | Brings the database up to this version. Every command also does this when it starts. | At each release, after a backup |
| `python -m scripts.run_research_worker` | Runs research sessions and chat replies, and loads the model list. | Always on |

Use the Python in your virtual environment, such as `.venv/bin/python`.

### Without the server setup

The buttons in the web app only queue work, and the server setup picks it up. When you run Beehive
by hand, run the matching command:

- To fetch, run `--mode fetch`. **Fetch now** needs the server setup, where a systemd path unit
  hands the request to `--mode fetch-channel`.
- After you select **Deep read**, run `--mode deep-read`.
- For research sessions and **Refresh list**, keep `scripts.run_research_worker` running.
- To send email and reminders, run `--mode digest` and `--mode tracker-reminders`.

To keep Beehive current, run `--mode fetch` on a schedule, for example every 15 minutes with cron.
