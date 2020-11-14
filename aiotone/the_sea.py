"""See the docstring to main()."""

from __future__ import annotations
from typing import *

import asyncio
import configparser
import functools
import itertools
import json
from pathlib import Path
import random
import sys
import time

from attr import dataclass, Factory
import click
import uvloop

from .metronome import Metronome
from .midi import (
    MidiOut,
    NOTE_OFF,
    NOTE_ON,
    CLOCK,
    START,
    STOP,
    SONG_POSITION,
    CONTROL_CHANGE,
    MOD_WHEEL,
    EXPRESSION_PEDAL,
    SUSTAIN_PEDAL,
    PORTAMENTO,
    PORTAMENTO_TIME,
    PITCH_BEND,
    ALL_NOTES_OFF,
    STRIP_CHANNEL,
    get_ports,
    get_out_port,
    silence,
)
from .notes import C, Cs, D, Ds, E, F, Fs, G, Gs, A, As, B, Db, Eb, Gb, Ab, Bb  # NoQA
from .notes import all_notes


# types
EventDelta = float  # in seconds
TimeStamp = float  # time.time()
MidiPacket = List[int]
MidiMessage = Tuple[MidiPacket, EventDelta, TimeStamp]


CURRENT_DIR = Path(__file__).parent
CONFIGPARSER_FALSE = {
    k
    for k, v in configparser.ConfigParser.BOOLEAN_STATES.items()  # type: ignore
    if v is False
}
REZ = {"rez", "resonance", "modwheel"}
PORTAMENTO_MODES = REZ | CONFIGPARSER_FALSE
CC = CONTROL_CHANGE


class PlayAsyncFunction(Protocol):
    def __call__(
        self,
        note: int,
        pulses: int,
        volume: int,
        decay: float = 0.5,
    ) -> Awaitable[None]:
        ...


class RawAsyncFunction(Protocol):
    def __call__(self, event: int, note: int, volume: int) -> Awaitable[None]:
        ...


