# The Last Offer

A visual-novel style startup pitching game where you face four investor personas in sequence.

The current app runs as a FastAPI + WebSocket backend with a single-page frontend.

You can try the game yourself from releases its an exe file but you need an API key so make an account on Groq and generate one for free.


## What this project includes

- Web app (current): FastAPI server + WebSocket event loop + HTML/CSS/JS frontend
- Core logic module: investor personas, Groq calls, scoring/parsing helpers


## Project structure

- `the_last_offer_vn/server.py`: FastAPI app entrypoint, static hosting, `/ws` WebSocket route
- `the_last_offer_vn/game_engine.py`: game state machine and event generation
- `the_last_offer_vn/static/index.html`: frontend UI and WebSocket client
- `the_last_offer_vn/requirements.txt`: dependencies for the current web app
- `the_last_offer_v4_25.py`: shared/core logic used by the VN engine



## Requirements

- Python 3.10+
- A Groq API key

## Setup

1. Create and activate a virtual environment (if not already created).

PowerShell (Windows):

```powershell
python -m venv .venv
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned)
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r .\the_last_offer_vn\requirements.txt
```

3. Create a `.env` file in the project root (`The Last Offer/.env`) with your Groq key:

```env
GROQ_API_KEY=your_key_here
```

The app also accepts lowercase `groq_api_key`, but `GROQ_API_KEY` is recommended.

## Run the current app

From project root:

```powershell
cd .\the_last_offer_vn
python .\server.py
```

Then open:

- http://127.0.0.1:8000

## How the app works (high level)

1. Frontend connects to `ws://127.0.0.1:8000/ws`.
2. UI sends actions (for example `start`, `select_mode`, `submit_text`).
3. `GameSession` in `game_engine.py` processes action + state.
4. Backend returns a list of UI events (`scene`, `dialogue`, `buttons`, etc.).
5. Frontend applies events in order.



## Troubleshooting

- `Error: HTTP 401/403`: check your Groq API key in `.env`.
- `Error: HTTP 429`: rate-limit hit; retry shortly.
- Frontend not loading: ensure `server.py` is running and you opened `http://127.0.0.1:8000`.
- WebSocket reconnect loops: check server logs for exceptions in `game_engine.py`.
