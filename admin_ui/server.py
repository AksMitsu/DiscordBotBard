"""Локальный сервер админ-панели: отдаёт UI и проксирует /api и /ws на бота, подставляя токен."""
import asyncio
import os
import json
import sys
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

import rest_backend

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, '.env'))
load_dotenv(os.path.join(HERE, '..', '.env'))

BOT_URL = os.getenv('BOT_URL', 'http://127.0.0.1:8787').rstrip('/')
TOKEN = os.getenv('ADMIN_API_TOKEN', '').strip()
READONLY = os.getenv('ADMIN_MODE', 'remote').strip().lower() == 'readonly'
MAX_UPLOAD = (max(int(os.getenv('SOUND_MAX_MB', '10')), 25) + 1) * 1024 * 1024
HOST = os.getenv('UI_HOST', '127.0.0.1')
PORT = int(os.getenv('UI_PORT', '8080'))

HOP = {'host', 'content-length', 'connection', 'upgrade', 'transfer-encoding', 'authorization', 'origin', 'cookie'}


def _allowed_hosts():
    return {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'[::1]:{PORT}', f'{HOST}:{PORT}'}


@web.middleware
async def guard(request: web.Request, handler):
    # прокси держит токен бота: Host/Origin проверяются против DNS rebinding и CSRF
    if request.host not in _allowed_hosts():
        return web.json_response({'error': 'bad host'}, status=403)
    origin = request.headers.get('Origin')
    if origin and urlparse(origin).netloc not in _allowed_hosts():
        return web.json_response({'error': 'bad origin'}, status=403)
    return await handler(request)


async def index(request):
    return web.FileResponse(os.path.join(HERE, 'static', 'index.html'), headers={'Cache-Control': 'no-store'})


async def local_info(request):
    return web.json_response({'bot_url': BOT_URL})


async def proxy_api(request: web.Request):
    if READONLY:
        return await rest_backend.handle(request.app['rest'], request)
    url = f'{BOT_URL}{request.rel_url}'
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers['Authorization'] = f'Bearer {TOKEN}'
    data = await request.read() if request.can_read_body else None
    try:
        async with request.app['http'].request(request.method, url, headers=headers, data=data) as r:
            body = await r.read()
            return web.Response(body=body, status=r.status, content_type=r.content_type or 'application/json')
    except asyncio.TimeoutError:
        return web.json_response({'error': 'Бот не ответил вовремя'}, status=504)
    except aiohttp.ClientError as e:
        return web.json_response({'error': f'Бот недоступен ({BOT_URL}): {type(e).__name__}'}, status=502)


async def readonly_ws(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    backend = request.app['rest']

    async def push():
        await ws.send_str(json.dumps({'type': 'events', 'data': []}))
        while not ws.closed:
            try:
                await ws.send_str(json.dumps({'type': 'snapshot', 'data': await backend.snapshot()}))
            except (rest_backend.RestError, aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(10)

    sender = asyncio.ensure_future(push())
    try:
        async for _ in ws:
            pass
    finally:
        sender.cancel()
    return ws


async def proxy_ws(request: web.Request):
    if READONLY:
        return await readonly_ws(request)
    ws_client = web.WebSocketResponse(heartbeat=25)
    await ws_client.prepare(request)
    try:
        async with request.app['http'].ws_connect(
                f'{BOT_URL}/ws', headers={'Authorization': f'Bearer {TOKEN}'}, heartbeat=25) as ws_bot:
            async def up():
                async for msg in ws_client:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_bot.send_str(msg.data)

            async def down():
                async for msg in ws_bot:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws_client.send_str(msg.data)

            tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
    except Exception:
        pass
    await ws_client.close()
    return ws_client


async def on_startup(app):
    app['http'] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
    if READONLY:
        app['rest'] = rest_backend.RestBackend(os.getenv('DISCORD_TOKEN', '').strip(), app['http'])


async def on_cleanup(app):
    await app['http'].close()


def main():
    if READONLY and not os.getenv('DISCORD_TOKEN'):
        sys.exit('Для ADMIN_MODE=readonly нужен DISCORD_TOKEN (admin_ui/.env или .env бота)')
    if not READONLY and not TOKEN:
        sys.exit('Не задан ADMIN_API_TOKEN в admin_ui/.env (скопируйте .env.example)')
    app = web.Application(middlewares=[guard], client_max_size=MAX_UPLOAD)
    app.router.add_get('/', index)
    app.router.add_get('/local/info', local_info)
    app.router.add_get('/ws', proxy_ws)
    app.router.add_route('*', '/api/{tail:.*}', proxy_api)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    target = 'Discord REST (режим просмотра)' if READONLY else f'бот {BOT_URL}'
    print(f'Админ-панель: http://{HOST}:{PORT}  ->  {target}')
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == '__main__':
    main()