@dataclass
class Performance:
    red_port: MidiOut
    red_channel: int
    blue_port: MidiOut
    blue_channel: int
    green_port: MidiOut
    green_channel: int
    start_stop: bool
    portamento: str
    portamento_max: int
    red_sea: List[int]
    blue_sea: List[int]
    green_sea: List[int]

    # Current state of the performance
    metronome: Metronome = Factory(Metronome)
    last_expression_value: int = 64
    blue_sequencer: Optional[asyncio.Task] = None
    red_sequencer: Optional[asyncio.Task] = None
    green_sequencer: Optional[asyncio.Task] = None
    key: List[int] = F

    def __attrs_post_init__(self) -> None:
        self.play_red = functools.partial(
            self.play, out=self.red_port, channel=self.red_channel, color="red"
        )
        self.play_blue = functools.partial(
            self.play, out=self.blue_port, channel=self.blue_channel, color="blue"
        )
        self.play_green = functools.partial(
            self.play, out=self.green_port, channel=self.green_channel, color="green"
        )

    async def setup(self) -> None:
        # Like `__attrs_post_init__` but requires awaiting so a separate step.
        silence(self.red_port, stop=self.start_stop, channels=[self.red_channel])
        silence(self.blue_port, stop=self.start_stop, channels=[self.blue_channel])
        silence(self.green_port, stop=self.start_stop, channels=[self.green_channel])
        await self.red(CC, MOD_WHEEL, 0)
        await self.blue(CC, MOD_WHEEL, 0)
        await self.green(CC, MOD_WHEEL, 0)
        await self.all(CC, PORTAMENTO, 0)
        await self.all(CC, PORTAMENTO_TIME, 0)

    async def play(
        self,
        note: int,
        pulses: int,
        volume: int,
        decay: float = 0.5,
        *,
        out: MidiOut,
        channel: int,
        color: str = "white",
    ) -> None:
        click.secho(f"-> {[NOTE_ON | channel, note, volume]}", fg=color, bold=True)
        note_on_length = int(round(pulses * decay, 0))
        rest_length = pulses - note_on_length
        out.send_message([NOTE_ON | channel, note, volume])
        await self.wait(note_on_length)
        click.secho(f"-> {[NOTE_OFF | channel, note, volume]}", fg=color, bold=True)
        out.send_message([NOTE_OFF | channel, note, volume])
        await self.wait(rest_length)

    async def wait(self, pulses: int) -> None:
        await self.metronome.wait(pulses)

    def send_once(self, message: Sequence[int]) -> None:
        """Ensure that each device receives this message only once.

        When all devices are on the same MIDI OUT port (just different channels),
        certain MIDI messages which are channel agnostic, would be effectively sent
        three times.  While that doesn't matter most of the time, when it does, use this
        method to ensure a message is only received once on each device.
        """
        rp = self.red_port
        bp = self.blue_port
        gp = self.green_port
        rp.send_message(message)
        if bp is not rp:
            bp.send_message(message)
        if gp is not rp:
            gp.send_message(message)

    # Messages received from `midi_consumer`

    async def clock(self) -> None:
        await self.metronome.tick()
        self.send_once([CLOCK])

    async def start(self) -> None:
        if self.start_stop:
            self.send_once([START])
        await self.metronome.reset()
        if not self.red_sequencer:
            self.red_sequencer = asyncio.create_task(
                self.mother_simple(self.play_red, self.red, self.red_sea)
            )
        if not self.blue_sequencer:
            self.blue_sequencer = asyncio.create_task(
                self.mother_simple(self.play_blue, self.blue, self.blue_sea)
            )
        if not self.green_sequencer:
            self.green_sequencer = asyncio.create_task(
                self.mother_simple(self.play_green, self.green, self.green_sea)
            )

    async def stop(self) -> None:
        if self.red_sequencer:
            self.red_sequencer.cancel()
            self.red_sequencer = None
        if self.blue_sequencer:
            self.blue_sequencer.cancel()
            self.blue_sequencer = None
        if self.green_sequencer:
            self.green_sequencer.cancel()
            self.green_sequencer = None
        if self.start_stop:
            self.send_once([STOP])
        await self.red(CC, ALL_NOTES_OFF, 0)
        await self.blue(CC, ALL_NOTES_OFF, 0)

    async def note_on(self, note: int, volume: int) -> None:
        for note_octaves in all_notes:
            if note in note_octaves:
                self.key = note_octaves
                break

    async def note_off(self, note: int) -> None:
        ...

    async def mod_wheel(self, value: int) -> None:
        await self.red(CC, MOD_WHEEL, value)
        if self.portamento not in REZ:
            return

        if value == 0:
            await self.all(CC, PORTAMENTO, 0)
            await self.all(CC, PORTAMENTO_TIME, 0)
        else:
            await self.all(CC, PORTAMENTO, 127)
            converted_value = int(self.portamento_max * value / 127)
            await self.all(CC, PORTAMENTO_TIME, converted_value)

    async def expression(self, value: int) -> None:
        self.last_expression_value = value
        await self.blue(CC, MOD_WHEEL, value)

    # Raw commands

    async def red(self, event: int, note: int, volume: int) -> None:
        self.red_port.send_message([event | self.red_channel, note, volume])

    async def blue(self, event: int, note: int, volume: int) -> None:
        self.blue_port.send_message([event | self.blue_channel, note, volume])

    async def green(self, event: int, note: int, volume: int) -> None:
        self.green_port.send_message([event | self.green_channel, note, volume])

    async def mothers(self, event: int, note: int, volume: int) -> None:
        self.red_port.send_message([event | self.red_channel, note, volume])
        self.blue_port.send_message([event | self.blue_channel, note, volume])

    async def all(self, event: int, note: int, volume: int) -> None:
        self.red_port.send_message([event | self.red_channel, note, volume])
        self.blue_port.send_message([event | self.blue_channel, note, volume])
        self.green_port.send_message([event | self.green_channel, note, volume])

    # Sequencers

    async def mother_simple(
        self, play: PlayAsyncFunction, raw: RawAsyncFunction, seq: List[int]
    ) -> None:
        oct = 3
        # fmt: off
        interval = [
            0, 7, 5, 10, 7,
            -12, 7, 5, 10, 7,
            -12, -12+7, -12+5, -12+10, -12+7,
            -12, 7, 5, 10, 7,
        ]
        # fmt: on

        for duration in seq:
            if duration > 0:
                print(f"Playing for {duration}")
                await play(self.key[oct] + interval[0], duration, 64, 1.0)
                interval.append(interval.pop(0))
            else:
                print(f"Waiting for {-duration}")
                await self.wait(-duration)

        print("Done.", play)


