#!/bin/bash
# EC2 上で、コンテナごとの CPU・メモリと load average を一定間隔で記録する（負荷試験の間に動かす）
# 使い方: EC2 に送って bash loadtest_sample.sh 秒数 > stats.txt（docker stats 自体も少し CPU を使う）
END=$(( $(date +%s) + $1 ))
while [ "$(date +%s)" -lt "$END" ]; do
  printf '%s load=%s ' "$(date +%T)" "$(cut -d' ' -f1 /proc/loadavg)"
  docker stats --no-stream --format '{{.Name}}={{.CPUPerc}},{{.MemUsage}}' | sed 's/aql-sokuhou-py-//; s/-1=/=/; s/ \/ [0-9.]*MiB//' | tr '\n' ' '
  echo
done
