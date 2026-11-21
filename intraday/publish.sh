#!/bin/bash
# GitHub Actions 版發布（由 intraday.py 呼叫，取代 box 的 publish.sh）
#  data 分支：data.json / intraday.json（每次有變就推；先對齊 origin，不動 box 擁有的其他檔）
#  main 分支：data.json 備援 / hist.json / hist/ / heat_close.json（FORCE 或每 30 分鐘）
SRC=/workspace/intraday/public; D=$RUNNER_TEMP/site-data; M=$RUNNER_TEMP/site-main; STAMP=$RUNNER_TEMP/.last_main
exec 9>"$RUNNER_TEMP/.publish.lock"; flock -w 100 9 || exit 0
[ -f "$SRC/data.json" ] || exit 0
pushd_data() {
  cd "$D" || return 1
  for i in 1 2 3; do
    git fetch -q --depth 1 origin data && git reset -q --hard FETCH_HEAD || return 1
    cp "$SRC/data.json" data.json; [ -f "$SRC/intraday.json" ] && cp "$SRC/intraday.json" intraday.json
    git add -A -- data.json intraday.json
    git diff --cached --quiet && { echo "data no change"; return 0; }
    git commit -qm "data $(date '+%F %T') [gha]" && git push -q origin HEAD:data && { echo "data pushed"; return 0; }
    sleep 3
  done; echo "data push failed"
}
pushd_main() {
  cd "$M" || return 1
  for i in 1 2 3; do
    git fetch -q --depth 1 origin main && git reset -q --hard FETCH_HEAD || return 1
    cp "$SRC/data.json" data.json
    [ -f "$SRC/hist.json" ] && cp "$SRC/hist.json" hist.json
    [ -f "$SRC/heat_close.json" ] && cp "$SRC/heat_close.json" heat_close.json
    [ -d "$SRC/hist" ] && [ "$(ls "$SRC/hist" | wc -l)" -gt 1000 ] && { rm -rf hist; cp -r "$SRC/hist" hist; }
    git add -A -- data.json hist.json hist heat_close.json
    git diff --cached --quiet && { echo "main no change"; touch "$STAMP"; return 0; }
    git commit -qm "page $(date '+%F %T') [gha]" && git push -q origin HEAD:main && { touch "$STAMP"; echo "main pushed"; return 0; }
    sleep 3
  done; echo "main push failed"
}
pushd_data
if [ -n "$FORCE" ] || [ ! -f "$STAMP" ] || [ $(( $(date +%s) - $(stat -c %Y "$STAMP") )) -ge 1800 ]; then pushd_main; fi