@click.command()
@click.option(
    "--config",
    help="Read configuration from this file",
    default=str(CURRENT_DIR / "aiotone-mothergen.ini"),
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    show_default=True,
)
@click.option(
    "--make-config",
    help="Write a new configuration file to standard output",
    is_flag=True,
)
def main(config: str, make_config: bool) -> None:
    """
    This is a module which generates music on three Moog synthesizers.

    To use this yourself, you will need:

    - two Mother 32 synthesizers, let's call them Red and Blue

    - MIDI connections to both Mothers, let's say Red on Channel 2, Blue on Channel 3

    - an IAC port called "IAC aiotone" which you can configure in Audio MIDI Setup on
      macOS

    You can customize the ports by creating a config file.  Use `--make-config` to
    output a new config to stdout.

    Then run `python -m aiotone.mothergen --config=PATH_TO_YOUR_CONFIG_FILE`.
    """
    if make_config:
        with open(CURRENT_DIR / "aiotone-mothergen.ini") as f:
            print(f.read())
        return

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    asyncio.run(async_main(config))


async def async_main(config: str) -> None:
    queue: asyncio.Queue[MidiMessage] = asyncio.Queue(maxsize=256)
    loop = asyncio.get_event_loop()

    cfg = configparser.ConfigParser()
    cfg.read(config)
    if cfg["from-ableton"].getint("channel") != 1:
        click.secho("from-ableton channel must be 1, sorry")
        raise click.Abort

    # Configure the `from_ableton` port
    try:
        from_ableton, to_ableton = get_ports(
            cfg["from-ableton"]["port-name"], clock_source=True
        )
    except ValueError as port:
        click.secho(f"from-ableton port {port} not connected", fg="red", err=True)
        raise click.Abort

    def midi_callback(msg, data=None):
        sent_time = time.time()
        midi_message, event_delta = msg
        try:
            loop.call_soon_threadsafe(
                queue.put_nowait, (midi_message, event_delta, sent_time)
            )
        except BaseException as be:
            click.secho(f"callback exc: {type(be)} {be}", fg="red", err=True)

    from_ableton.set_callback(midi_callback)

    # Configure the `to_mother_red` port
    if cfg["from-ableton"]["port-name"] == cfg["to-mother-red"]["port-name"]:
        to_mother_red = to_ableton
    else:
        try:
            to_mother_red = get_out_port(cfg["to-mother-red"]["port-name"])
        except ValueError as port:
            click.secho(f"{port} not connected", fg="red", err=True)
            raise click.Abort

    # Configure the `to_mother_blue` port
    if cfg["from-ableton"]["port-name"] == cfg["to-mother-blue"]["port-name"]:
        to_mother_blue = to_ableton
    elif cfg["to-mother-red"]["port-name"] == cfg["to-mother-blue"]["port-name"]:
        to_mother_blue = to_mother_red
    else:
        try:
            to_mother_blue = get_out_port(cfg["to-mother-blue"]["port-name"])
        except ValueError as port:
            click.secho(f"{port} not connected", fg="red", err=True)
            raise click.Abort

    # Configure the `to_subharmonicon` port
    if cfg["from-ableton"]["port-name"] == cfg["to-subharmonicon"]["port-name"]:
        to_subharmonicon = to_ableton
    elif cfg["to-mother-red"]["port-name"] == cfg["to-subharmonicon"]["port-name"]:
        to_subharmonicon = to_mother_red
    else:
        try:
            to_subharmonicon = get_out_port(cfg["to-subharmonicon"]["port-name"])
        except ValueError as port:
            click.secho(f"{port} not connected", fg="red", err=True)
            raise click.Abort

    porta_mode = cfg["from-ableton"]["portamento"]
    if porta_mode not in PORTAMENTO_MODES:
        click.secho(
            f"from-ableton/portamento mode not recognized. Got {porta_mode!r}, "
            f"expected one of {', '.join(PORTAMENTO_MODES)}",
            fg="red",
            err=True,
        )
        raise click.Abort

    with open(CURRENT_DIR / "the_sea.json") as json_file:
        json_contents = json.load(json_file)

    red_seq: List[int] = []
    green_seq: List[int] = []
    blue_seq: List[int] = []
    seq = [red_seq, green_seq, blue_seq]
    for line in json_contents:
        note = False
        count = 0
        for elem in line:
            if elem >= 1.0:
                if note:
                    count += 1
                else:
                    if count:
                        seq[0].append(-count)  # waits are negative
                    note = True
                    count = 1
            else:
                if not note:
                    count += 1
                else:
                    if count:
                        seq[0].append(count)  # notes are positive
                    note = False
                    count = 1

        seq.append(seq.pop(0))

    green_seq2 = []
    for dur in green_seq:
        if dur < 0 and green_seq2:
            green_seq2[-1] -= dur
        else:
            green_seq2.append(dur)

    green_seq = green_seq2

    print("red:", len(red_seq), sum(abs(n) for n in red_seq))
    print("blue:", len(blue_seq), sum(abs(n) for n in blue_seq))
    print("green:", len(green_seq), sum(abs(n) for n in green_seq))

    performance = Performance(
        red_port=to_mother_red,
        blue_port=to_mother_blue,
        green_port=to_subharmonicon,
        red_channel=cfg["to-mother-red"].getint("channel") - 1,
        blue_channel=cfg["to-mother-blue"].getint("channel") - 1,
        green_channel=cfg["to-subharmonicon"].getint("channel") - 1,
        start_stop=cfg["from-ableton"].getboolean("start-stop"),
        portamento=cfg["from-ableton"]["portamento"],
        portamento_max=cfg["from-ableton"].getint("portamento-max"),
        red_sea=red_seq,
        blue_sea=blue_seq,
        green_sea=green_seq,
    )

    await performance.setup()

    try:
        await midi_consumer(queue, performance)
    except asyncio.CancelledError:
        from_ableton.cancel_callback()
        silence(to_ableton, stop=performance.start_stop)
        silence(to_mother_red, stop=performance.start_stop)
        silence(to_mother_blue, stop=performance.start_stop)


