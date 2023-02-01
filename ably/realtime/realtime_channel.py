import asyncio
from dataclasses import dataclass
import logging
from typing import Optional

from ably.realtime.connection import ConnectionState, ProtocolMessageAction
from ably.rest.channel import Channel
from ably.types.message import Message
from ably.util.eventemitter import EventEmitter
from ably.util.exceptions import AblyException
from enum import Enum

from ably.util.helper import Timer, is_callable_or_coroutine

log = logging.getLogger(__name__)


class ChannelState(str, Enum):
    INITIALIZED = 'initialized'
    ATTACHING = 'attaching'
    ATTACHED = 'attached'
    DETACHING = 'detaching'
    DETACHED = 'detached'
    SUSPENDED = 'suspended'
    FAILED = 'failed'


class Flag(int, Enum):
    # Channel attach state flags
    HAS_PRESENCE = 1 << 0
    HAS_BACKLOG = 1 << 1
    RESUMED = 1 << 2
    TRANSIENT = 1 << 4
    ATTACH_RESUME = 1 << 5
    # Channel mode flags
    PRESENCE = 1 << 16
    PUBLISH = 1 << 17
    SUBSCRIBE = 1 << 18
    PRESENCE_SUBSCRIBE = 1 << 19


def has_flag(message_flags: int, flag: Flag):
    return message_flags & flag > 0


@dataclass
class ChannelStateChange:
    previous: ChannelState
    current: ChannelState
    resumed: bool
    reason: Optional[AblyException] = None


