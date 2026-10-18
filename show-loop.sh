#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

while true
do
  ./stat-solar.sh
  sleep 29.6
done
