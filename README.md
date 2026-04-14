# AI Bot

## Run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

## Config
- Fill `.env` from `.env.example`.
- `WEBAPP_BASE_DOMAIN` must point to the public shop webapp (product links and static assets).
- `INTERNAL_API_BASE_URL` must point to the shop backend base URL used for bot-to-shop internal API calls.
