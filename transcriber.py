"""
Transcriber — consumes raw audio chunks from RabbitMQ, runs them through
the piano-transcription-inference AI model, and publishes the resulting
MIDI note/pedal events back to RabbitMQ for the player.
"""

import pika
import sys
from piano_transcription_inference import PianoTranscription
import numpy as np
from piano_transcription_inference.utilities import RegressionPostProcessor
from piano_transcription_inference.pytorch_utils import forward
from mido import Message, MetaMessage
import logging
import pickle
from io import BytesIO
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

def bytes_to_array(b: bytes) -> np.ndarray:
    np_bytes = BytesIO(b)
    return np.load(np_bytes, allow_pickle=True)

def tuple_to_bytes(x: tuple) -> bytes:
    return pickle.dumps(x)


class StreamingTranscription(PianoTranscription):
    def __init__(self, pub):
        super().__init__()
        self.pub = pub
        self.notes = {}
        self.pedal = {}

    def drop_pedal(self):
        pedal_to_drop = []
        for pedal in self.pedal:
            if self.pedal[pedal] == 1:
                pedal_to_drop.append(pedal)
                self.pedal[pedal] = 0
        return pedal_to_drop
    
    def send_pedal_to_off(self, messages):
        pedal_to_drop = self.drop_pedal()
        for pedal in pedal_to_drop:
            messages.insert(-1, Message('control_change', control=pedal, value=0, time=0))
        return messages

    def drop_notes(self):
        notes_to_drop = []
        for note in self.notes:
            if self.notes[note] != 0:
                notes_to_drop.append(note)
                self.notes[note] = 0
        return notes_to_drop
    
    def send_notes_to_off(self, messages):
        notes_to_drop = self.drop_notes()
        for note in notes_to_drop:
            messages.insert(-1, Message('note_on', note=note, velocity=0, time=0))
        return messages

    def streaming_transcribe(self, audio):
        audio = audio[None, :]
        audio_len = audio.shape[1]
        pad_len = int(np.ceil(audio_len / self.segment_samples))\
            * self.segment_samples - audio_len
        audio = np.concatenate((audio, np.zeros((1, pad_len))), axis=1)
        segments = self.enframe(audio, self.segment_samples)

        output_dict = forward(model = self.model, x = segments, batch_size = 1)

        for key in output_dict.keys():
            output_dict[key] = self.deframe(output_dict[key])[0 : audio_len]

        post_processor = RegressionPostProcessor(self.frames_per_second, 
        classes_num=self.classes_num, onset_threshold=self.onset_threshold, 
        offset_threshold=self.offset_threshod, 
        frame_threshold=self.frame_threshold, 
        pedal_offset_threshold=self.pedal_offset_threshold)


        (est_note_events, est_pedal_events) = post_processor.output_dict_to_midi_events(output_dict)
        return self.convert_to_midi_messages((est_note_events, est_pedal_events))
        # Pub to next topic

    def convert_to_midi_messages(self, event):
        message_roll = []
        start_time = 0
        ticks_per_beat = 384
        beats_per_second = 2
        ticks_per_second = ticks_per_beat * beats_per_second
        microseconds_per_beat = int(1e6 // beats_per_second)
        note_events = event[0]
        pedal_events = event[1]
        for note_event in note_events:
            # Onset
            message_roll.append({
                'time': note_event['onset_time'] , 
                'midi_note': note_event['midi_note'], 
                'velocity': note_event['velocity']})

            # Offset
            message_roll.append({
                'time': note_event['offset_time'], 
                'midi_note': note_event['midi_note'], 
                'velocity': 0})
            

        if pedal_events:
            for pedal_event in pedal_events:
                message_roll.append({'time': pedal_event['onset_time'], 'control_change': 64, 'value': 127})
                message_roll.append({'time': pedal_event['offset_time'], 'control_change': 64, 'value': 0})

        # Sort MIDI messages by time
        message_roll.sort(key=lambda note_event: note_event['time'])

        previous_ticks = 0
        messages = []
        for message in message_roll:
            this_ticks = int((message['time'] - start_time) * ticks_per_second)
            if this_ticks >= 0:
                diff_ticks = this_ticks - previous_ticks
                if diff_ticks == 0:
                    continue
                previous_ticks = this_ticks
                if 'midi_note' in message.keys():
                    messages.append(Message('note_on', note=message['midi_note'], velocity=message['velocity'], time=diff_ticks))
                    if message['velocity'] == 0:
                        self.notes[message['midi_note']] = 0
                    else:
                        self.notes[message['midi_note']] = time.time()
                elif 'control_change' in message.keys():
                    messages.append(Message('control_change', channel=0, control=message['control_change'], value=message['value'], time=diff_ticks))
                    if message['value'] == 0:
                        self.pedal[message['control_change']] = 0
                    else:
                        self.pedal[message['control_change']] = 1

        notes_to_turn_off = []
        for note, timestamp in self.notes.items():
            if timestamp != 0 and (time.time() - timestamp) > 8:
                notes_to_turn_off.append(note)
        
        for note in notes_to_turn_off:
            messages.append(Message('note_on', note=note, velocity=0, time=0))
            self.notes[note] = 0
        messages.append(MetaMessage('end_of_track', time=1))
        if len(messages) == 1 and isinstance(messages[0], MetaMessage) and messages[0].type == 'end_of_track':
            messages = self.send_notes_to_off(messages)
            messages = self.send_pedal_to_off(messages)
        return messages

def get_dummy_array():
    return np.zeros(79877, dtype=np.float32)

def main():
    connection = pika.BlockingConnection(pika.ConnectionParameters(host='localhost', heartbeat=1800))
    sub_channel = connection.channel()
    pub_channel = connection.channel()
    transcriber = StreamingTranscription(pub_channel)
    # warm up
    transcriber.streaming_transcribe(get_dummy_array())

    sub_channel.queue_declare(queue='Audio chunks')
    pub_channel.queue_declare(queue='Note events')

    def callback(ch, method, properties, body):
        logger.info('received message!')
        res = transcriber.streaming_transcribe(audio=bytes_to_array(body))
        pub_channel.basic_publish(exchange='PianoSpeaker', routing_key='Note events', body=tuple_to_bytes(res))

    sub_channel.basic_consume(queue='Audio chunks', on_message_callback=callback, auto_ack=True)

    logger.info(' [*] Waiting for messages. To exit press CTRL+C')
    sub_channel.start_consuming()

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info('Interrupted')
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)
