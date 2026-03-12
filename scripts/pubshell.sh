#!/bin/sh


if [ -n "$VOLUME" ]; then
rabbitmqadmin publish exchange=PianoSpeaker routing_key="Volume events" payload=$VOLUME
fi
 
rabbitmqadmin publish routing_key=spotifyd payload="$PLAYER_EVENT"
