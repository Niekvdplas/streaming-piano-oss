#!/bin/sh


/dist/spotifyd --no-daemon --backend="pulseaudio" --bitrate="320" --device="spotifySink" --device-name "PianoSpeaker" --onevent /dist/pubshell.sh



