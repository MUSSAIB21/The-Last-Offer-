"""
FastAPI + WebSocket server for The Last Offer visual novel.
Serves the static frontend and handles game logic via WebSocket.
"""
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from game_engine import GameSession

app = FastAPI(title='The Last Offer')

_static = os.path.join(os.path.dirname(__file__), 'static')
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_deck_file = os.path.join(_project_root, 'pitchDeck.html')
app.mount('/static', StaticFiles(directory=_static), name='static')
# Compatibility mount so the pitch deck's existing relative asset paths keep working.
app.mount('/the_last_offer_vn/static', StaticFiles(directory=_static), name='static_compat')


@app.get('/')
async def root():
    return FileResponse(os.path.join(_static, 'index.html'))


@app.get('/deck')
async def deck():
    if os.path.exists(_deck_file):
        return FileResponse(_deck_file)
    return FileResponse(os.path.join(_static, 'index.html'))


@app.websocket('/ws')
async def ws(websocket: WebSocket):
    await websocket.accept()
    session = GameSession()

    # Send initial screen
    for e in await session.handle('start'):
        pass  # Client requests start explicitly
    try:
        while True:
            msg = await websocket.receive_json()
            action = msg.get('type', '')
            # Special case: transition_done triggers advancing to next investor
            if action == 'transition_done':
                events = await session._advance_to_investor()
            # Special case: continue_talk toggles post-verdict mode
            elif action == 'continue_talk':
                session.state['post_verdict_mode'] = True
                events = [
                    {'type': 'input', 'enabled': True, 'placeholder': 'Ask them anything...'},
                    {'type': 'buttons', 'buttons': [
                        {'id': 'continue', 'label': 'Next →', 'variant': 'primary'},
                    ]},
                ]
            else:
                events = await session.handle(action, msg)
            # Send all events as a JSON array
            await websocket.send_json(events)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json([{'type': 'error', 'message': str(e)}])
        except Exception:
            pass


if __name__ == '__main__':
    import uvicorn
    print('🚀 Starting The Last Offer (Visual Novel)...')
    uvicorn.run(app, host='127.0.0.1', port=8000)
