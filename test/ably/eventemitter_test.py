import asyncio
from ably.realtime.connection import ConnectionState
from test.ably.restsetup import RestSetup
from test.ably.utils import BaseAsyncTestCase


class TestEventEmitter(BaseAsyncTestCase):
    async def setUp(self):
        self.test_vars = await RestSetup.get_test_vars()

    async def test_connection_events(self):
        realtime = await RestSetup.get_ably_realtime()
        call_count = 0

        def listener(_):
            nonlocal call_count
            call_count += 1

        realtime.connection.on(ConnectionState.CONNECTED, listener)

        await realtime.connect()

        # Listener is only called once event loop is free
        assert call_count == 0
        await asyncio.sleep(0)
        assert call_count == 1
        await realtime.close()

    async def test_event_listener_error(self):
        realtime = await RestSetup.get_ably_realtime()
        call_count = 0

        def listener(_):
            nonlocal call_count
            call_count += 1
            raise Exception()

        # If a listener throws an exception it should not propagate (#RTE6)
        listener.side_effect = Exception()
        realtime.connection.on(ConnectionState.CONNECTED, listener)

        await realtime.connect()

        assert call_count == 0
        await asyncio.sleep(0)
        assert call_count == 1
        await realtime.close()

    async def test_event_emitter_off(self):
        realtime = await RestSetup.get_ably_realtime()
        call_count = 0

        def listener(_):
            nonlocal call_count
            call_count += 1

        realtime.connection.on(ConnectionState.CONNECTED, listener)
        realtime.connection.off(ConnectionState.CONNECTED, listener)

        await realtime.connect()

        assert call_count == 0
        await asyncio.sleep(0)
        assert call_count == 0
        await realtime.close()
