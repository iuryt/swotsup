#!/bin/bash
# Apply one bug at a time (tests/mutations.sh) and check that the tests catch it. Each runs in its own copy of app/
# and tests/ on the node's disk, several at once, with this project's environment and swot_cloud as of its last
# commit: the repository is never edited (servers and other work on it can go on), and swot_cloud's own mutation runs
# do not get in. Usage: sbatch slurm/mutants.sh [PATTERN]   (only the mutations whose label matches, a bash regex)
#SBATCH -p batch -c 16 --mem 32G -t 60 -o logs/mutants-%j.out
cd "${SLURM_SUBMIT_DIR:-$PWD}"
PATTERN=${1:-} JOBS=${JOBS:-8} PYTHON=$PWD/.pixi/envs/default/bin/python
BASE=$(mktemp -d "${TMPDIR:-/tmp}/mutants-XXXX")
trap 'rm -rf "$BASE"' EXIT
mkdir "$BASE/src"
cp -r app tests pyproject.toml "$BASE/src/"
git -C ../swot_cloud archive HEAD swot_cloud | tar -x -C "$BASE/src"  # swot_cloud as committed (before the .pth)
export PYTHONDONTWRITEBYTECODE=1
echo "swot_cloud from $(cd "$BASE/src" && PYTHONPATH=$PWD "$PYTHON" -c 'import swot_cloud; print(swot_cloud.__file__)')"

files=() exprs=() labels=()
mutate() { files+=("$1"); exprs+=("$2"); labels+=("$3"); }
source tests/mutations.sh

one() {  # index, file, sed expression, label: in a fresh copy
    local d=$BASE/$1
    cp -r "$BASE/src" "$d" && cd "$d" && sed -i "$3" "$2"
    if cmp -s "$2" "$BASE/src/$2"; then echo "NOT APPLIED: $4"
    elif PYTHONPATH=$d timeout 300 "$PYTHON" -m pytest -q -x -p no:cacheprovider tests >/dev/null 2>&1; then echo "SURVIVED: $4"
    else echo "caught: $4"; fi
    rm -rf "$d"
}

start=$SECONDS run=()
for k in "${!labels[@]}"; do
    [[ -z $PATTERN || ${labels[k]} =~ $PATTERN ]] || continue
    while (( $(jobs -rp | wc -l) >= JOBS )); do wait -n; done
    one "$k" "${files[k]}" "${exprs[k]}" "${labels[k]}" > "$BASE/out.$k" &
    run+=("$k")
done
wait
for k in "${run[@]}"; do cat "$BASE/out.$k"; done
echo "${#run[@]} mutations in $((SECONDS - start)) s"
