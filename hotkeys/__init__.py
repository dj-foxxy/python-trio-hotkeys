from __future__ import annotations
from dataclasses import dataclass
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from operator import itemgetter
from typing import Any, Awaitable, Final, Type, TypeAlias, TypeVar

import trio

from xcffib import Connection, CurrentTime, VoidCookie, connect
from xcffib.xproto import (
    GrabKeyboardCookie,
    GrabMode,
    KeyButMask,
    KeyPressEvent,
    KeyReleaseEvent,
    Setup,
)

from . import keysymdef


T = TypeVar('T')


TRIVIAL_MODS: Final = [
    0,
    KeyButMask.Lock,
    KeyButMask.Mod2,
    KeyButMask.Lock | KeyButMask.Mod2,
]


def _create_modifier_string_to_mask() -> dict[str, int]:
    items = list[tuple[str, int]]()
    for key, value in KeyButMask.__dict__.items():
        if isinstance(value, int):
            items.append((key, value))
    items.sort(key=itemgetter(1))
    return dict(items)


MODIFIER_STRING_TO_MASK: Final = _create_modifier_string_to_mask()
del _create_modifier_string_to_mask


def check_instance(object: Any, type: Type[T]) -> T:
    if isinstance(object, type):
        return object
    raise ValueError(f'object must be an instance of {type.__name__}')


def create_modifiers(*modifiers: str) -> int:
    mask = 0
    for modifier in modifiers:
        mask |= MODIFIER_STRING_TO_MASK[modifier]
    return mask


def get_keysym(keyname: str) -> int:
    return keysymdef.keyname_to_keysym[keyname]


class KeyboardMap:
    def __init__(self, conn: Connection, setup: Setup) -> None:
        self.min_keycode = check_instance(setup.min_keycode, int)
        max_keycode = check_instance(setup.max_keycode, int)
        keycode_count = (max_keycode - self.min_keycode) + 1
        cookie = conn.core.GetKeyboardMapping(self.min_keycode, keycode_count)
        reply = cookie.reply()
        self.keysyms = list[int](reply.keysyms)
        self.keysyms_per_keycode = check_instance(reply.keysyms_per_keycode, int)
        self.keysym_to_keycode = dict[int, int]()
        for keycode in range(self.min_keycode, max_keycode + 1):
            for column in range(self.keysyms_per_keycode):
                keysym = self.get_keysym(keycode, column=column)
                if keysym != 0 and keysym not in self.keysym_to_keycode:
                    self.keysym_to_keycode[keysym] = keycode
                    break

    def get_keycode(self, keysym: int) -> int:
        return self.keysym_to_keycode[keysym]

    def get_keysym(self, keycode: int, column: int = 0) -> int:
        index = (keycode - self.min_keycode) * self.keysyms_per_keycode + column
        return self.keysyms[index]


class ModifierMap:
    def __init__(self, conn: Connection, keyboard_map: KeyboardMap) -> None:
        reply = conn.core.GetModifierMapping().reply()
        keycodes = list(reply.keycodes)
        kpm = reply.keycodes_per_modifier
        it = zip(range(0, len(keycodes) - kpm, kpm), MODIFIER_STRING_TO_MASK)
        modifier_map = defaultdict[str, set[int]](set)
        for i, modifier_name in it:
            for modifier_keycode in keycodes[i : i + kpm]:
                if modifier_keycode != 0:
                    keysym = keyboard_map.get_keysym(modifier_keycode)
                    if keysym != 0:
                        modifier_map[modifier_name].add(keysym)
        self._to_keysym = dict(modifier_map)
        self._to_name = dict[int, str]()
        for name, keysyms in modifier_map.items():
            for keysym in keysyms:
                self._to_name[keysym] = name

    def get_name(self, keysym: int) -> str:
        return self._to_name[keysym]

    def get_keysyms(self, name: str) -> set[int]:
        return self._to_keysym[name]


