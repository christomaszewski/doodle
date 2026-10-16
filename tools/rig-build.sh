# shellcheck shell=sh
# Rig build contract helper. Keep identical copies in service build contexts.
# Source this file, then rig_build IMAGE [docker build arguments ...].
rig_build() (
    rb_ref="$1"; shift
    rb_meta=""
    if [ -n "${RIG_BUILD_NO_CACHE:-}" ]; then
        if [ "${RIG_BUILD_PULL:-1}" = 1 ]; then set -- --pull "$@"; fi
        set -- --no-cache "$@"
    fi
    if [ -n "${RIG_BUILD_PLATFORMS:-}" ]; then
        rb_meta="$(mktemp "${TMPDIR:-/tmp}/rig-build.XXXXXX")"
        trap 'rm -f "$rb_meta"' EXIT
        if [ "${PUSH:-1}" = 0 ]; then
            case "$RIG_BUILD_PLATFORMS" in
                *,*) echo 'multiarch builds require PUSH=1; select one architecture to load locally' >&2; exit 1 ;;
            esac
            set -- --load "$@"
        else
            set -- --push "$@"
        fi
        docker buildx build --platform "$RIG_BUILD_PLATFORMS" --provenance=mode=min \
            --metadata-file "$rb_meta" -t "$rb_ref" "$@"
        if [ -n "${RIG_BUILD_RESULT:-}" ]; then
            python3 - "$rb_ref" "$rb_meta" "$RIG_BUILD_RESULT" <<'PY'
import json, sys
ref, metadata, output = sys.argv[1:]
with open(metadata) as source:
    data = json.load(source)
digest = data.get('containerimage.digest')
if not digest:
    raise SystemExit('builder metadata has no published image digest')
with open(output, 'a') as target:
    target.write(json.dumps({'ref': ref, 'digest': digest}) + '\n')
PY
        fi
    else
        docker build -t "$rb_ref" "$@"
        if [ "${PUSH:-1}" = 1 ]; then docker push "$rb_ref"; fi
    fi
)
