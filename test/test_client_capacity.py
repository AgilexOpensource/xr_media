import asyncio
import unittest

from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from xr_media import MediaServer, MediaServerConfig


class ClientCapacityTest(unittest.IsolatedAsyncioTestCase):
    async def test_admission_reserves_seat_before_media_negotiation(self):
        server = MediaServer(MediaServerConfig(max_clients=1))
        app = web.Application()
        app.router.add_get('/signal', server._websocket)
        async with TestClient(TestServer(app)) as client:
            first = await client.ws_connect('/signal?admission=1')
            self.assertEqual((await first.receive_json(timeout=2))['type'],
                             'session.accepted')
            self.assertFalse(server._peers)
            second = await client.ws_connect('/signal?admission=1')
            reply = await second.receive_json(timeout=2)
            self.assertEqual(reply['code'], 'capacity_full')
            self.assertEqual((reply['count'], reply['limit']), (1, 1))
            await second.close()
            await first.close()

            async def wait_for_release():
                while server._clients:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_release(), 2)
            replacement = await client.ws_connect('/signal?admission=1')
            self.assertEqual((await replacement.receive_json(timeout=2))['type'],
                             'session.accepted')
            await replacement.close()

    async def test_audio_input_is_exclusive_and_released(self):
        await self.check_input_ownership('audio')

    async def test_video_input_is_exclusive_and_released(self):
        await self.check_input_ownership('video')

    async def check_input_ownership(self, kind):
        server = MediaServer(MediaServerConfig(max_clients=2))
        server.register_browser_input('input', kind)
        app = web.Application()
        app.router.add_get('/signal', server._websocket)
        async with TestClient(TestServer(app)) as client:
            first = await client.ws_connect('/signal')
            second = await client.ws_connect('/signal')

            async def control(socket, action, status='live'):
                await socket.send_json({'type': 'input.control', 'stream': 'input',
                                        'action': action, 'status': status})

            await control(first, 'claim')
            self.assertTrue((await first.receive_json(timeout=2))['ok'])
            await control(first, 'start')
            await control(second, 'claim')
            reply = await second.receive_json(timeout=2)
            self.assertEqual(reply['code'], 'input_busy')
            self.assertEqual(reply['client']['id'], 'client-1')
            for action in ('stop', 'pause', 'speaker', 'start'):
                await control(second, action, 'idle')
                self.assertFalse((await second.receive_json(timeout=2))['ok'])
                self.assertEqual(server._browser_inputs['input']['status'], 'live')
            await control(first, 'stop', 'idle')
            # Same-socket claim confirms that stop has been processed.
            await control(first, 'claim')
            self.assertTrue((await first.receive_json(timeout=2))['ok'])
            await first.close()

            async def wait_for_release():
                while server._input_owners:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_release(), 2)
            await control(second, 'claim')
            self.assertTrue((await second.receive_json(timeout=2))['ok'])
            await second.close()

    async def test_full_server_preserves_clients_and_reuses_vacant_seat(self):
        server = MediaServer(MediaServerConfig(max_clients=2))
        app = web.Application()
        app.router.add_get('/signal', server._websocket)
        async with TestClient(TestServer(app)) as client:
            first = await client.ws_connect('/signal', headers={'User-Agent': 'Firefox/156.0'})
            second = await client.ws_connect('/signal', headers={'User-Agent': 'Chrome/153.0'})
            rejected = await client.ws_connect('/signal')
            response = await rejected.receive_json(timeout=2)
            self.assertEqual(response['code'], 'capacity_full')
            self.assertEqual((response['count'], response['limit']), (2, 2))
            self.assertEqual(response['clients'], [
                {'id': 'client-1', 'browser': 'Firefox'},
                {'id': 'client-2', 'browser': 'Chrome'},
            ])
            self.assertEqual((await rejected.receive(timeout=2)).type, WSMsgType.CLOSE)
            self.assertFalse(first.closed)
            self.assertFalse(second.closed)
            self.assertEqual(len(server._clients), 2)
            await first.close()

            async def wait_for_vacancy():
                while len(server._clients) != 1:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_vacancy(), 2)
            replacement = await client.ws_connect('/signal')
            self.assertEqual(len(server._clients), 2)
            self.assertEqual([entry['id'] for entry in server._clients.values()],
                             ['client-2', 'client-3'])
            await second.close()
            await replacement.close()
