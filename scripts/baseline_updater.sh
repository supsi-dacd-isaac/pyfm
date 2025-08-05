#!/bin/sh

while true; do
    minute=$(date +%M)
    if [ "$minute" = "01" ] || [ "$minute" = "16" ] || [ "$minute" = "31" ] || [ "$minute" = "46" ]; then
        python baseline_updater.py --config_file ../conf/test_fm01_aem.json --fsp supsi01 #--log_file ../logs/fsp_supsi01_baseline.log
        sleep 60  # Wait a minute to avoid running multiple times in the same minute
    else
        sleep 30  # Check again in 30 seconds
    fi
done