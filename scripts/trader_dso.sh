#!/bin/sh

while true; do
    minute=$(date +%M)
    if [ "$minute" = "02" ] || [ "$minute" = "17" ] || [ "$minute" = "32" ] || [ "$minute" = "47" ]; then
        python trader_dso.py --config_file ../conf/test_fm01_aem.json #--log_file ../logs/dso_aem.log
        sleep 60  # Wait a minute to avoid running multiple times in the same minute
    else
        sleep 30  # Check again in 30 seconds
    fi
done