@contextmanager
def open_connection() -> Iterator[Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.disconnect()


def check_cookie(cookie: Any) -> None:
    check_instance(cookie, VoidCookie).check()


def _grab_key(conn: Connection, window: int, modifiers: int, keycode: int) \
        -> None:
    check_cookie(conn.core.GrabKey(owner_events=False, grab_window=window,
                                   modifiers=modifiers, key=keycode,
                                   pointer_mode=GrabMode.Async,
                                   keyboard_mode=GrabMode.Async,
                                   is_checked=True))


def _ungrab_key(conn: Connection, window: int, modifiers: int, keycode: int) \
        -> None:
    check_cookie(conn.core.UngrabKey(key=keycode, grab_window=window,
                                     modifiers=modifiers, is_checked=True))


@contextmanager
def grab_key(conn: Connection, window: int, modifiers: int, keycode: int) \
        -> Iterator[None]:
    _grab_key(conn, window, modifiers, keycode)
    try:
        yield
    finally:
        _ungrab_key(conn, window, modifiers, keycode)


def _grab_keyboard(conn: Connection, window: int) -> None:
    check_instance(conn.core.GrabKeyboard(
        owner_events=True,
        grab_window=window,
        time=CurrentTime,
        pointer_mode=GrabMode.Async,
        keyboard_mode=GrabMode.Async,
        is_checked=True,
    ), GrabKeyboardCookie).reply()


def _ungrab_keyboard(conn: Connection) -> None:
    check_cookie(conn.core.UngrabKeyboard(CurrentTime, is_checked=True))


class GrabKeyboard:
    def __init__(self, conn: Connection, window: int) -> None:
        self._conn = conn
        self._window = window
        self._grabbed = False

    def grab(self) -> None:
        if not self._grabbed:
            _grab_keyboard(self._conn, self._window)
            self._grabbed = True

    def ungrab(self) -> None:
        if self._grabbed:
            _ungrab_keyboard(self._conn)
            self._grabbed = False


@contextmanager
def open_grab_keyboard(conn: Connection, window: int) -> Iterator[GrabKeyboard]:
    grab = GrabKeyboard(conn, window)
    try:
        yield grab
    finally:
        grab.ungrab()


async def wait_events(conn: Connection) \
        -> AsyncIterator[KeyPressEvent | KeyReleaseEvent]:
    fd = check_instance(conn.get_file_descriptor(), int)
    await trio.lowlevel.wait_readable(fd)
    event: object
    for event in iter(conn.poll_for_event, None):
        if isinstance(event, (KeyPressEvent, KeyReleaseEvent)):
            yield event


class Binding:
    def __init__(
        self,
        name: str,
        modifier_keycodes: set[int],
        modifiers: int,
        keycode: int,
        on_press: Callable[[], Awaitable[None] | None],
        on_release: Callable[[], Awaitable[None] | None],
    ) -> None:
        self.name = name
        self.modifiers = modifiers
        self.modifier_keycodes = modifier_keycodes
        self.keycode = keycode
        self._on_press = on_press
        self._on_release = on_release
        self._is_pressed = False
        self._was_pressed = self._is_pressed
        self._dirty = False

    async def commit(self) -> None:
        if not self._dirty:
            return
        self._was_pressed = self._is_pressed
        self._dirty = False
        result = self._on_press() if self._is_pressed else self._on_release()
        if result is not None:
            await result

    def _set_pressed(self, is_pressed: bool) -> None:
        if is_pressed != self._is_pressed:
            self._is_pressed = is_pressed
            self._dirty = is_pressed != self._was_pressed

    def press(self) -> None:
        self._set_pressed(True)

    def release(self) -> None:
        self._set_pressed(False)


@asynccontextmanager
async def ensure_release(binding: Binding):
    try:
        yield binding
    finally:
        if binding._was_pressed:
            result = binding._on_release()
            if result is not None:
                with trio.move_on_after(1) as cancel_scope:
                    cancel_scope.shield = True
                    await result


@dataclass
class Hotkey:
    modifier: str
    key: str
    on_press: Callable[[], Awaitable[None] | None]
    on_release: Callable[[], Awaitable[None] | None]


HotkeySpec: TypeAlias = tuple[
    str,
    str,
    Callable[[], Awaitable[None] | None],
    Callable[[], Awaitable[None] | None],
]


async def run(spec: Iterable[HotkeySpec]) -> None:
    hks = [Hotkey(modifier, key, on_press, on_release)
           for modifier, key, on_press, on_release in spec]

    with open_connection() as conn:
        async with AsyncExitStack() as stack:
            setup = check_instance(conn.get_setup(), Setup)
            keyboard_map = KeyboardMap(conn, setup)
            modifier_map = ModifierMap(conn, keyboard_map)
            window = check_instance(setup.roots[0].root, int)

            bindings = dict[tuple[int, int], Binding]()

            for hotkey in hks:
                base_modifiers = create_modifiers(hotkey.modifier)
                modifier_keycodes = {
                        keyboard_map.get_keycode(keysym)
                        for keysym in modifier_map.get_keysyms(hotkey.modifier)}
                keycode = keyboard_map.get_keycode(get_keysym(hotkey.key))
                for trivial_mod in TRIVIAL_MODS:
                    modifiers = trivial_mod | base_modifiers
                    binding = Binding(
                        f'{hotkey.modifier} + {hotkey.key}',
                        modifier_keycodes,
                        modifiers,
                        keycode,
                        hotkey.on_press,
                        hotkey.on_release,
                    )
                    bindings[(modifiers, keycode)] = binding
                    await stack.enter_async_context(ensure_release(binding))
                    stack.enter_context(grab_key(conn, window, modifiers, keycode))

            modifier_index = defaultdict[int, set[Binding]](set)
            for binding in bindings.values():
                for modifier_keycode in binding.modifier_keycodes:
                    modifier_index[modifier_keycode].add(binding)

            def count_pressed() -> int:
                return sum(1 for binding in bindings.values() if binding._is_pressed)

            old_pressed = count_pressed()

            grab_keyboard = stack.enter_context(open_grab_keyboard(conn, window))

            while True:
                async for event in wait_events(conn):
                    state = check_instance(event.state, int)
                    detail = check_instance(event.detail, int)
                    binding = bindings.get((state & 0x7f, detail))

                    if isinstance(event, KeyPressEvent):
                        if binding is not None:
                            binding.press()
                        continue

                    if binding is not None:
                        binding.release()
                        continue

                    for binding in modifier_index[detail]:
                        binding.release()

                for binding in bindings.values():
                    await binding.commit()

                count = count_pressed()
                if count != 0 and old_pressed == 0:
                    grab_keyboard.grab()
                elif count == 0 and old_pressed != 0:
                    grab_keyboard.ungrab()
                old_pressed = count
