# Elixir AI Bot Suite

Python service that runs three specialized Telegram AI assistants for the Elixir Peptide ecosystem. The assistants share operational infrastructure while keeping their prompts, credentials, and conversation behavior independently configurable.

## Highlights

- Runs three polling bots concurrently with automatic restart and graceful shutdown.
- Integrates configurable OpenAI assistants with separate instruction sets.
- Connects AI conversations to shop catalog, order, and internal API workflows.
- Includes dose calculation and simulation utilities.
- Provides admin, broadcast, guest, and user interaction flows.
- Enforces soft and hard conversation token limits.
- Records operational heartbeat state without committing runtime logs.

## Mentor Workspace

`/mentor` opens Today, Nutrition, Workouts, My Course, Progress, Ask Mentor,
Profile and Settings. Confirmed meals, training sets, existing course schedules,
measurements and notification rules are stored through the signed shop API.
Ordinary AI conversations, subscription checks and media limits are unchanged.

Deploy the matching Shop Application backend before restarting this bot.
The reminder protocol uses leases and delivery acknowledgements; stop the old bot
while changing the backend contract, and roll both back together if needed.
Preserve `.env`, `data/`, instruction files and Telegram sessions.

Configuration: `TELEGRAM_MENTOR_CLOSED_SECTIONS` is empty by default;
`TELEGRAM_MENTOR_SPECIALIST_URL` must be an explicitly approved HTTPS destination.
The bot never generates medical prescriptions or changes doses itself.
Implementation and operational details are maintained in the Shop Application
repository at `integrations/telegram-ai-bot/README.md`.

## Architecture

```text
Telegram users
  -> aiogram bot handlers
  -> assistant routing and conversation controls
  -> OpenAI assistant integration
  -> Elixir Shop internal API
  -> calculation and plotting utilities
```

Important directories:

- `src/ai/`: AI client, assistant helpers, and shop web-client integration.
- `src/bot/`: handlers, middleware, keyboards, state machines, and limits.
- `src/calc/`: calculation, simulation, and plotting utilities.
- `data/instructions/`: versioned instructions for each assistant mode.
- `tests/`: deterministic unit tests and manual integration utilities.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Configuration notes:

- `WEBAPP_BASE_DOMAIN` points to the public shop web application.
- `INTERNAL_API_BASE_URL` points to the shop backend used for bot-to-shop operations.
- Each assistant requires its own bot token, assistant ID, and API credential.
- Telethon credentials are required for Telegram client functionality.

## Tests

Run the deterministic test suite without starting Telegram polling:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

`tests/gm.py` is a manual Telegram integration check. It requires `GUEST_TEST_BOT_TOKEN` and should not be run in automated CI.

## Security and Privacy

- Never commit `.env`, Telethon sessions, API keys, bot tokens, chat exports, or runtime logs.
- Use synthetic data when demonstrating customer or order workflows.
- Rotate credentials immediately if they have ever appeared in Git history.
