"""
Listener — captures audio from the PulseAudio null sink (Spotifyd output),
resamples it, and publishes chunks to the RabbitMQ "Audio chunks" queue for
the transcriber to process.

Subscribes to Spotify play/pause events to start and stop recording
in sync with playback.
"""

import pika
import sys
import os
import pyaudio
import numpy as np
import librosa
import threading
import time
import logging
from io import BytesIO

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

RECORD_SECONDS = 10

conn = pika.BlockingConnection(pika.ConnectionParameters('localhost', heartbeat=600))

channel = conn.channel()

channel.queue_declare(queue='Audio chunks')
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 2
RATE = 44100
listening = False

p = pyaudio.PyAudio()

def array_to_bytes(x: np.ndarray) -> bytes:
    """Serialize a numpy array to bytes for RabbitMQ transport."""
    np_bytes = BytesIO()
    np.save(np_bytes, x, allow_pickle=True)
    return np_bytes.getvalue()


class Listener():
    def __init__(self):
        self.listening = False
        self.last_pub_ts = time.time()
        self.dummy_array = np.zeros(79877, dtype=np.float32)
    def start(self):
        self.listening = True
    def stop(self):
        self.listening = False
    def listen(self):
        stream = p.open(format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK)
        while True:
            if self.listening:
                logger.info("* recording chunk")
                n = 0
                y = []
                s_start = int(np.round(44100 * 0.0)) * 2
                s_end = s_start + (int(np.round(44100 * RECORD_SECONDS)) * 2)
                for i in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
                    data = stream.read(CHUNK)
                    frame = librosa.core.audio.util.buf_to_float(data, dtype=np.float32)
                    n_prev = n
                    n = n + len(frame)

                    if n < s_start:
                        continue
                    if s_end < n_prev:
                        break
                    if s_end < n:
                        frame = frame[:s_end - n_prev]

                    if n_prev <= s_start <= n:
                        frame = frame[(s_start - n_prev):]
                    
                    y.append(frame)

                if y:
                    y = np.concatenate(y)
                    y = y.reshape((-1, 2)).T
                    y = librosa.core.audio.to_mono(y)

                    y = librosa.core.audio.resample(y, **{"target_sr": 16000, "orig_sr": 44100, "res_type": 'kaiser_best'})

                y = np.ascontiguousarray(y, dtype=np.float32)
                channel.basic_publish(exchange='', routing_key='Audio chunks', body=array_to_bytes(y))
            else:
                initial_buffer_data = b""
                while stream.get_read_available() > 0:
                    initial_buffer_data = stream.read(CHUNK)
                    if self.listening:
                        break
                    curr_ts = time.time()
                    if curr_ts - self.last_pub_ts > 60:
                        self.last_pub_ts = curr_ts
                        channel.basic_publish(exchange='', routing_key='Audio chunks', body=array_to_bytes(self.dummy_array))

listener = Listener()
last_msg = ""
def consume():
    global last_msg
    threaded_conn = pika.BlockingConnection(pika.ConnectionParameters('localhost', heartbeat=1800))
    spotifyd = threaded_conn.channel()
    spotifyd.queue_declare(queue='spotifyd')

    def callback(ch, method, properties, body):
        global last_msg
        segments = body.decode('ascii')
        if segments == 'play' or segments == 'start':
            if last_msg != "play" and last_msg != "start":
                listener.start()
                last_msg = segments
        elif segments == 'stop' or segments == 'pause':
            if last_msg != "stop" and last_msg != "pause":
                listener.stop()
                last_msg = segments

    spotifyd.basic_consume(queue='spotifyd', on_message_callback=callback, auto_ack=True)

    logger.info(' [*] Waiting for messages. To exit press CTRL+C')
    spotifyd.start_consuming()

if __name__ == '__main__':
    thread1 = threading.Thread(target=listener.listen)
    thread1.start()
    thread2 = threading.Thread(target=consume)
    thread2.start()
