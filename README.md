# SMTP Test Tool

Simple SMTP testing web app.

## Features
- SMTP test form (server, port, security, username, password, from/to email)
- Live SMTP debug stream
- SQLite persistence for SMTP hosts, saved configs, and test history
- Tabbed interface focused only on SMTP: `SMTP Test`, `Configs`, `History`
- Config and History management

## Local run (without Docker)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open [http://localhost:8000](http://localhost:8000)

## Docker run
```bash
cp .env.example .env
# Edit .env and set a strong FLASK_SECRET_KEY
mkdir -p data
docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000)

## Notes
- In `Auto` mode, port `465` uses implicit TLS.
- For other ports in `Auto`, STARTTLS is required.
- If a server has no TLS support, use `Security=None` only on trusted networks.
