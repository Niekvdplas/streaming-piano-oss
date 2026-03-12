#!/bin/bash

# Function to check internet connectivity
check_internet() {
    local retries=0
    local max_retries=3
    local wait_time=3

    while [[ $retries -lt $max_retries ]]; do
        wget -q --spider http://google.com
        if [[ $? -eq 0 ]]; then
            return 0
        fi
        retries=$((retries+1))
        sleep $wait_time
    done

    return 1
}

# Check for internet connection
if check_internet; then
    echo "Internet connection is active."
else
    echo "No internet connection found."

    # Launch Balena WiFi Connect
    echo "Launching WiFi Connect to select a network..."
    /dist/wifi/wifi-connect --portal-ssid PianoSpeaker -u /dist/wifi/ui
fi

