#!/bin/sh

while true; do
    minute=$(date +%M)
    if [ "$minute" = "03" ] || [ "$minute" = "18" ] || [ "$minute" = "33" ] || [ "$minute" = "48" ]; then
        python trader_fsp.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 #--log_file ../logs/fsp_supsi01.log
        sleep 60  # Wait a minute to avoid running multiple times in the same minute
    else
        sleep 30  # Check again in 30 seconds
    fi
done