class RealtimeChannel(EventEmitter, Channel):
    """
    Ably Realtime Channel

    Attributes
    ----------
    name: str
        Channel name
    state: str
        Channel state

    Methods
    -------
    attach()
        Attach to channel
    detach()
        Detach from channel
    subscribe(*args)
        Subscribe to messages on a channel
    unsubscribe(*args)
        Unsubscribe to messages from a channel
    """

    def __init__(self, realtime, name):
        EventEmitter.__init__(self)
        self.__name = name
        self.__realtime = realtime
        self.__state = ChannelState.INITIALIZED
        self.__message_emitter = EventEmitter()
        self.__state_timer: Timer | None = None
        self.__attach_resume = False
        self.__channel_serial: str | None = None

        # Used to listen to state changes internally, if we use the public event emitter interface then internals
        # will be disrupted if the user called .off() to remove all listeners
        self.__internal_state_emitter = EventEmitter()

        Channel.__init__(self, realtime, name, {})

    # RTL4
    async def attach(self):
        """Attach to channel

        Attach to this channel ensuring the channel is created in the Ably system and all messages published
        on the channel are received by any channel listeners registered using subscribe

        Raises
        ------
        AblyException
            If unable to attach channel
        """

        log.info(f'RealtimeChannel.attach() called, channel = {self.name}')

        # RTL4a - if channel is attached do nothing
        if self.state == ChannelState.ATTACHED:
            return

        # RTL4b
        if self.__realtime.connection.state not in [
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.DISCONNECTED
        ]:
            raise AblyException(
                message=f"Unable to attach; channel state = {self.state}",
                code=90001,
                status_code=400
            )

        if self.state != ChannelState.ATTACHING:
            self._request_state(ChannelState.ATTACHING)

        state_change = await self.__internal_state_emitter.once_async()

        if state_change.current in (ChannelState.SUSPENDED, ChannelState.FAILED):
            raise state_change.reason

    def _attach_impl(self):
        log.info("RealtimeChannel.attach_impl(): sending ATTACH protocol message")

        # RTL4c
        attach_msg = {
            "action": ProtocolMessageAction.ATTACH,
            "channel": self.name,
        }

        if self.__attach_resume:
            attach_msg["flags"] = Flag.ATTACH_RESUME
        if self.__channel_serial:
            attach_msg["channelSerial"] = self.__channel_serial

        self._send_message(attach_msg)

    # RTL5
    async def detach(self):
        """Detach from channel

        Any resulting channel state change is emitted to any listeners registered
        Once all clients globally have detached from the channel, the channel will be released
        in the Ably service within two minutes.

        Raises
        ------
        AblyException
            If unable to detach channel
        """

        log.info(f'RealtimeChannel.detach() called, channel = {self.name}')

        # RTL5g, RTL5b - raise exception if state invalid
        if self.__realtime.connection.state in [ConnectionState.CLOSING, ConnectionState.FAILED]:
            raise AblyException(
                message=f"Unable to detach; channel state = {self.state}",
                code=90001,
                status_code=400
            )

        # RTL5a - if channel already detached do nothing
        if self.state in [ChannelState.INITIALIZED, ChannelState.DETACHED]:
            return

        if self.state == ChannelState.SUSPENDED:
            self._notify_state(ChannelState.DETACHED)
            return
        elif self.state == ChannelState.FAILED:
            raise AblyException("Unable to detach; channel state = failed", 90001, 400)
        else:
            self._request_state(ChannelState.DETACHING)

        # RTL5h - wait for pending connection
        if self.__realtime.connection.state == ConnectionState.CONNECTING:
            await self.__realtime.connect()

        state_change = await self.__internal_state_emitter.once_async()
        new_state = state_change.current

        if new_state == ChannelState.DETACHED:
            return
        elif new_state == ChannelState.ATTACHING:
            raise AblyException("Detach request superseded by a subsequent attach request", 90000, 409)
        else:
            raise state_change.reason

    def _detach_impl(self):
        log.info("RealtimeChannel.detach_impl(): sending DETACH protocol message")

        # RTL5d
        detach_msg = {
            "action": ProtocolMessageAction.DETACH,
            "channel": self.__name,
        }

        self._send_message(detach_msg)

    # RTL7
    async def subscribe(self, *args):
        """Subscribe to a channel

        Registers a listener for messages on the channel.
        The caller supplies a listener function, which is called
        each time one or more messages arrives on the channel.

        The function resolves once the channel is attached.

        Parameters
        ----------
        *args: event, listener
            Subscribe event and listener

            arg1(event): str, optional
                Subscribe to messages with the given event name

            arg2(listener): callable
                Subscribe to all messages on the channel

            When no event is provided, arg1 is used as the listener.

        Raises
        ------
        AblyException
            If unable to subscribe to a channel due to invalid connection state
        ValueError
            If no valid subscribe arguments are passed
        """
        if isinstance(args[0], str):
            event = args[0]
            if not args[1]:
                raise ValueError("channel.subscribe called without listener")
            if not is_callable_or_coroutine(args[1]):
                raise ValueError("subscribe listener must be function or coroutine function")
            listener = args[1]
        elif is_callable_or_coroutine(args[0]):
            listener = args[0]
            event = None
        else:
            raise ValueError('invalid subscribe arguments')

        log.info(f'RealtimeChannel.subscribe called, channel = {self.name}, event = {event}')

        if event is not None:
            # RTL7b
            self.__message_emitter.on(event, listener)
        else:
            # RTL7a
            self.__message_emitter.on(listener)

        # RTL7c
        await self.attach()

    # RTL8
    def unsubscribe(self, *args):
        """Unsubscribe from a channel

        Deregister the given listener for (for any/all event names).
        This removes an earlier event-specific subscription.

        Parameters
        ----------
        *args: event, listener
            Unsubscribe event and listener

            arg1(event): str, optional
                Unsubscribe to messages with the given event name

            arg2(listener): callable
                Unsubscribe to all messages on the channel

            When no event is provided, arg1 is used as the listener.

        Raises
        ------
        ValueError
            If no valid unsubscribe arguments are passed, no listener or listener is not a function
            or coroutine
        """
        if len(args) == 0:
            event = None
            listener = None
        elif isinstance(args[0], str):
            event = args[0]
            if not args[1]:
                raise ValueError("channel.unsubscribe called without listener")
            if not is_callable_or_coroutine(args[1]):
                raise ValueError("unsubscribe listener must be a function or coroutine function")
            listener = args[1]
        elif is_callable_or_coroutine(args[0]):
            listener = args[0]
            event = None
        else:
            raise ValueError('invalid unsubscribe arguments')

        log.info(f'RealtimeChannel.unsubscribe called, channel = {self.name}, event = {event}')

        if listener is None:
            # RTL8c
            self.__message_emitter.off()
        elif event is not None:
            # RTL8b
            self.__message_emitter.off(event, listener)
        else:
            # RTL8a
            self.__message_emitter.off(listener)

    def _on_message(self, msg):
        action = msg.get('action')

        # RTL4c1
        channel_serial = msg.get('channelSerial')
        if channel_serial:
            self.__channel_serial = channel_serial

        if action == ProtocolMessageAction.ATTACHED:
            flags = msg.get('flags')
            error = msg.get("error")
            exception = None
            resumed = None

            if error:
                exception = AblyException(error.get('message'), error.get('statusCode'), error.get('code'))

            if flags:
                resumed = has_flag(flags, Flag.RESUMED)

            #  RTL12
            if self.state == ChannelState.ATTACHED:
                if not resumed:
                    state_change = ChannelStateChange(self.state, ChannelState.ATTACHED, resumed, exception)
                    self._emit("update", state_change)
            elif self.state == ChannelState.ATTACHING:
                self._notify_state(ChannelState.ATTACHED, resumed=resumed)
            else:
                log.warn("RealtimeChannel._on_message(): ATTACHED received while not attaching")
        elif action == ProtocolMessageAction.DETACHED:
            if self.state == ChannelState.DETACHING:
                self._notify_state(ChannelState.DETACHED)
            else:
                log.warn("RealtimeChannel._on_message(): DETACHED recieved while not detaching")
        elif action == ProtocolMessageAction.MESSAGE:
            messages = Message.from_encoded_array(msg.get('messages'))
            for message in messages:
                self.__message_emitter._emit(message.name, message)

    def _request_state(self, state: ChannelState):
        log.info(f'RealtimeChannel._request_state(): state = {state}')
        self._notify_state(state)
        self._check_pending_state()

    def _notify_state(self, state: ChannelState, reason=None, resumed=False):
        log.info(f'RealtimeChannel._notify_state(): state = {state}')

        self.__clear_state_timer()

        if state == self.state:
            return

        # RTL4j1
        if state == ChannelState.ATTACHED:
            self.__attach_resume = True
        if state in (ChannelState.DETACHING, ChannelState.FAILED):
            self.__attach_resume = False

        # RTP5a1
        if state in (ChannelState.DETACHED, ChannelState.SUSPENDED, ChannelState.FAILED):
            self.__channel_serial = None

        state_change = ChannelStateChange(self.__state, state, resumed, reason=reason)

        self.__state = state
        self._emit(state, state_change)
        self.__internal_state_emitter._emit(state, state_change)

    def _send_message(self, msg):
        asyncio.create_task(self.__realtime.connection.connection_manager.send_protocol_message(msg))

    def _check_pending_state(self):
        connection_state = self.__realtime.connection.connection_manager.state

        if connection_state is not ConnectionState.CONNECTED:
            log.info(f"RealtimeChannel._check_pending_state(): connection state = {connection_state}")
            return

        if self.state == ChannelState.ATTACHING:
            self.__start_state_timer()
            self._attach_impl()
        elif self.state == ChannelState.DETACHING:
            self.__start_state_timer()
            self._detach_impl()

    def __start_state_timer(self):
        if not self.__state_timer:
            def on_timeout():
                log.info('RealtimeChannel.start_state_timer(): timer expired')
                self.__state_timer = None
                self.__timeout_pending_state()

            self.__state_timer = Timer(self.__realtime.options.realtime_request_timeout, on_timeout)

    def __clear_state_timer(self):
        if self.__state_timer:
            self.__state_timer.cancel()
            self.__state_timer = None

    def __timeout_pending_state(self):
        if self.state == ChannelState.ATTACHING:
            self._notify_state(
                ChannelState.SUSPENDED, reason=AblyException("Channel attach timed out", 408, 90007))
        elif self.state == ChannelState.DETACHING:
            self._notify_state(ChannelState.ATTACHED, reason=AblyException("Channel detach timed out", 408, 90007))
        else:
            self._check_pending_state()

    # RTL23
    @property
    def name(self):
        """Returns channel name"""
        return self.__name

    # RTL2b
    @property
    def state(self):
        """Returns channel state"""
        return self.__state

    @state.setter
    def state(self, state: ChannelState):
        self.__state = state
