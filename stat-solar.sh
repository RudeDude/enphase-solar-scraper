#!/bin/bash
# Print today's local samples. Daily files follow the computer clock
# (America/New_York, same zone the gateway reports as US/Eastern).
set -euo pipefail
cd "$(dirname "$0")"

D=$(date +'%Y-%m-%d')
Y=$(date +'%Y')
M=$(date +'%m')
F="data/$Y/$M/solar-log-$D.json"

if [[ ! -s "$F" ]]; then
  echo "No samples yet today ($F)"
  exit 0
fi

echo
echo "Recent wNow:"
jq '.wNow' "$F" | tail -n 22
echo
echo "Latest sample:"
tail -n 1 "$F" | jq '{
  activeCount,
  measurementType,
  wNow,
  rmsCurrent,
  rmsVoltage,
  reactPwr,
  apprntPwr,
  whToday,
  whTodayRaw,
  whTodaySource,
  whLifetime,
  pcu,
  inverterSummary
}'

echo -n "Max wNow today: "
jq '.wNow' "$F" | sort -g | tail -n 1
