"""
Player — receives MIDI messages from the transcriber via RabbitMQ and sends
them to a hardware MIDI output device (synthesizer).  Also handles volume
events from Spotify to scale playback velocity dynamically.
"""

import pika
import sys
import os
import time
from mido import Message, MidiFile, MidiTrack, MetaMessage
import mido
import threading
import logging
import pickle

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

volume = 65535

def bytes_to_tuple(b: bytes) -> tuple:
    return pickle.loads(b)

class MidiStreaming:
    def __init__(self, ticks_per_beat=384, messages=None):
        self.ticks_per_beat = ticks_per_beat
        self.last_added = 0
        self.tempo = int(1e6 // 2)
        self.messages = []
        if messages != None:
            self.add_messages(messages)

    def __iter__(self):
        tempo = self.tempo
        for msg in self.messages:
            if msg.time > 0:
                delta = tick2second(msg.time, self.ticks_per_beat, tempo)
            else:
                delta = 0

            yield msg.copy(time=delta)

            if msg.type == "set_tempo":
                tempo = msg.tempo

    def play(self):
        tempo = self.tempo
        while True:
            if self.messages:
                start_time = time.time()
                input_time = 0.0
                msg = self.messages.pop(0)
                if msg.time > 0:
                    msg.time = tick2second(msg.time, self.ticks_per_beat, tempo)
                else:
                    msg.time = 0
                if msg.type == "set_tempo":
                    tempo = msg.tempo
                input_time += msg.time

                playback_time = time.time() - start_time
                duration_to_next_event = input_time - playback_time

                if duration_to_next_event > 0.0:
                    time.sleep(duration_to_next_event)

                if isinstance(msg, MetaMessage):
                    continue
                else:
                    yield msg

    def add_messages(self, messages_list):
        messages = []
        messages.extend(self._to_abstime(messages_list))
        messages.sort(key=lambda msg: msg.time)
        test = _to_reltime(messages)
        self.messages.extend(test)
        self.messages = list(fix_end_of_track(self.messages))


    def _to_abstime(self, messages):
        now = 0.0
        for msg in messages:
            now += msg.time
            self.last_added = now
            yield msg.copy(time=now)


def fix_end_of_track(messages):
    """Remove all end_of_track messages and add one at the end.

    This is used by merge_tracks() and MidiFile.save()."""
    # Accumulated delta time from removed end of track messages.
    # This is added to the next message.
    accum = 0

    for msg in messages:
        if msg.type == "end_of_track":
            accum += msg.time
        else:
            if accum:
                delta = accum + msg.time
                yield msg.copy(time=delta)
                accum = 0
            else:
                yield msg

    yield MetaMessage("end_of_track", time=accum)


def tick2second(tick, ticks_per_beat, tempo):
    """Convert absolute time in ticks to seconds.

    Returns absolute time in seconds for a chosen MIDI file time resolution
    (ticks/pulses per quarter note, also called PPQN) and tempo (microseconds
    per quarter note).
    """
    scale = tempo * 1e-6 / ticks_per_beat
    return tick * scale


def _to_reltime(messages, incr_time=0):
    """Convert messages to relative time."""
    now = incr_time
    for msg in messages:
        delta = msg.time - now
        yield msg.copy(time=delta)
        now = msg.time

player_helper = MidiStreaming()

def normalize_volume(volume) -> float:
    return 1 + (5 - 1) * ((volume - 65535) / (32768 - 65535))

def soft_knee_compression(velocity, threshold, ratio, knee):
    if velocity <= threshold - knee:
        return velocity
    elif threshold - knee < velocity <= threshold + knee:
        return int(velocity - (velocity - threshold + knee)/2 * (1 - 1/ratio))
    else:
        return int(threshold + (velocity - threshold) / ratio)

MIDI_DEVICE_INDEX = int(os.environ.get('MIDI_DEVICE_INDEX', 1))


def midi_player():
    global volume

    output_names = mido.get_output_names()
    if not output_names:
        logger.error("No MIDI output devices found")
        return
    if MIDI_DEVICE_INDEX >= len(output_names):
        logger.warning(f"MIDI device index {MIDI_DEVICE_INDEX} out of range, using device 0: {output_names[0]}")
        device_name = output_names[0]
    else:
        device_name = output_names[MIDI_DEVICE_INDEX]

    logger.info(f"Opening MIDI output: {device_name}")
    with mido.open_output(device_name) as output:
        output.reset()

        for message in player_helper.play():
            if isinstance(message, Message):
                if message.type == 'note_on':
                    if volume < 32767:
                        scaledmessage = message.copy(velocity=0)
                    else:
                        scaledmessage = message.copy(velocity=soft_knee_compression(message.velocity, 30, normalize_volume(volume), 10))
                else:
                    scaledmessage = message.copy()

                output.send(scaledmessage)

def main():
    global player_helper
    connection = pika.BlockingConnection(pika.ConnectionParameters(host='localhost', heartbeat=1800))
    channel = connection.channel()

    # Define the routing keys you want to subscribe to
    routing_keys = ['Note events', 'Volume events']

    # Create a single queue and bind it to both routing keys
    channel.exchange_declare('PianoSpeaker')
    result = channel.queue_declare(queue='', exclusive=True)
    queue_name = result.method.queue

    for routing_key in routing_keys:
        channel.queue_bind(exchange='PianoSpeaker', queue=queue_name, routing_key=routing_key)

    def callback(ch, method, properties, body):
        logger.info('received message!')
        global volume
        if method.routing_key == 'Volume events':
            volume = int(body)
        else:
            player_helper.add_messages(bytes_to_tuple(body))
            logger.info(player_helper.messages)

    channel.basic_consume(queue=queue_name, on_message_callback=callback, auto_ack=True)

    logger.info(' [*] Waiting for messages. To exit press CTRL+C')
    channel.start_consuming()

if __name__ == '__main__':
    try:
        thread = threading.Thread(target=midi_player)
        thread.start()
        main()
    except KeyboardInterrupt:
        logger.info('Interrupted')
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)