async def midi_consumer(
    queue: asyncio.Queue[MidiMessage], performance: Performance
) -> None:
    print("Waiting for MIDI messages...")
    system_realtime = {START, STOP, SONG_POSITION}
    notes = {NOTE_ON, NOTE_OFF}
    handled_types = system_realtime | notes | {CONTROL_CHANGE}
    while True:
        msg, delta, sent_time = await queue.get()
        latency = time.time() - sent_time
        # Note hack below. We are matching the default which is channel 1 only.
        # This is what we want.
        t = msg[0]
        if t == CLOCK:
            await performance.clock()
            continue

        st = t & STRIP_CHANNEL
        if st == STRIP_CHANNEL:  # system realtime message didn't have a channel
            st = t
        if __debug__ and st == t:
            fg = "white"
            if t in system_realtime:
                fg = "blue"
            elif t == CONTROL_CHANGE:
                fg = "yellow"
            click.secho(
                f"{msg}\tevent delta: {delta:.4f}\tlatency: {latency:.4f}", fg=fg
            )
        if t == START:
            await performance.start()
        elif t == STOP:
            await performance.stop()
        elif t == NOTE_ON:
            await performance.note_on(msg[1], msg[2])
        elif t == NOTE_OFF:
            await performance.note_off(msg[1])
        elif t == CONTROL_CHANGE:
            if msg[1] == MOD_WHEEL:
                await performance.mod_wheel(msg[2])
            elif msg[1] == EXPRESSION_PEDAL:
                await performance.expression(msg[2])
            elif msg[1] == SUSTAIN_PEDAL:
                await performance.all(CC, SUSTAIN_PEDAL, msg[2])
            elif msg[1] == ALL_NOTES_OFF:
                await performance.all(CC, ALL_NOTES_OFF, msg[2])
            else:
                print(f"warning: unhandled CC {msg}", file=sys.stderr)
        elif t == PITCH_BEND:
            # Note: this requires Mother 32 firmware 2.0.
            await performance.all(PITCH_BEND, msg[1], msg[2])
        else:
            if st not in handled_types:
                print(f"warning: unhandled event {msg}", file=sys.stderr)


if __name__ == "__main__":
    main()
